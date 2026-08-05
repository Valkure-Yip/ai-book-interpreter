"""LangChain v1 Action harness with durable, thread-scoped checkpoints."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast
from uuid import UUID

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import EmptyInputError, GraphRecursionError
from langgraph.types import Command, Interrupt
from pydantic import Field, ValidationError

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
    RepairRequired,
    RetryableFailure,
    ToolCallRecord,
)
from abi.types.run import LLMConfig
from abi.types.tools import ToolBinding


class CheckpointResume(FrozenModel):
    """Resume the durable graph from its latest checkpoint without new input."""

    kind: Literal["checkpoint"] = "checkpoint"


class HitlDecision(FrozenModel):
    """One ordered human decision for a pending business tool call."""

    decision: Literal["approve", "reject"]
    feedback: str | None = None


class HitlResume(FrozenModel):
    """Typed ordered decisions for one multi-tool graph interrupt."""

    kind: Literal["hitl"] = "hitl"
    decisions: tuple[HitlDecision, ...] = Field(min_length=1)


AgentResume: TypeAlias = CheckpointResume | HitlResume


class _CheckpointActionRequest(FrozenModel):
    """Validated public HITL action payload stored in an interrupt."""

    name: str
    args: dict[str, object]
    description: str | None = None


class _CheckpointReviewConfig(FrozenModel):
    """Validated decision policy paired with one interrupted action."""

    action_name: str
    allowed_decisions: tuple[Literal["approve", "edit", "reject", "respond"], ...] = (
        Field(min_length=1)
    )
    args_schema: dict[str, object] | None = None


class _CheckpointHitlRequest(FrozenModel):
    """Current LangChain HITL request shape carried by ``Interrupt.value``."""

    action_requests: tuple[_CheckpointActionRequest, ...] = Field(min_length=1)
    review_configs: tuple[_CheckpointReviewConfig, ...] = Field(min_length=1)


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
        if len(tool_names) != len(self.tools):
            duplicates = sorted(
                name
                for name in tool_names
                if sum(tool.name == name for tool in self.tools) > 1
            )
            raise ValueError(f"duplicate tool names are forbidden: {', '.join(duplicates)}")
        unknown_approval_tools = set(self.approval_tools) - tool_names
        if unknown_approval_tools:
            unknown = ", ".join(sorted(unknown_approval_tools))
            raise ValueError(f"approval_tools must name bound tools; unknown: {unknown}")


def _estimate_text_tokens(text: str) -> int:
    return max(1, len(text) // 3) if text else 0


def _provider_error_classification(error: BaseException) -> str | None:
    """Return a provider classification, giving HTTP status the first vote."""
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        if status_code == 429 or 500 <= status_code <= 599:
            return "transient_provider_error"
        if 400 <= status_code <= 499:
            return "permanent_provider_error"
    if isinstance(error, TimeoutError):
        return "provider_timeout"
    if isinstance(error, _PERMANENT_LLM_ERRORS):
        return "permanent_provider_error"
    if isinstance(error, _TRANSIENT_LLM_ERRORS):
        return "transient_provider_error"
    return None


def _hitl_mismatch_counts(
    error: BaseException,
    resume: AgentResume | None,
) -> tuple[str, str] | None:
    if not isinstance(resume, HitlResume) or not isinstance(error, ValueError):
        return None
    mismatch = re.search(
        r"Number of human decisions \((\d+)\).*tool calls \((\d+)\)",
        str(error),
    )
    if mismatch is None:
        return None
    supplied, pending = mismatch.groups()
    return supplied, pending


def _runtime_error_classification(
    error: BaseException,
    *,
    resume: AgentResume | None = None,
) -> str:
    """Classify graph/model errors once, with provider evidence first."""
    provider_classification = _provider_error_classification(error)
    if provider_classification is not None:
        return provider_classification
    if isinstance(error, BudgetExceeded):
        return "budget"
    if isinstance(error, GraphRecursionError):
        return "iteration_limit"
    if _hitl_mismatch_counts(error, resume) is not None:
        return "hitl_decision_count_mismatch"
    if isinstance(error, EmptyInputError):
        return "checkpoint_not_resumable"
    return "unclassified_exception"


def _empty_result(outcome: ActionOutcome) -> AgentRunResult:
    """Build a control-plane result before any graph, model, or tool work."""
    return AgentRunResult(
        outcome=outcome,
        llm_calls=0,
        tool_calls=0,
        cost_usd=0.0,
        stopped_reason="error",
    )


def _checkpoint_not_resumable() -> AgentRunResult:
    return _empty_result(
        RepairRequired(
            defect_codes=("checkpoint_not_resumable",),
            message=(
                "This thread has no compatible checkpoint to resume. "
                "Start a fresh Action invocation instead."
            ),
        )
    )


def _checkpoint_read_failure(error: BaseException) -> AgentRunResult:
    """Classify checkpoint storage failures without provider inference or text."""
    raw_sqlite_code = getattr(error, "sqlite_errorcode", None)
    sqlite_code = raw_sqlite_code & 0xFF if isinstance(raw_sqlite_code, int) else None
    if isinstance(error, TimeoutError):
        return _empty_result(
            RetryableFailure(
                error_code="checkpoint_read_timeout",
                message="The checkpoint read timed out. Retry the Action continuation.",
            )
        )
    if sqlite_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return _empty_result(
            RetryableFailure(
                error_code="checkpoint_busy",
                message="The checkpoint store is busy. Retry the Action continuation.",
            )
        )
    if isinstance(error, PermissionError) or sqlite_code in {
        sqlite3.SQLITE_AUTH,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_PERM,
        sqlite3.SQLITE_READONLY,
    }:
        return _empty_result(
            RepairRequired(
                defect_codes=("checkpoint_permission_denied",),
                message=(
                    "The checkpoint store is not readable. Repair its permissions "
                    "before resuming."
                ),
            )
        )
    if isinstance(
        error,
        (sqlite3.DatabaseError, UnicodeError, ValueError, TypeError, KeyError),
    ):
        return _empty_result(
            RepairRequired(
                defect_codes=("checkpoint_corrupt",),
                message=(
                    "The checkpoint data is malformed or corrupt. Restore a valid "
                    "checkpoint before resuming."
                ),
            )
        )
    return _empty_result(
        RepairRequired(
            defect_codes=("checkpoint_read_failed",),
            message=(
                "The checkpoint could not be read safely. Repair the checkpoint store "
                "before resuming."
            ),
        )
    )


def _pending_hitl_actions(
    checkpoint_tuple: CheckpointTuple,
) -> tuple[tuple[str, frozenset[str]], ...] | None:
    """Parse current public pending writes into ordered HITL action policies."""
    pending: list[tuple[str, frozenset[str]]] = []
    for _task_id, channel, value in checkpoint_tuple.pending_writes or ():
        if channel != "__interrupt__":
            continue
        if not isinstance(value, (list, tuple)):
            return None
        for interrupt_value in value:
            if not isinstance(interrupt_value, Interrupt):
                return None
            try:
                request = _CheckpointHitlRequest.model_validate(interrupt_value.value)
            except ValidationError:
                return None
            if len(request.action_requests) != len(request.review_configs):
                return None
            for action, review in zip(
                request.action_requests, request.review_configs, strict=True
            ):
                if action.name != review.action_name:
                    return None
                pending.append((action.name, frozenset(review.allowed_decisions)))
    return tuple(pending) or None


def _validate_hitl_resume(
    request: AgentActionRequest,
    checkpoint_tuple: CheckpointTuple,
) -> AgentRunResult | None:
    """Fail closed unless this exact checkpoint has an allowed HITL interrupt."""
    if not isinstance(request.resume, HitlResume):
        return None
    pending = _pending_hitl_actions(checkpoint_tuple)
    if pending is None:
        return _checkpoint_not_resumable()
    if len(request.resume.decisions) != len(pending):
        return _empty_result(
            RepairRequired(
                defect_codes=("hitl_decision_count_mismatch",),
                message=(
                    f"Provided {len(request.resume.decisions)} HITL decisions for "
                    f"{len(pending)} pending tools. Provide one ordered approve/reject "
                    "decision per pending tool."
                ),
            )
        )
    allowed_tools = frozenset(request.approval_tools)
    if any(tool_name not in allowed_tools for tool_name, _decisions in pending):
        return _empty_result(
            RepairRequired(
                defect_codes=("hitl_tool_not_allowed",),
                message=(
                    "A pending HITL tool is no longer in this Action's approval allowlist. "
                    "Start a fresh authorized Action invocation."
                ),
            )
        )
    if any(
        decision.decision not in allowed_decisions
        for decision, (_tool_name, allowed_decisions) in zip(
            request.resume.decisions, pending, strict=True
        )
    ):
        return _empty_result(
            RepairRequired(
                defect_codes=("hitl_decision_not_allowed",),
                message=(
                    "A supplied HITL decision is not allowed by the pending review policy. "
                    "Provide an allowed ordered decision."
                ),
            )
        )
    return None


async def _read_resume_checkpoint(
    checkpoint_path: Path,
    config: RunnableConfig,
) -> CheckpointTuple | AgentRunResult:
    """Read an existing resume target in an isolated checkpoint boundary."""
    try:
        if not checkpoint_path.is_file():
            return _checkpoint_not_resumable()
    except OSError as error:
        return _checkpoint_read_failure(error)
    try:
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
            checkpoint_tuple = await saver.aget_tuple(config)
    except Exception as error:
        return _checkpoint_read_failure(error)
    if checkpoint_tuple is None:
        return _checkpoint_not_resumable()
    return checkpoint_tuple


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
        self.tool_log: list[ToolCallRecord] = []
        self._started_llm_runs: set[UUID] = set()
        self._finalized_llm_runs: set[UUID] = set()

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        if run_id in self._started_llm_runs:
            return
        self._started_llm_runs.add(run_id)
        self.llm_calls += 1
        joined = "\n".join(str(message.content) for batch in messages for message in batch)
        estimated_input = _estimate_text_tokens(joined)
        estimated_cost = estimate_cost_usd(
            self._model,
            tokens_in=estimated_input,
            tokens_out=self._max_out,
        )
        self._budget.admit(estimated_cost)

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        if run_id not in self._started_llm_runs or run_id in self._finalized_llm_runs:
            return
        self._finalized_llm_runs.add(run_id)
        tokens_in, tokens_out = self._extract_usage(response)
        cost = estimate_cost_usd(self._model, tokens_in=tokens_in, tokens_out=tokens_out)
        self._budget.record(cost)
        self.cost_usd += cost
        self._metrics.record_llm_call(tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost)
        self._events.event(
            "agent.call",
            agent=self._agent,
            model=self._model,
            tokens={"input": tokens_in, "output": tokens_out},
            cost_usd=round(cost, 6),
            outcome="ok",
        )

    def on_llm_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._record_failed_attempt(run_id, _runtime_error_classification(error))

    def record_pending_failure(self, classification: str) -> None:
        """Account for callback-start failures that bypass ``on_llm_error``."""
        for run_id in self._started_llm_runs - self._finalized_llm_runs:
            self._record_failed_attempt(run_id, classification)

    def _record_failed_attempt(self, run_id: UUID, classification: str) -> None:
        if run_id not in self._started_llm_runs or run_id in self._finalized_llm_runs:
            return
        self._finalized_llm_runs.add(run_id)
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

    def record_tool_start(
        self, binding: ToolBinding, arguments: dict[str, object]
    ) -> None:
        """Record a validated ABI handler immediately before it is invoked."""
        self.tool_log.append(
            ToolCallRecord(
                name=binding.name,
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

        def finish(result: AgentRunResult) -> AgentRunResult:
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

        if request.resume is not None:
            checkpoint_read = await _read_resume_checkpoint(
                request.checkpoint_path, config
            )
            if isinstance(checkpoint_read, AgentRunResult):
                return finish(checkpoint_read)
            hitl_failure = _validate_hitl_resume(request, checkpoint_read)
            if hitl_failure is not None:
                return finish(hitl_failure)
        else:
            try:
                request.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                return finish(_checkpoint_read_failure(error))

        try:
            async with AsyncSqliteSaver.from_conn_string(
                str(request.checkpoint_path)
            ) as checkpointer:
                agent = create_agent(
                    model=self._get_model(),
                    tools=[
                        to_langchain_tool(
                            tool, on_actual_start=callback.record_tool_start
                        )
                        for tool in request.tools
                    ],
                    system_prompt=request.system_prompt,
                    response_format=ActionOutcomeEnvelope,
                    checkpointer=checkpointer,
                    name=request.agent_name,
                    middleware=(
                        [
                            HumanInTheLoopMiddleware(
                                interrupt_on={
                                    name: True for name in request.approval_tools
                                }
                            )
                        ]
                        if request.approval_tools
                        else ()
                    ),
                )
                graph_input: Any
                if request.resume is None:
                    graph_input = {
                        "messages": [
                            {"role": "user", "content": request.user_prompt}
                        ]
                    }
                elif isinstance(request.resume, CheckpointResume):
                    graph_input = None
                else:
                    provider_decisions: list[dict[str, str]] = []
                    for item in request.resume.decisions:
                        decision: dict[str, str] = {"type": item.decision}
                        if item.decision == "reject" and item.feedback:
                            decision["message"] = item.feedback
                        provider_decisions.append(decision)
                    graph_input = Command(resume={"decisions": provider_decisions})
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
        except Exception as error:
            classification = _runtime_error_classification(
                error, resume=request.resume
            )
            callback.record_pending_failure(classification)
            failure: ActionOutcome
            stopped_reason: Literal["iteration_limit", "paused", "error"] = "error"
            if classification == "provider_timeout":
                if request.may_have_side_effects and callback.tool_log:
                    failure = Indeterminate(
                        operation_key=request.thread_id,
                        message=(
                            "The model provider timed out after a business tool started. "
                            "Reconcile the side effect before retrying."
                        ),
                    )
                else:
                    failure = RetryableFailure(
                        error_code=classification,
                        message=(
                            "The model provider timed out before completion. "
                            "Retry the Action."
                        ),
                    )
            elif classification == "transient_provider_error":
                failure = RetryableFailure(
                    error_code=classification,
                    message=(
                        "The model provider is temporarily unavailable. "
                        "Retry the Action."
                    ),
                )
            elif classification == "permanent_provider_error":
                failure = PermanentFailure(
                    error_code=classification,
                    message=(
                        "The model provider permanently rejected this Action request. "
                        "Repair the request before retrying."
                    ),
                )
            elif classification == "budget":
                failure = Paused(
                    reason="budget",
                    message=(
                        "The Action budget is exhausted. Increase the budget before "
                        "resuming."
                    ),
                )
                stopped_reason = "paused"
            elif classification == "iteration_limit":
                failure = RetryableFailure(
                    error_code="iteration_limit",
                    message=(
                        "The Action reached its iteration limit. Resume from the checkpoint "
                        "with a higher bounded limit."
                    ),
                )
                stopped_reason = "iteration_limit"
            elif classification == "hitl_decision_count_mismatch":
                counts = _hitl_mismatch_counts(error, request.resume)
                supplied, pending = counts if counts is not None else ("unknown", "unknown")
                failure = RepairRequired(
                    defect_codes=("hitl_decision_count_mismatch",),
                    message=(
                        f"Provided {supplied} HITL decisions for {pending} pending tools. "
                        "Provide one ordered approve/reject decision per pending tool."
                    ),
                )
            elif classification == "checkpoint_not_resumable":
                failure = RepairRequired(
                    defect_codes=("checkpoint_not_resumable",),
                    message=(
                        "This thread has no compatible checkpoint to resume. "
                        "Start a fresh Action invocation instead."
                    ),
                )
            else:
                failure = PermanentFailure(
                    error_code="unclassified_exception",
                    message="The Action failed with an unclassified internal error.",
                )
            result = AgentRunResult(
                outcome=failure,
                llm_calls=callback.llm_calls,
                tool_calls=len(callback.tool_log),
                cost_usd=callback.cost_usd,
                stopped_reason=stopped_reason,
                tool_log=tuple(callback.tool_log),
            )
        return finish(result)


def _set_model_for_testing(runtime: AgentRuntime, model: BaseChatModel) -> AgentRuntime:
    """Inject a fake provider model without expanding the public constructor."""
    runtime._model = model
    return runtime
