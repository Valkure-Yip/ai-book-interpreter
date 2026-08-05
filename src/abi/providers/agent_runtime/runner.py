"""LangChain v1 Action harness with durable, thread-scoped checkpoints."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError

from abi.providers.agent_runtime.tooling import to_langchain_tool
from abi.providers.llm.budget import BudgetExceeded, BudgetGate
from abi.providers.llm.factory import _TRANSIENT_LLM_ERRORS
from abi.providers.llm.pricing import estimate_cost_usd
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.providers.observability.langfuse_client import LangfuseStatus
from abi.types.orchestration import (
    ActionOutcome,
    ActionOutcomeEnvelope,
    AgentRunResult,
    Indeterminate,
    Paused,
    PermanentFailure,
    RetryableFailure,
    ToolCallRecord,
)
from abi.types.run import LLMConfig
from abi.types.tools import ToolBinding


@dataclass(frozen=True, slots=True)
class AgentActionRequest:
    """One isolated Action invocation; callables remain outside persisted state."""

    system_prompt: str
    user_prompt: str
    tools: tuple[ToolBinding, ...]
    agent_name: str
    thread_id: str
    checkpoint_path: Path
    max_iterations: int
    may_have_side_effects: bool = False

    def __post_init__(self) -> None:
        if not self.thread_id:
            raise ValueError(
                "thread_id must be stable and non-empty; derive it from the Action attempt"
            )
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1; configure a bounded Action loop")


def _estimate_text_tokens(text: str) -> int:
    return max(1, len(text) // 3) if text else 0


class _CostCallback(BaseCallbackHandler):
    """Budget gate and structured accounting for every Action model call."""

    raise_error = True

    def __init__(
        self,
        *,
        model: str,
        budget: BudgetGate,
        events: EventLogger,
        metrics: MetricsAggregator,
        agent_name: str,
        max_output_tokens: int,
    ) -> None:
        self._model = model
        self._budget = budget
        self._events = events
        self._metrics = metrics
        self._agent = agent_name
        self._max_out = max_output_tokens
        self.llm_calls = 0
        self.cost_usd = 0.0

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[BaseMessage]], **kwargs: Any
    ) -> None:
        joined = "\n".join(str(message.content) for batch in messages for message in batch)
        estimated_input = _estimate_text_tokens(joined)
        estimated_cost = estimate_cost_usd(
            self._model,
            tokens_in=estimated_input,
            tokens_out=self._max_out,
        )
        self._budget.admit(estimated_cost)

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        tokens_in, tokens_out = self._extract_usage(response)
        cost = estimate_cost_usd(self._model, tokens_in=tokens_in, tokens_out=tokens_out)
        self._budget.record(cost)
        self.cost_usd += cost
        self.llm_calls += 1
        self._metrics.record_llm_call(tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost)
        self._events.event(
            "agent.call",
            agent=self._agent,
            model=self._model,
            tokens={"input": tokens_in, "output": tokens_out},
            cost_usd=round(cost, 6),
            outcome="ok",
        )

    def _extract_usage(self, response: Any) -> tuple[int, int]:
        output = getattr(response, "llm_output", None) or {}
        usage = output.get("token_usage") or output.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens", 0) or 0)
        tokens_out = int(usage.get("completion_tokens", 0) or 0)
        if tokens_in or tokens_out:
            return tokens_in, tokens_out
        for generation_list in getattr(response, "generations", ()):
            for generation in generation_list:
                message = getattr(generation, "message", None)
                metadata = getattr(message, "usage_metadata", None) if message else None
                if metadata:
                    return int(metadata.get("input_tokens", 0)), int(
                        metadata.get("output_tokens", 0)
                    )
                text = getattr(generation, "text", "") or ""
                if text:
                    return 0, _estimate_text_tokens(text)
        return 0, self._max_out // 4


class AgentRuntime:
    """Run bounded Action agents while sharing budget and observability services."""

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
        model: BaseChatModel | None = None,
    ) -> None:
        self._config = config
        self._api_key = api_key
        self._budget = budget
        self._events = events
        self._metrics = metrics
        self._langfuse = langfuse_handler
        self._langfuse_status = langfuse_status
        self._sem = sem
        self._injected_model = model
        self._model: BaseChatModel | None = None

    @property
    def budget(self) -> BudgetGate:
        return self._budget

    @property
    def langfuse_status(self) -> LangfuseStatus:
        return self._langfuse_status

    def _get_model(self) -> BaseChatModel:
        if self._injected_model is not None:
            return self._injected_model
        if self._model is None:
            self._model = ChatOpenAI(
                base_url=self._config.base_url,
                api_key=self._api_key,
                model=self._config.model,
                temperature=self._config.temperature,
                max_tokens=self._config.max_output_tokens,
                timeout=self._config.request_timeout_s,
                max_retries=0,
            )
        return self._model

    async def run_action(self, request: AgentActionRequest) -> AgentRunResult:
        """Execute or resume an Action and return only ABI-owned frozen models."""
        request.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        callback = _CostCallback(
            model=self._config.model,
            budget=self._budget,
            events=self._events,
            metrics=self._metrics,
            agent_name=request.agent_name,
            max_output_tokens=self._config.max_output_tokens,
        )
        callbacks: list[BaseCallbackHandler] = [callback]
        if self._langfuse is not None:
            callbacks.append(self._langfuse)
        config: RunnableConfig = {
            "configurable": {"thread_id": request.thread_id},
            "recursion_limit": request.max_iterations * 2 + 6,
            "callbacks": callbacks,
            "metadata": {"agent": request.agent_name},
            "tags": [request.agent_name],
            "run_name": request.agent_name,
        }
        self._events.event(
            "agent.run.start",
            agent=request.agent_name,
            thread_id=request.thread_id,
            max_iterations=request.max_iterations,
        )
        try:
            async with AsyncSqliteSaver.from_conn_string(
                str(request.checkpoint_path)
            ) as checkpointer:
                agent = create_agent(
                    model=self._get_model(),
                    tools=[to_langchain_tool(tool) for tool in request.tools],
                    system_prompt=request.system_prompt,
                    response_format=ActionOutcomeEnvelope,
                    checkpointer=checkpointer,
                    name=request.agent_name,
                )
                async with self._sem:
                    raw_result = cast(
                        dict[str, Any],
                        await agent.ainvoke(
                            cast(
                                Any,
                                {"messages": [{"role": "user", "content": request.user_prompt}]},
                            ),
                            config=config,
                        ),
                    )
            envelope = ActionOutcomeEnvelope.model_validate(raw_result["structured_response"])
            messages = tuple(raw_result.get("messages", ()))
            tool_log = self._tool_log(messages)
            result = AgentRunResult(
                outcome=envelope.outcome,
                llm_calls=callback.llm_calls,
                tool_calls=len(tool_log),
                cost_usd=callback.cost_usd,
                stopped_reason="paused" if envelope.outcome.kind == "paused" else "completed",
                tool_log=tool_log,
            )
        except GraphRecursionError as exc:
            result = AgentRunResult(
                outcome=RetryableFailure(
                    error_code="iteration_limit",
                    message=f"Action iteration limit reached: {exc}",
                ),
                llm_calls=callback.llm_calls,
                tool_calls=0,
                cost_usd=callback.cost_usd,
                stopped_reason="iteration_limit",
            )
        except BudgetExceeded as exc:
            result = AgentRunResult(
                outcome=Paused(reason="budget", message=str(exc)),
                llm_calls=callback.llm_calls,
                tool_calls=0,
                cost_usd=callback.cost_usd,
                stopped_reason="paused",
            )
        except TimeoutError as exc:
            outcome: ActionOutcome
            if request.may_have_side_effects:
                outcome = Indeterminate(
                    operation_key=request.thread_id,
                    message=f"Timed out after a possible side effect: {exc}",
                )
            else:
                outcome = RetryableFailure(
                    error_code="provider_timeout",
                    message=f"Transient provider timeout: {exc}",
                )
            result = AgentRunResult(
                outcome=outcome,
                llm_calls=callback.llm_calls,
                tool_calls=0,
                cost_usd=callback.cost_usd,
                stopped_reason="error",
            )
        except _TRANSIENT_LLM_ERRORS as exc:
            result = AgentRunResult(
                outcome=RetryableFailure(
                    error_code="transient_provider_error",
                    message=f"Transient provider failure: {exc}",
                ),
                llm_calls=callback.llm_calls,
                tool_calls=0,
                cost_usd=callback.cost_usd,
                stopped_reason="error",
            )
        except Exception as exc:
            result = AgentRunResult(
                outcome=PermanentFailure(
                    error_code="unclassified_exception",
                    message=f"Unclassified Action failure: {type(exc).__name__}: {exc}",
                ),
                llm_calls=callback.llm_calls,
                tool_calls=0,
                cost_usd=callback.cost_usd,
                stopped_reason="error",
            )
        self._events.event(
            "agent.run.end",
            agent=request.agent_name,
            thread_id=request.thread_id,
            outcome=result.outcome.kind,
            stopped_reason=result.stopped_reason,
            llm_calls=result.llm_calls,
            tool_calls=result.tool_calls,
            cost_usd=round(result.cost_usd, 6),
        )
        return result

    @staticmethod
    def _tool_log(messages: tuple[object, ...]) -> tuple[ToolCallRecord, ...]:
        records: list[ToolCallRecord] = []
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            for call in message.tool_calls:
                if call.get("name") == "ActionOutcomeEnvelope":
                    continue
                records.append(
                    ToolCallRecord(
                        name=str(call.get("name", "")),
                        arguments_json=json.dumps(
                            call.get("args", {}), ensure_ascii=False, sort_keys=True
                        ),
                    )
                )
        return tuple(records)
