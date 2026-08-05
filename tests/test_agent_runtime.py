"""Behavior tests for the provider-owned Action harness."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import openai
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.errors import GraphRecursionError
from pydantic import Field

from abi.providers.llm.budget import BudgetExceeded, BudgetGate
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.providers.observability.langfuse_client import LangfuseStatus
from abi.types._base import FrozenModel
from abi.types.run import LLMConfig
from abi.types.tools import ToolBinding


def test_action_runtime_exposes_abi_owned_request_and_result_surface() -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")

    assert hasattr(runner, "AgentActionRequest")
    assert hasattr(runner.AgentRuntime, "run_action")


def test_agent_runtime_public_constructor_has_no_sdk_model_injection() -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")

    assert "model" not in inspect.signature(runner.AgentRuntime).parameters


class _HistoryAwareModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "history-aware-test-model"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        human_turns = sum(message.type == "human" for message in messages)
        if human_turns == 1:
            outcome: dict[str, Any] = {
                "kind": "paused",
                "reason": "hitl",
                "message": "resume this Action on the same durable thread",
            }
        else:
            outcome = {
                "kind": "succeeded",
                "staging_relpath": "state/staging/a1/1/result.json",
                "evidence_refs": ["prior_tool_result"],
            }
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "ActionOutcomeEnvelope",
                    "args": {"outcome": outcome},
                    "id": f"outcome-{human_turns}",
                    "type": "tool_call",
                }
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class _ExplodingModel(BaseChatModel):
    error: Exception

    @property
    def _llm_type(self) -> str:
        return "exploding-test-model"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise self.error


class _ValueErrorWithStatus(ValueError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _GraphRecursionErrorWithStatus(GraphRecursionError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _NoopInput(FrozenModel):
    """No arguments are accepted."""


class _DeliveryInput(FrozenModel):
    recipient: str


class _LoopingModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "looping-test-model"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = AIMessage(
            content="",
            tool_calls=[
                {"name": "noop", "args": {}, "id": f"loop-{len(messages)}", "type": "tool_call"}
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class _SideEffectModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "side-effect-test-model"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "deliver",
                    "args": {},
                    "id": "delivery-attempt",
                    "type": "tool_call",
                }
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _success_message(*, evidence: list[str] | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "ActionOutcomeEnvelope",
                "args": {
                    "outcome": {
                        "kind": "succeeded",
                        "staging_relpath": "state/staging/a1/1/result.json",
                        "evidence_refs": evidence or [],
                    }
                },
                "id": "outcome-success",
                "type": "tool_call",
            }
        ],
    )


class _RecordingOutcomeModel(BaseChatModel):
    human_counts: list[int] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "recording-outcome-test-model"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.human_counts.append(sum(message.type == "human" for message in messages))
        return ChatResult(generations=[ChatGeneration(message=_success_message())])


class _ResumeAfterToolModel(_RecordingOutcomeModel):
    fail_once: bool = True

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.human_counts.append(sum(message.type == "human" for message in messages))
        completed = any(isinstance(message, ToolMessage) and message.name == "once" for message in messages)
        if not completed:
            message = AIMessage(
                content="",
                tool_calls=[
                    {"name": "once", "args": {}, "id": "once-call", "type": "tool_call"}
                ],
            )
            return ChatResult(generations=[ChatGeneration(message=message)])
        if self.fail_once:
            self.fail_once = False
            raise ConnectionError("provider failed after the durable tool result")
        return ChatResult(
            generations=[ChatGeneration(message=_success_message(evidence=["prior_tool_result"]))]
        )


class _FiniteLoopModel(_RecordingOutcomeModel):
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.human_counts.append(sum(message.type == "human" for message in messages))
        completed = sum(
            isinstance(message, ToolMessage) and message.name == "noop" for message in messages
        )
        if completed >= 4:
            return ChatResult(generations=[ChatGeneration(message=_success_message())])
        message = AIMessage(
            content="",
            tool_calls=[
                {"name": "noop", "args": {}, "id": f"loop-{completed}", "type": "tool_call"}
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class _ApprovalModel(_RecordingOutcomeModel):
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.human_counts.append(sum(message.type == "human" for message in messages))
        if any(isinstance(message, ToolMessage) for message in messages):
            return ChatResult(generations=[ChatGeneration(message=_success_message())])
        message = AIMessage(
            content="",
            tool_calls=[
                {"name": "deliver", "args": {}, "id": "delivery-call", "type": "tool_call"}
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class _TwoApprovalModel(_RecordingOutcomeModel):
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.human_counts.append(sum(message.type == "human" for message in messages))
        if sum(isinstance(message, ToolMessage) for message in messages) >= 2:
            return ChatResult(generations=[ChatGeneration(message=_success_message())])
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "deliver_a",
                                "args": {},
                                "id": "delivery-a",
                                "type": "tool_call",
                            },
                            {
                                "name": "deliver_b",
                                "args": {},
                                "id": "delivery-b",
                                "type": "tool_call",
                            },
                        ],
                    )
                )
            ]
        )


class _InvalidToolThenTimeoutModel(_RecordingOutcomeModel):
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if any(isinstance(message, ToolMessage) for message in messages):
            raise TimeoutError("provider timeout after rejected tool arguments")
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "deliver",
                                "args": {"wrong": "invalid"},
                                "id": "invalid-delivery",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


def _runtime(tmp_path: Path, model: BaseChatModel, *, cap: float | None = None) -> Any:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    assert hasattr(runner, "AgentActionRequest")
    runtime = runner.AgentRuntime(
        config=LLMConfig(model="gpt-4o-mini"),
        api_key="test-key",
        budget=BudgetGate(cap),
        events=EventLogger(tmp_path / "events.jsonl", "run-1"),
        metrics=MetricsAggregator(tmp_path / "metrics.json", "run-1", "book-1"),
        langfuse_handler=None,
        langfuse_status=LangfuseStatus(False, False, "", "test"),
        sem=asyncio.Semaphore(1),
    )
    return runner._set_model_for_testing(runtime, model)


def _request(
    tmp_path: Path,
    *,
    side_effects: bool = False,
    thread_id: str = "run-1/a1/1",
    tools: tuple[ToolBinding, ...] = (),
    max_iterations: int = 2,
    resume: object | None = None,
    approval_tools: tuple[str, ...] = (),
    checkpoint_path: Path | None = None,
) -> Any:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    assert hasattr(runner, "AgentActionRequest")
    return runner.AgentActionRequest(
        system_prompt="Return a typed Action outcome.",
        user_prompt="Continue the Action.",
        tools=tools,
        agent_name="test-action",
        thread_id=thread_id,
        checkpoint_path=checkpoint_path or tmp_path / "graph-checkpoints.sqlite",
        max_iterations=max_iterations,
        may_have_side_effects=side_effects,
        resume=resume,
        approval_tools=approval_tools,
    )


@pytest.mark.asyncio
async def test_budget_pause_resumes_without_appending_a_human_message(tmp_path: Path) -> None:
    model = _RecordingOutcomeModel(human_counts=[])
    paused_runtime = _runtime(tmp_path, model, cap=0.0)
    resumed_runtime = _runtime(tmp_path, model)
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    resume = runner.CheckpointResume()

    first = await paused_runtime.run_action(_request(tmp_path))
    resumed = await resumed_runtime.run_action(_request(tmp_path, resume=resume))
    isolated = await resumed_runtime.run_action(_request(tmp_path, thread_id="run-1/a2/1"))

    assert first.outcome.kind == "paused"
    assert resumed.outcome.kind == "succeeded"
    assert isolated.outcome.kind == "succeeded"
    assert model.human_counts == [1, 1]
    assert (tmp_path / "graph-checkpoints.sqlite").is_file()


@pytest.mark.asyncio
async def test_provider_crash_resume_does_not_replay_completed_side_effect(
    tmp_path: Path,
) -> None:
    model = _ResumeAfterToolModel(human_counts=[])
    runtime = _runtime(tmp_path, model)
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    executions = 0

    def once() -> str:
        nonlocal executions
        executions += 1
        return "durable result"

    tool = ToolBinding("once", "Execute one idempotent side effect.", _NoopInput, once)
    first = await runtime.run_action(_request(tmp_path, tools=(tool,), side_effects=True))
    resumed = await runtime.run_action(
        _request(tmp_path, tools=(tool,), side_effects=True, resume=runner.CheckpointResume())
    )

    assert first.outcome.kind == "retryable_failure"
    assert resumed.outcome.kind == "succeeded"
    assert resumed.outcome.evidence_refs == ("prior_tool_result",)
    assert executions == 1
    assert set(model.human_counts) == {1}


@pytest.mark.asyncio
async def test_recursion_resume_keeps_one_human_turn_and_completed_tools(
    tmp_path: Path,
) -> None:
    model = _FiniteLoopModel(human_counts=[])
    runtime = _runtime(tmp_path, model)
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    executions = 0

    def noop() -> str:
        nonlocal executions
        executions += 1
        return "continue"

    tool = ToolBinding("noop", "Continue the bounded test loop.", _NoopInput, noop)
    first = await runtime.run_action(_request(tmp_path, tools=(tool,), max_iterations=1))
    resumed = await runtime.run_action(
        _request(
            tmp_path,
            tools=(tool,),
            max_iterations=1,
            resume=runner.CheckpointResume(),
        )
    )

    assert first.outcome.error_code == "iteration_limit"
    assert first.tool_calls == 4
    assert resumed.outcome.kind == "succeeded"
    assert executions == 4
    assert set(model.human_counts) == {1}


@pytest.mark.asyncio
async def test_hitl_approve_resumes_interrupt_without_new_human_or_replay(
    tmp_path: Path,
) -> None:
    model = _ApprovalModel(human_counts=[])
    runtime = _runtime(tmp_path, model)
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    executions = 0

    def deliver() -> str:
        nonlocal executions
        executions += 1
        return "delivered"

    tool = ToolBinding("deliver", "Deliver after approval.", _NoopInput, deliver)
    first = await runtime.run_action(
        _request(tmp_path, tools=(tool,), side_effects=True, approval_tools=("deliver",))
    )
    resumed = await runtime.run_action(
        _request(
            tmp_path,
            tools=(tool,),
            side_effects=True,
            approval_tools=("deliver",),
            resume=runner.HitlResume(
                decisions=(runner.HitlDecision(decision="approve"),)
            ),
        )
    )

    assert first.outcome.kind == "paused"
    assert first.outcome.reason == "hitl"
    assert resumed.outcome.kind == "succeeded"
    assert executions == 1
    assert set(model.human_counts) == {1}


@pytest.mark.asyncio
async def test_hitl_reject_resumes_without_executing_the_business_tool(
    tmp_path: Path,
) -> None:
    model = _ApprovalModel(human_counts=[])
    runtime = _runtime(tmp_path, model)
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    executions = 0

    def deliver() -> str:
        nonlocal executions
        executions += 1
        return "delivered"

    tool = ToolBinding("deliver", "Deliver after approval.", _NoopInput, deliver)
    first = await runtime.run_action(
        _request(tmp_path, tools=(tool,), side_effects=True, approval_tools=("deliver",))
    )
    rejected = await runtime.run_action(
        _request(
            tmp_path,
            tools=(tool,),
            side_effects=True,
            approval_tools=("deliver",),
            resume=runner.HitlResume(
                decisions=(
                    runner.HitlDecision(
                        decision="reject", feedback="not authorized"
                    ),
                )
            ),
        )
    )

    assert first.outcome.kind == "paused"
    assert rejected.outcome.kind == "succeeded"
    assert executions == 0
    assert set(model.human_counts) == {1}


def test_resume_boundary_rejects_untyped_dicts(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="typed"):
        _request(tmp_path, resume={"decision": "approve"})


def test_hitl_resume_rejects_unsupported_decisions() -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")

    with pytest.raises(ValueError):
        runner.HitlDecision(decision="edit")
    with pytest.raises(ValueError):
        runner.HitlResume(decisions=())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decisions", "expected_executions"),
    [
        (("approve", "approve"), ("deliver_a", "deliver_b")),
        (("reject", "reject"), ()),
        (("approve", "reject"), ("deliver_a",)),
    ],
)
async def test_hitl_resumes_multiple_tools_with_ordered_decisions(
    tmp_path: Path,
    decisions: tuple[str, str],
    expected_executions: tuple[str, ...],
) -> None:
    model = _TwoApprovalModel(human_counts=[])
    runtime = _runtime(tmp_path, model)
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    executions: list[str] = []

    def deliver_a() -> str:
        executions.append("deliver_a")
        return "a"

    def deliver_b() -> str:
        executions.append("deliver_b")
        return "b"

    tools = (
        ToolBinding("deliver_a", "First delivery.", _NoopInput, deliver_a),
        ToolBinding("deliver_b", "Second delivery.", _NoopInput, deliver_b),
    )
    request_args = {
        "tools": tools,
        "side_effects": True,
        "approval_tools": ("deliver_a", "deliver_b"),
    }

    paused = await runtime.run_action(_request(tmp_path, **request_args))
    resumed = await runtime.run_action(
        _request(
            tmp_path,
            **request_args,
            resume=runner.HitlResume(
                decisions=tuple(
                    runner.HitlDecision(decision=decision) for decision in decisions
                )
            ),
        )
    )

    assert paused.outcome.kind == "paused"
    assert resumed.outcome.kind == "succeeded"
    assert tuple(executions) == expected_executions
    assert set(model.human_counts) == {1}


@pytest.mark.asyncio
async def test_hitl_decision_count_mismatch_is_repair_required(tmp_path: Path) -> None:
    model = _TwoApprovalModel(human_counts=[])
    runtime = _runtime(tmp_path, model)
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    tools = (
        ToolBinding("deliver_a", "First delivery.", _NoopInput, lambda: "a"),
        ToolBinding("deliver_b", "Second delivery.", _NoopInput, lambda: "b"),
    )
    request_args = {
        "tools": tools,
        "side_effects": True,
        "approval_tools": ("deliver_a", "deliver_b"),
    }
    paused = await runtime.run_action(_request(tmp_path, **request_args))
    real_create_agent = runner.create_agent
    graph_creations = 0

    def tracked_create_agent(*args: Any, **kwargs: Any) -> Any:
        nonlocal graph_creations
        graph_creations += 1
        return real_create_agent(*args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(runner, "create_agent", tracked_create_agent)

        result = await runtime.run_action(
            _request(
                tmp_path,
                **request_args,
                resume=runner.HitlResume(
                    decisions=(runner.HitlDecision(decision="approve"),)
                ),
            )
        )

    assert paused.outcome.kind == "paused"
    assert result.outcome.kind == "repair_required"
    assert result.outcome.defect_codes == ("hitl_decision_count_mismatch",)
    assert "two" in result.outcome.message.lower() or "2" in result.outcome.message
    assert graph_creations == 0
    assert model.human_counts == [1]


@pytest.mark.asyncio
async def test_action_harness_maps_real_graph_recursion_limit(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _LoopingModel())
    noop = ToolBinding("noop", "Continue the test loop.", _NoopInput, lambda: "continue")

    result = await runtime.run_action(_request(tmp_path, tools=(noop,), max_iterations=1))

    assert result.outcome.kind == "retryable_failure"
    assert result.outcome.error_code == "iteration_limit"
    assert result.outcome.message == (
        "The Action reached its iteration limit. Resume from the checkpoint with a "
        "higher bounded limit."
    )
    assert result.stopped_reason == "iteration_limit"


@pytest.mark.asyncio
async def test_action_harness_maps_budget_callback_to_pause(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _HistoryAwareModel(), cap=0.0)

    result = await runtime.run_action(_request(tmp_path))

    assert result.outcome.kind == "paused"
    assert result.outcome.reason == "budget"
    assert result.outcome.message == (
        "The Action budget is exhausted. Increase the budget before resuming."
    )
    assert result.stopped_reason == "paused"


@pytest.mark.asyncio
async def test_action_harness_maps_tool_side_effect_timeout_to_indeterminate(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, _SideEffectModel())

    def deliver() -> str:
        raise TimeoutError("remote accepted request but response was lost")

    tool = ToolBinding("deliver", "Deliver one external side effect.", _NoopInput, deliver)
    result = await runtime.run_action(_request(tmp_path, tools=(tool,), side_effects=True))

    assert result.outcome.kind == "indeterminate"
    assert result.outcome.operation_key == "run-1/a1/1"
    assert result.outcome.message == (
        "The model provider timed out after a business tool started. Reconcile the "
        "side effect before retrying."
    )
    assert result.tool_calls == 1
    assert result.stopped_reason == "error"


@pytest.mark.asyncio
async def test_invalid_tool_arguments_do_not_count_as_side_effect_start(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, _InvalidToolThenTimeoutModel(human_counts=[]))
    executions = 0

    def deliver(recipient: str) -> str:
        nonlocal executions
        executions += 1
        return f"delivered to {recipient}"

    tool = ToolBinding("deliver", "Validated side effect.", _DeliveryInput, deliver)
    result = await runtime.run_action(
        _request(tmp_path, tools=(tool,), side_effects=True)
    )

    assert result.outcome.kind == "retryable_failure"
    assert result.outcome.error_code == "provider_timeout"
    assert result.outcome.message == (
        "The model provider timed out before completion. Retry the Action."
    )
    assert result.tool_calls == 0
    assert result.tool_log == ()
    assert executions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "side_effects", "kind", "error_code"),
    [
        (
            ConnectionError("connection-sensitive-payload"),
            False,
            "retryable_failure",
            "transient_provider_error",
        ),
        (
            TimeoutError("timeout-sensitive-payload-before-model"),
            False,
            "retryable_failure",
            "provider_timeout",
        ),
        (
            TimeoutError("timeout-sensitive-payload-before-tool"),
            True,
            "retryable_failure",
            "provider_timeout",
        ),
        (BudgetExceeded(0.0, 0.0, 0.1), False, "paused", None),
        (
            RuntimeError("unknown-sensitive-payload"),
            False,
            "permanent_failure",
            "unclassified_exception",
        ),
    ],
)
async def test_action_harness_classifies_failures_without_throwing(
    tmp_path: Path,
    error: Exception,
    side_effects: bool,
    kind: str,
    error_code: str | None,
) -> None:
    runtime = _runtime(tmp_path, _ExplodingModel(error=error))

    result = await runtime.run_action(_request(tmp_path, side_effects=side_effects))

    assert result.outcome.kind == kind
    if error_code is not None:
        assert result.outcome.error_code == error_code
    assert str(error) not in result.outcome.message
    assert result.stopped_reason in {"paused", "error"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "status_code"),
    [
        (openai.BadRequestError, 400),
        (openai.AuthenticationError, 401),
        (openai.PermissionDeniedError, 403),
        (openai.NotFoundError, 404),
    ],
)
async def test_openai_4xx_is_permanent_and_failed_call_is_observable_without_secrets(
    tmp_path: Path,
    error_type: type[Exception],
    status_code: int,
) -> None:
    secret = "sk-secret-must-not-appear"
    response = httpx.Response(
        status_code,
        request=httpx.Request("POST", "https://provider.invalid/v1/chat"),
    )
    error = error_type(secret, response=response, body={"api_key": secret})
    runtime = _runtime(tmp_path, _ExplodingModel(error=error))

    result = await runtime.run_action(_request(tmp_path))

    assert result.outcome.kind == "permanent_failure"
    assert result.outcome.error_code == "permanent_provider_error"
    assert result.llm_calls == 1
    assert runtime._metrics.snapshot()["llm_calls"] == 1
    event_text = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in event_text.splitlines()]
    failed_calls = [event for event in events if event["event"] == "agent.call"]
    assert failed_calls[-1]["outcome"] == "error"
    assert failed_calls[-1]["error_classification"] == "permanent_provider_error"
    assert secret not in event_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "kind", "error_code"),
    [
        (400, "permanent_failure", "permanent_provider_error"),
        (401, "permanent_failure", "permanent_provider_error"),
        (403, "permanent_failure", "permanent_provider_error"),
        (404, "permanent_failure", "permanent_provider_error"),
        (429, "retryable_failure", "transient_provider_error"),
        (500, "retryable_failure", "transient_provider_error"),
        (502, "retryable_failure", "transient_provider_error"),
        (503, "retryable_failure", "transient_provider_error"),
        (504, "retryable_failure", "transient_provider_error"),
    ],
)
async def test_api_status_errors_are_classified_by_http_status_without_body_leakage(
    tmp_path: Path,
    status_code: int,
    kind: str,
    error_code: str,
) -> None:
    secret = f"provider-body-secret-{status_code}"
    response = httpx.Response(
        status_code,
        request=httpx.Request("POST", "https://provider.invalid/v1/chat"),
    )
    error = openai.APIStatusError(secret, response=response, body={"secret": secret})
    runtime = _runtime(tmp_path, _ExplodingModel(error=error))

    result = await runtime.run_action(_request(tmp_path))

    assert result.outcome.kind == kind
    assert result.outcome.error_code == error_code
    assert result.llm_calls == 1
    assert secret not in (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert secret not in result.outcome.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "status_code", "kind", "error_code"),
    [
        (
            openai.BadRequestError,
            500,
            "retryable_failure",
            "transient_provider_error",
        ),
        (
            openai.RateLimitError,
            400,
            "permanent_failure",
            "permanent_provider_error",
        ),
    ],
)
async def test_http_status_precedes_openai_exception_subclass_for_outcome_and_event(
    tmp_path: Path,
    error_type: type[Exception],
    status_code: int,
    kind: str,
    error_code: str,
) -> None:
    secret = f"contradictory-secret-{status_code}"
    response = httpx.Response(
        status_code,
        request=httpx.Request("POST", "https://provider.invalid/v1/chat"),
    )
    error = error_type(secret, response=response, body={"secret": secret})
    runtime = _runtime(tmp_path, _ExplodingModel(error=error))

    result = await runtime.run_action(_request(tmp_path))

    assert result.outcome.kind == kind
    assert result.outcome.error_code == error_code
    event_text = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in event_text.splitlines()]
    failed_calls = [event for event in events if event["event"] == "agent.call"]
    assert failed_calls[-1]["error_classification"] == error_code
    assert secret not in event_text
    assert secret not in result.outcome.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        _ValueErrorWithStatus("value-error-secret", status_code=503),
        _GraphRecursionErrorWithStatus("recursion-error-secret", status_code=503),
    ],
)
async def test_provider_status_precedes_control_exception_types_and_matches_event(
    tmp_path: Path,
    error: Exception,
) -> None:
    runtime = _runtime(tmp_path, _ExplodingModel(error=error))

    result = await runtime.run_action(_request(tmp_path))

    assert result.outcome.kind == "retryable_failure"
    assert result.outcome.error_code == "transient_provider_error"
    assert result.outcome.message == (
        "The model provider is temporarily unavailable. Retry the Action."
    )
    event_text = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in event_text.splitlines()]
    failed_calls = [event for event in events if event["event"] == "agent.call"]
    assert failed_calls[-1]["error_classification"] == result.outcome.error_code
    assert str(error) not in event_text
    assert str(error) not in result.outcome.message


def test_action_request_rejects_duplicate_tool_names_before_checkpoint_work(
    tmp_path: Path,
) -> None:
    executions = 0

    def handler() -> str:
        nonlocal executions
        executions += 1
        return "ok"

    tool = ToolBinding("duplicate", "Duplicate binding.", _NoopInput, handler)

    with pytest.raises(ValueError, match="duplicate tool"):
        _request(tmp_path, tools=(tool, tool))

    assert executions == 0
    assert not (tmp_path / "graph-checkpoints.sqlite").exists()


def test_llm_callback_finalizes_each_run_id_only_once(tmp_path: Path) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    events = EventLogger(tmp_path / "events.jsonl", "run-1")
    metrics = MetricsAggregator(tmp_path / "metrics.json", "run-1", "book-1")
    callback = runner._CostCallback(
        model="gpt-4o-mini",
        budget=BudgetGate(None),
        events=events,
        metrics=metrics,
        agent_name="idempotency-test",
        max_output_tokens=128,
    )
    run_id = uuid4()

    callback.on_chat_model_start(
        {}, [[HumanMessage(content="one attempt")]], run_id=run_id
    )
    callback.on_llm_error(ConnectionError("first delivery"), run_id=run_id)
    callback.on_llm_error(ConnectionError("duplicate delivery"), run_id=run_id)

    assert callback.llm_calls == 1
    assert metrics.snapshot()["llm_calls"] == 1
    records = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len([record for record in records if record["event"] == "agent.call"]) == 1


@pytest.mark.asyncio
async def test_checkpoint_resume_on_new_thread_returns_repair_instruction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    model = _RecordingOutcomeModel(human_counts=[])
    runtime = _runtime(tmp_path, model)
    real_create_agent = runner.create_agent
    graph_creations = 0

    def tracked_create_agent(*args: Any, **kwargs: Any) -> Any:
        nonlocal graph_creations
        graph_creations += 1
        return real_create_agent(*args, **kwargs)

    monkeypatch.setattr(runner, "create_agent", tracked_create_agent)

    result = await runtime.run_action(
        _request(tmp_path, resume=runner.CheckpointResume())
    )

    assert result.outcome.kind == "repair_required"
    assert result.outcome.defect_codes == ("checkpoint_not_resumable",)
    assert "fresh" in result.outcome.message.lower()
    assert model.human_counts == []
    assert result.llm_calls == 0
    assert result.tool_calls == 0
    assert graph_creations == 0
    assert not (tmp_path / "graph-checkpoints.sqlite").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_side_effect_tool", [False, True])
async def test_missing_hitl_resume_is_rejected_before_graph_model_or_tool_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_side_effect_tool: bool,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    real_create_agent = runner.create_agent
    graph_creations = 0
    handler_calls = 0

    def tracked_create_agent(*args: Any, **kwargs: Any) -> Any:
        nonlocal graph_creations
        graph_creations += 1
        return real_create_agent(*args, **kwargs)

    def deliver() -> str:
        nonlocal handler_calls
        handler_calls += 1
        raise TimeoutError("must not execute")

    monkeypatch.setattr(runner, "create_agent", tracked_create_agent)
    model: BaseChatModel
    tools: tuple[ToolBinding, ...]
    approval_tools: tuple[str, ...]
    if with_side_effect_tool:
        model = _SideEffectModel()
        tools = (ToolBinding("deliver", "Side effect.", _NoopInput, deliver),)
        approval_tools = ("deliver",)
    else:
        model = _ExplodingModel(error=AssertionError("model must not execute"))
        tools = ()
        approval_tools = ()
    runtime = _runtime(tmp_path, model)
    resume = runner.HitlResume(
        decisions=(runner.HitlDecision(decision="approve"),)
    )

    result = await runtime.run_action(
        _request(
            tmp_path,
            tools=tools,
            approval_tools=approval_tools,
            side_effects=with_side_effect_tool,
            resume=resume,
        )
    )

    assert result.outcome.kind == "repair_required"
    assert result.outcome.defect_codes == ("checkpoint_not_resumable",)
    assert result.llm_calls == 0
    assert result.tool_calls == 0
    assert result.tool_log == ()
    assert handler_calls == 0
    assert graph_creations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_state", ["completed", "budget"])
async def test_hitl_resume_rejects_non_hitl_checkpoint_before_graph_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint_state: str,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    model = _RecordingOutcomeModel(human_counts=[])
    first_runtime = _runtime(
        tmp_path,
        model,
        cap=0.0 if checkpoint_state == "budget" else None,
    )
    first = await first_runtime.run_action(_request(tmp_path))
    runtime = _runtime(tmp_path, model)
    real_create_agent = runner.create_agent
    graph_creations = 0

    def tracked_create_agent(*args: Any, **kwargs: Any) -> Any:
        nonlocal graph_creations
        graph_creations += 1
        return real_create_agent(*args, **kwargs)

    monkeypatch.setattr(runner, "create_agent", tracked_create_agent)

    result = await runtime.run_action(
        _request(
            tmp_path,
            resume=runner.HitlResume(
                decisions=(runner.HitlDecision(decision="approve"),)
            ),
        )
    )

    assert first.outcome.kind == ("paused" if checkpoint_state == "budget" else "succeeded")
    assert result.outcome.kind == "repair_required"
    assert result.outcome.defect_codes == ("checkpoint_not_resumable",)
    assert result.llm_calls == 0
    assert result.tool_calls == 0
    assert result.tool_log == ()
    assert graph_creations == 0
    assert model.human_counts == ([] if checkpoint_state == "budget" else [1])


@pytest.mark.asyncio
async def test_hitl_resume_rejects_pending_tool_removed_from_approval_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    model = _ApprovalModel(human_counts=[])
    runtime = _runtime(tmp_path, model)
    handler_calls = 0

    def deliver() -> str:
        nonlocal handler_calls
        handler_calls += 1
        return "delivered"

    tool = ToolBinding("deliver", "Deliver after approval.", _NoopInput, deliver)
    paused = await runtime.run_action(
        _request(tmp_path, tools=(tool,), approval_tools=("deliver",))
    )
    real_create_agent = runner.create_agent
    graph_creations = 0

    def tracked_create_agent(*args: Any, **kwargs: Any) -> Any:
        nonlocal graph_creations
        graph_creations += 1
        return real_create_agent(*args, **kwargs)

    monkeypatch.setattr(runner, "create_agent", tracked_create_agent)

    result = await runtime.run_action(
        _request(
            tmp_path,
            tools=(tool,),
            approval_tools=(),
            resume=runner.HitlResume(
                decisions=(runner.HitlDecision(decision="approve"),)
            ),
        )
    )

    assert paused.outcome.kind == "paused"
    assert result.outcome.kind == "repair_required"
    assert result.outcome.defect_codes == ("hitl_tool_not_allowed",)
    assert result.llm_calls == 0
    assert result.tool_calls == 0
    assert handler_calls == 0
    assert graph_creations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_kind", ["checkpoint", "hitl"])
async def test_missing_resume_database_does_not_create_parent_or_connect_saver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resume_kind: str,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    runtime = _runtime(tmp_path, _ExplodingModel(error=AssertionError("model must not run")))
    checkpoint_path = tmp_path / "absent-checkpoints" / "state" / "graph.sqlite"
    missing_parent = tmp_path / "absent-checkpoints"
    real_from_conn_string = runner.AsyncSqliteSaver.from_conn_string
    saver_connections = 0

    def tracked_from_conn_string(path: str) -> Any:
        nonlocal saver_connections
        saver_connections += 1
        return real_from_conn_string(path)

    monkeypatch.setattr(
        runner.AsyncSqliteSaver, "from_conn_string", tracked_from_conn_string
    )
    resume: object
    if resume_kind == "checkpoint":
        resume = runner.CheckpointResume()
    else:
        resume = runner.HitlResume(
            decisions=(runner.HitlDecision(decision="approve"),)
        )

    result = await runtime.run_action(
        _request(tmp_path, checkpoint_path=checkpoint_path, resume=resume)
    )

    assert result.outcome.kind == "repair_required"
    assert result.outcome.defect_codes == ("checkpoint_not_resumable",)
    assert result.llm_calls == 0
    assert result.tool_calls == 0
    assert saver_connections == 0
    assert not checkpoint_path.exists()
    assert not missing_parent.exists()


@pytest.mark.asyncio
async def test_fresh_invocation_may_create_checkpoint_parent_and_database(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, _RecordingOutcomeModel(human_counts=[]))
    checkpoint_path = tmp_path / "fresh-checkpoints" / "state" / "graph.sqlite"

    result = await runtime.run_action(
        _request(tmp_path, checkpoint_path=checkpoint_path)
    )

    assert result.outcome.kind == "succeeded"
    assert checkpoint_path.is_file()


def _sqlite_read_error(
    error_type: type[sqlite3.Error], sqlite_errorcode: int, secret: str
) -> sqlite3.Error:
    error = error_type(secret)
    error.sqlite_errorcode = sqlite_errorcode
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "kind", "checkpoint_code"),
    [
        (
            _sqlite_read_error(
                sqlite3.OperationalError, sqlite3.SQLITE_BUSY, "locked-secret"
            ),
            "retryable_failure",
            "checkpoint_busy",
        ),
        (
            _sqlite_read_error(
                sqlite3.OperationalError, sqlite3.SQLITE_LOCKED, "busy-secret"
            ),
            "retryable_failure",
            "checkpoint_busy",
        ),
        (
            TimeoutError("checkpoint-timeout-secret"),
            "retryable_failure",
            "checkpoint_read_timeout",
        ),
        (
            _sqlite_read_error(
                sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT, "corrupt-secret"
            ),
            "repair_required",
            "checkpoint_corrupt",
        ),
        (
            _sqlite_read_error(
                sqlite3.DatabaseError, sqlite3.SQLITE_NOTADB, "malformed-secret"
            ),
            "repair_required",
            "checkpoint_corrupt",
        ),
        (
            PermissionError("checkpoint-permission-secret"),
            "repair_required",
            "checkpoint_permission_denied",
        ),
    ],
)
async def test_checkpoint_read_failures_are_control_results_without_graph_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    kind: str,
    checkpoint_code: str,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    checkpoint_path = tmp_path / "graph-checkpoints.sqlite"
    checkpoint_path.touch()
    model = _ExplodingModel(error=AssertionError("model must not run"))
    runtime = _runtime(tmp_path, model)
    real_create_agent = runner.create_agent
    graph_creations = 0

    async def failing_get_tuple(self: Any, config: Any) -> Any:
        raise error

    def tracked_create_agent(*args: Any, **kwargs: Any) -> Any:
        nonlocal graph_creations
        graph_creations += 1
        return real_create_agent(*args, **kwargs)

    monkeypatch.setattr(runner.AsyncSqliteSaver, "aget_tuple", failing_get_tuple)
    monkeypatch.setattr(runner, "create_agent", tracked_create_agent)

    result = await runtime.run_action(
        _request(
            tmp_path,
            checkpoint_path=checkpoint_path,
            resume=runner.CheckpointResume(),
        )
    )

    assert result.outcome.kind == kind
    if kind == "retryable_failure":
        assert result.outcome.error_code == checkpoint_code
    else:
        assert result.outcome.defect_codes == (checkpoint_code,)
    assert result.llm_calls == 0
    assert result.tool_calls == 0
    assert result.tool_log == ()
    assert graph_creations == 0
    event_text = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert str(error) not in result.outcome.message
    assert str(error) not in event_text


@pytest.mark.asyncio
async def test_completed_checkpoint_resume_returns_cached_success_without_new_work(
    tmp_path: Path,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    model = _RecordingOutcomeModel(human_counts=[])
    runtime = _runtime(tmp_path, model)

    first = await runtime.run_action(_request(tmp_path))
    resumed = await runtime.run_action(
        _request(tmp_path, resume=runner.CheckpointResume())
    )

    assert first.outcome.kind == "succeeded"
    assert resumed.outcome == first.outcome
    assert resumed.llm_calls == 0
    assert resumed.tool_calls == 0
    assert model.human_counts == [1]


def test_architecture_linter_reports_forbidden_sdk_import(tmp_path: Path) -> None:
    linter_path = Path("tools/lint/architecture.py")
    assert linter_path.exists(), "architecture linter must be executable repository code"

    spec = importlib.util.spec_from_file_location("architecture_linter", linter_path)
    assert spec is not None and spec.loader is not None
    module_under_test = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module_under_test
    spec.loader.exec_module(module_under_test)

    module = tmp_path / "src/abi/tools/bad.py"
    module.parent.mkdir(parents=True)
    module.write_text("from langchain.tools import tool\n", encoding="utf-8")

    violations = module_under_test.scan_tree(tmp_path / "src/abi")

    assert [(item.rule, item.path, item.line) for item in violations] == [
        ("sdk-import-outside-providers", "tools/bad.py", 1)
    ]


def test_architecture_linter_matches_only_exact_sdk_package_names(tmp_path: Path) -> None:
    linter_path = Path("tools/lint/architecture.py")
    spec = importlib.util.spec_from_file_location("architecture_linter_exact", linter_path)
    assert spec is not None and spec.loader is not None
    module_under_test = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module_under_test
    spec.loader.exec_module(module_under_test)
    module = tmp_path / "src/abi/tools/allowed.py"
    module.parent.mkdir(parents=True)
    module.write_text("import langchainish\n", encoding="utf-8")

    assert module_under_test.scan_tree(tmp_path / "src/abi") == ()


def test_architecture_linter_cli_rejects_violation_with_repair_instruction(
    tmp_path: Path,
) -> None:
    module = tmp_path / "src/abi/tools/bad.py"
    module.parent.mkdir(parents=True)
    module.write_text("import langgraph\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "tools/lint/architecture.py",
            str(tmp_path / "src/abi"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "tools/bad.py:1: sdk-import-outside-providers" in completed.stderr
    assert "Move the SDK import" in completed.stderr


def test_repository_has_no_sdk_imports_outside_providers() -> None:
    linter_path = Path("tools/lint/architecture.py")
    spec = importlib.util.spec_from_file_location("repository_architecture_linter", linter_path)
    assert spec is not None and spec.loader is not None
    module_under_test = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module_under_test
    spec.loader.exec_module(module_under_test)

    assert module_under_test.scan_tree(Path("src/abi")) == ()
