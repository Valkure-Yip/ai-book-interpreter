"""LangChain v1 Action harness with durable, thread-scoped checkpoints."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.types import Command

from abi.providers.agent_runtime.tooling import to_langchain_tool
from abi.providers.llm.budget import BudgetExceeded, BudgetGate
from abi.providers.llm.factory import _PERMANENT_LLM_ERRORS, _TRANSIENT_LLM_ERRORS
from abi.providers.llm.pricing import estimate_cost_usd
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.providers.observability.langfuse_client import LangfuseStatus
from abi.types._base import FrozenModel
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


class CheckpointResume(FrozenModel):
    """Resume the durable graph from its latest checkpoint without new input."""

    kind: Literal["checkpoint"] = "checkpoint"


class HitlResume(FrozenModel):
    """Typed human decision for a graph paused before a business tool call."""

    kind: Literal["hitl"] = "hitl"
    decision: Literal["approve", "reject"]
    feedback: str | None = None


AgentResume: TypeAlias = CheckpointResume | HitlResume


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
    resume: AgentResume | None = None
    approval_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.thread_id:
            raise ValueError(
                "thread_id must be stable and non-empty; derive it from the Action attempt"
            )
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1; configure a bounded Action loop")
        if self.resume is not None and not isinstance(
            self.resume, (CheckpointResume, HitlResume)
        ):
            raise TypeError("resume must be a typed CheckpointResume or HitlResume")
        tool_names = {tool.name for tool in self.tools}
        unknown_approval_tools = set(self.approval_tools) - tool_names
        if unknown_approval_tools:
            unknown = ", ".join(sorted(unknown_approval_tools))
            raise ValueError(f"approval_tools must name bound tools; unknown: {unknown}")


def _estimate_text_tokens(text: str) -> int:
    return max(1, len(text) // 3) if text else 0


def _provider_error_classification(error: BaseException) -> str:
    if isinstance(error, BudgetExceeded):
        return "budget"
    if isinstance(error, TimeoutError):
        return "provider_timeout"
    if isinstance(error, _PERMANENT_LLM_ERRORS):
        return "permanent_provider_error"
    if isinstance(error, _TRANSIENT_LLM_ERRORS):
        return "transient_provider_error"
    return "unclassified_exception"


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
        tool_names: frozenset[str],
    ) -> None:
        self._model = model
        self._budget = budget
        self._events = events
        self._metrics = metrics
        self._agent = agent_name
        self._max_out = max_output_tokens
        self._tool_names = tool_names
        self.llm_calls = 0
        self.cost_usd = 0.0
        self.tool_log: list[ToolCallRecord] = []
        self._open_llm_attempts = 0

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[BaseMessage]], **kwargs: Any
    ) -> None:
        self.llm_calls += 1
        self._open_llm_attempts += 1
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
        self._open_llm_attempts = max(0, self._open_llm_attempts - 1)
        self._metrics.record_llm_call(tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost)
        self._events.event(
            "agent.call",
            agent=self._agent,
            model=self._model,
            tokens={"input": tokens_in, "output": tokens_out},
            cost_usd=round(cost, 6),
            outcome="ok",
        )

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self._record_failed_attempt(_provider_error_classification(error))

    def record_pending_failure(self, classification: str) -> None:
        """Account for callback-start failures that bypass ``on_llm_error``."""
        while self._open_llm_attempts:
            self._record_failed_attempt(classification)

    def _record_failed_attempt(self, classification: str) -> None:
        if self._open_llm_attempts:
            self._open_llm_attempts -= 1
        self._metrics.record_llm_call(tokens_in=0, tokens_out=0, cost_usd=0.0)
        self._events.event(
            "agent.call",
            agent=self._agent,
            model=self._model,
            tokens={"input": 0, "output": 0},
            cost_usd=0.0,
            outcome="error",
            error_classification=classification,
        )

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        name = str(serialized.get("name", ""))
        if name not in self._tool_names:
            return
        arguments: object = inputs
        if arguments is None:
            try:
                arguments = json.loads(input_str)
            except (TypeError, json.JSONDecodeError):
                arguments = input_str
        self.tool_log.append(
            ToolCallRecord(
                name=name,
                arguments_json=json.dumps(arguments, ensure_ascii=False, sort_keys=True),
            )
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
    ) -> None:
        self._config = config
        self._api_key = api_key
        self._budget = budget
        self._events = events
        self._metrics = metrics
        self._langfuse = langfuse_handler
        self._langfuse_status = langfuse_status
        self._sem = sem
        self._model: BaseChatModel | None = None

    @property
    def budget(self) -> BudgetGate:
        return self._budget

    @property
    def langfuse_status(self) -> LangfuseStatus:
        return self._langfuse_status

    def _get_model(self) -> BaseChatModel:
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
            tool_names=frozenset(tool.name for tool in request.tools),
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
                    middleware=(
                        [
                            HumanInTheLoopMiddleware(
                                interrupt_on={name: True for name in request.approval_tools}
                            )
                        ]
                        if request.approval_tools
                        else ()
                    ),
                )
                graph_input: Any
                if request.resume is None:
                    graph_input = {
                        "messages": [{"role": "user", "content": request.user_prompt}]
                    }
                elif isinstance(request.resume, CheckpointResume):
                    graph_input = None
                else:
                    decision: dict[str, str] = {"type": request.resume.decision}
                    if request.resume.decision == "reject" and request.resume.feedback:
                        decision["message"] = request.resume.feedback
                    graph_input = Command(resume={"decisions": [decision]})
                async with self._sem:
                    raw_result = cast(
                        dict[str, Any],
                        await agent.ainvoke(graph_input, config=config),
                    )
            if raw_result.get("__interrupt__"):
                result = AgentRunResult(
                    outcome=Paused(
                        reason="hitl",
                        message="Action paused for approval before a tool call.",
                    ),
                    llm_calls=callback.llm_calls,
                    tool_calls=0,
                    cost_usd=callback.cost_usd,
                    stopped_reason="paused",
                )
            else:
                envelope = ActionOutcomeEnvelope.model_validate(
                    raw_result["structured_response"]
                )
                tool_log = tuple(callback.tool_log)
                result = AgentRunResult(
                    outcome=envelope.outcome,
                    llm_calls=callback.llm_calls,
                    tool_calls=len(tool_log),
                    cost_usd=callback.cost_usd,
                    stopped_reason=(
                        "paused" if envelope.outcome.kind == "paused" else "completed"
                    ),
                    tool_log=tool_log,
                )
        except GraphRecursionError as exc:
            callback.record_pending_failure("iteration_limit")
            result = AgentRunResult(
                outcome=RetryableFailure(
                    error_code="iteration_limit",
                    message=f"Action iteration limit reached: {exc}",
                ),
                llm_calls=callback.llm_calls,
                tool_calls=len(callback.tool_log),
                cost_usd=callback.cost_usd,
                stopped_reason="iteration_limit",
                tool_log=tuple(callback.tool_log),
            )
        except BudgetExceeded as exc:
            callback.record_pending_failure("budget")
            result = AgentRunResult(
                outcome=Paused(reason="budget", message=str(exc)),
                llm_calls=callback.llm_calls,
                tool_calls=len(callback.tool_log),
                cost_usd=callback.cost_usd,
                stopped_reason="paused",
                tool_log=tuple(callback.tool_log),
            )
        except TimeoutError as exc:
            callback.record_pending_failure("provider_timeout")
            outcome: ActionOutcome
            if request.may_have_side_effects and callback.tool_log:
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
                tool_calls=len(callback.tool_log),
                cost_usd=callback.cost_usd,
                stopped_reason="error",
                tool_log=tuple(callback.tool_log),
            )
        except _TRANSIENT_LLM_ERRORS as exc:
            callback.record_pending_failure("transient_provider_error")
            result = AgentRunResult(
                outcome=RetryableFailure(
                    error_code="transient_provider_error",
                    message=f"Transient provider failure: {type(exc).__name__}",
                ),
                llm_calls=callback.llm_calls,
                tool_calls=len(callback.tool_log),
                cost_usd=callback.cost_usd,
                stopped_reason="error",
                tool_log=tuple(callback.tool_log),
            )
        except _PERMANENT_LLM_ERRORS as exc:
            callback.record_pending_failure("permanent_provider_error")
            result = AgentRunResult(
                outcome=PermanentFailure(
                    error_code="permanent_provider_error",
                    message=f"Permanent provider failure: {type(exc).__name__}",
                ),
                llm_calls=callback.llm_calls,
                tool_calls=len(callback.tool_log),
                cost_usd=callback.cost_usd,
                stopped_reason="error",
                tool_log=tuple(callback.tool_log),
            )
        except Exception as exc:
            callback.record_pending_failure("unclassified_exception")
            result = AgentRunResult(
                outcome=PermanentFailure(
                    error_code="unclassified_exception",
                    message=f"Unclassified Action failure: {type(exc).__name__}",
                ),
                llm_calls=callback.llm_calls,
                tool_calls=len(callback.tool_log),
                cost_usd=callback.cost_usd,
                stopped_reason="error",
                tool_log=tuple(callback.tool_log),
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


def _set_model_for_testing(runtime: AgentRuntime, model: BaseChatModel) -> AgentRuntime:
    """Inject a fake provider model without expanding the public constructor."""
    runtime._model = model
    return runtime
