"""LLM router: build a structured-output-capable chat model from RunConfig.

This is the **only** place in the codebase that touches langchain_openai or
the OpenAI SDK directly. Business modules call ``router.invoke_structured(...)``
and ``router.invoke_text(...)``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from abi.providers.llm.budget import BudgetGate
from abi.providers.llm.pricing import estimate_cost_usd
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.providers.observability.langfuse_client import (
    LangfuseStatus,
    build_langfuse_handler,
    flush_handler,
)
from abi.types.run import LLMConfig, RunConfig

_log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


def _build_transient_error_tuple() -> tuple[type[BaseException], ...]:
    """Collect transient OpenAI-SDK error types if the SDK is installed.

    We only retry on these; parse / validation errors are handled separately.
    """
    out: list[type[BaseException]] = [asyncio.TimeoutError, TimeoutError, ConnectionError]
    try:
        import openai

        for name in (
            "APITimeoutError",
            "APIConnectionError",
            "RateLimitError",
            "InternalServerError",
        ):
            cls = getattr(openai, name, None)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                out.append(cls)
    except Exception:  # pragma: no cover
        pass
    return tuple(out)


_TRANSIENT_LLM_ERRORS: tuple[type[BaseException], ...] = _build_transient_error_tuple()


def _build_permanent_error_tuple() -> tuple[type[BaseException], ...]:
    """Collect non-retryable OpenAI request/authentication error types."""
    out: list[type[BaseException]] = []
    try:
        import openai

        for name in (
            "BadRequestError",
            "AuthenticationError",
            "PermissionDeniedError",
            "NotFoundError",
        ):
            cls = getattr(openai, name, None)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                out.append(cls)
    except Exception:  # pragma: no cover
        pass
    return tuple(out)


_PERMANENT_LLM_ERRORS: tuple[type[BaseException], ...] = _build_permanent_error_tuple()


@dataclass
class LLMResponse:
    text: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    retries: int


class LLMRouter:
    """Thin facade over a LangChain chat model with budget + observability.

    Stateful: holds a BudgetGate and Langfuse handler per run.
    """

    def __init__(
        self,
        *,
        config: LLMConfig,
        api_key: str,
        budget: BudgetGate,
        events: EventLogger,
        metrics: MetricsAggregator,
        langfuse_handler: Any | None,
        langfuse_status: LangfuseStatus,
        sem: asyncio.Semaphore,
    ) -> None:
        self._config = config
        self._api_key = api_key
        self._budget = budget
        self._events = events
        self._metrics = metrics
        self._langfuse = langfuse_handler
        self._langfuse_status = langfuse_status
        self._sem = sem
        self._chat: BaseChatModel = ChatOpenAI(
            base_url=config.base_url,
            api_key=api_key,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_output_tokens,
            timeout=config.request_timeout_s,
            max_retries=0,  # we handle retries ourselves at a coarser level
        )
        # Cache of (model_name -> ChatOpenAI) for ``model_override`` calls,
        # so per-call swaps don't pay reconstruction cost on every invocation.
        self._chat_by_model: dict[str, BaseChatModel] = {config.model: self._chat}

    @property
    def model(self) -> str:
        return self._config.model

    @property
    def base_url(self) -> str:
        return self._config.base_url

    @property
    def total_cost(self) -> float:
        return self._budget.spent

    @property
    def langfuse_status(self) -> LangfuseStatus:
        return self._langfuse_status

    def flush(self) -> None:
        """Flush any pending observability data (e.g. queued Langfuse traces)."""
        flush_handler(self._langfuse)

    async def invoke_structured(
        self,
        schema: type[T],
        messages: list[BaseMessage],
        *,
        agent_name: str,
        prompt_version: str = "v1",
        metadata: dict[str, Any] | None = None,
        max_retries: int = 2,
        model_override: str | None = None,
    ) -> tuple[T, LLMResponse]:
        """Call the model and parse output into ``schema``. Retries on parse failure.

        ``model_override``, if given, swaps the chat model's ``model`` field
        for this call only. The base_url and API key are reused — this is
        the path used by eval judges to point at a stronger model than the
        translator without reconfiguring the rest of the pipeline.
        """
        # Try built-in structured output first, fall back to JSON mode + manual parse.
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                parsed, resp = await self._call_with_schema(
                    schema, messages, agent_name=agent_name,
                    prompt_version=prompt_version, metadata=metadata, attempt=attempt,
                    model_override=model_override,
                )
                return parsed, resp
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_err = exc
                self._events.event(
                    "agent.retry",
                    agent=agent_name,
                    attempt=attempt,
                    reason=type(exc).__name__,
                    detail=str(exc)[:200],
                )
                continue

        assert last_err is not None
        self._events.event(
            "agent.failed",
            agent=agent_name,
            reason=type(last_err).__name__,
            detail=str(last_err)[:200],
        )
        raise last_err

    def _chat_for(self, model_override: str | None) -> BaseChatModel:
        """Return a chat model bound to ``model_override`` or the default."""
        if model_override is None or model_override == self._config.model:
            return self._chat
        cached = self._chat_by_model.get(model_override)
        if cached is not None:
            return cached
        chat = ChatOpenAI(
            base_url=self._config.base_url,
            api_key=self._api_key,
            model=model_override,
            temperature=self._config.temperature,
            max_tokens=self._config.max_output_tokens,
            timeout=self._config.request_timeout_s,
            max_retries=0,
        )
        self._chat_by_model[model_override] = chat
        return chat

    async def _call_with_schema(
        self,
        schema: type[T],
        messages: list[BaseMessage],
        *,
        agent_name: str,
        prompt_version: str,
        metadata: dict[str, Any] | None,
        attempt: int,
        model_override: str | None = None,
    ) -> tuple[T, LLMResponse]:
        active_model = model_override or self._config.model
        chat = self._chat_for(model_override)
        # Pre-flight budget check (cheap estimate based on input length).
        est_in = self._estimate_tokens(messages)
        est_cost = estimate_cost_usd(
            active_model, tokens_in=est_in, tokens_out=self._config.max_output_tokens
        )
        self._budget.admit(est_cost)

        prompt_hash = self._prompt_hash(messages)

        # Use ``with_structured_output`` which selects best strategy for the endpoint.
        # ``method="json_mode"`` works on the broadest set of OpenAI-compatible endpoints.
        # Wrap with ``with_retry`` so transient infra errors (timeout / 5xx / rate-limit)
        # are retried with exponential backoff before bubbling up. Parse failures are
        # handled by the outer ``invoke_structured`` loop instead, on different criteria.
        structured = chat.with_structured_output(schema, method="json_mode").with_retry(
            retry_if_exception_type=_TRANSIENT_LLM_ERRORS,
            wait_exponential_jitter=True,
            stop_after_attempt=3,
        )
        # Inject Langfuse callback if available; otherwise empty list.
        callbacks = [self._langfuse] if self._langfuse is not None else []

        async with self._sem:
            t0 = time.perf_counter()
            try:
                result = await structured.ainvoke(
                    messages,
                    config={
                        "callbacks": callbacks,
                        "metadata": {
                            "agent": agent_name,
                            "prompt_version": prompt_version,
                            "prompt_hash": prompt_hash,
                            "attempt": attempt,
                            **(metadata or {}),
                        },
                        "tags": [agent_name],
                        "run_name": agent_name,
                    },
                )
            finally:
                latency_ms = int((time.perf_counter() - t0) * 1000)

        if not isinstance(result, schema):
            # with_structured_output may already validate; in case it returns dict, parse.
            result = schema.model_validate(result)

        # Token usage isn't returned by with_structured_output uniformly; fall back to estimate.
        tokens_in = est_in
        tokens_out = max(50, self._estimate_text_tokens(str(result.model_dump())))
        cost = estimate_cost_usd(
            active_model, tokens_in=tokens_in, tokens_out=tokens_out
        )
        self._budget.record(cost)
        self._metrics.record_llm_call(
            tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost
        )
        self._events.event(
            "agent.call",
            agent=agent_name,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            model=active_model,
            tokens={"input": tokens_in, "output": tokens_out},
            cost_usd=round(cost, 6),
            latency_ms=latency_ms,
            attempt=attempt,
            outcome="ok",
            **(metadata or {}),
        )
        resp = LLMResponse(
            text="",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            latency_ms=latency_ms,
            retries=attempt,
        )
        return result, resp

    @staticmethod
    def _prompt_hash(messages: list[BaseMessage]) -> str:
        payload = "\n---\n".join(
            f"{m.type}:{m.content if isinstance(m.content, str) else json.dumps(m.content)}"
            for m in messages
        )
        return hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]

    @staticmethod
    def _estimate_text_tokens(text: str) -> int:
        """Cheap token estimator: ~4 chars / token English, ~1.5 chars / token CJK."""
        if not text:
            return 0
        # Mixed heuristic.
        return max(1, len(text) // 3)

    def _estimate_tokens(self, messages: list[BaseMessage]) -> int:
        joined = ""
        for m in messages:
            content = m.content if isinstance(m.content, str) else json.dumps(m.content)
            joined += content + "\n"
        return self._estimate_text_tokens(joined)


def _ensure_api_key(env_name: str) -> str:
    key = os.environ.get(env_name, "")
    if not key:
        raise RuntimeError(
            f"LLM API key not set: please `export {env_name}=...` "
            f"(or change `llm.api_key_env` in your config)"
        )
    return key


def build_llm_router(
    *,
    config: RunConfig,
    events: EventLogger,
    metrics: MetricsAggregator,
) -> LLMRouter:
    api_key = _ensure_api_key(config.llm.api_key_env)
    handler, status = build_langfuse_handler(config.langfuse)
    events.event(
        "observability.langfuse",
        enabled=status.enabled,
        host=status.host,
        full_payload=status.full_payload,
        reason=status.reason,
    )
    budget = BudgetGate(config.cost.hard_cap_usd)
    sem = asyncio.Semaphore(config.llm.max_concurrency)
    return LLMRouter(
        config=config.llm,
        api_key=api_key,
        budget=budget,
        events=events,
        metrics=metrics,
        langfuse_handler=handler,
        langfuse_status=status,
        sem=sem,
    )


# Convenience helpers business modules use to build messages without importing langchain.
def system_message(text: str) -> BaseMessage:
    return SystemMessage(content=text)


def user_message(text: str) -> BaseMessage:
    return HumanMessage(content=text)
