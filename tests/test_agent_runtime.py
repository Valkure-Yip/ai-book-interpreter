"""Behavior tests for the provider-owned Action harness."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import operator
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from uuid import uuid4

import httpx
import openai
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt, PregelTask, StateSnapshot, interrupt
from pydantic import Field
from typing_extensions import TypedDict

from abi.actions.builtins import build_action_registry
from abi.actions.contracts import ActionExecutionContext
from abi.project import ScaffoldRequest, scaffold_project
from abi.providers.llm.budget import BudgetExceeded, BudgetGate
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.providers.observability.langfuse_client import LangfuseStatus
from abi.tools.context import ToolContext
from abi.types._base import FrozenModel
from abi.types.orchestration import Paused, RunSnapshot, RunStatus
from abi.types.run import LLMConfig
from abi.types.tools import ToolBinding


def _succeeded_envelope_payload(
    *, evidence_refs: list[str] | None = None, leaf: str = "result.json"
) -> dict[str, Any]:
    action_id = "provider-result"
    return {
        "action_id": action_id,
        "attempt": 1,
        "outcome": {
            "kind": "succeeded",
            "artifact_bundle": {
                "action_id": action_id,
                "attempt": 1,
                "entries": [
                    {
                        "staged_relpath": f"state/staging/{action_id}/1/{leaf}",
                        "canonical_relpath": leaf,
                        "media_type": "application/json",
                        "evidence_role": "provider_result",
                    }
                ],
            },
            "evidence_refs": evidence_refs or [],
        },
    }


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
            envelope_args: dict[str, Any] = {
                "action_id": "provider-result",
                "attempt": 1,
                "outcome": {
                    "kind": "paused",
                    "reason": "hitl",
                    "message": "resume this Action on the same durable thread",
                },
            }
        else:
            envelope_args = _succeeded_envelope_payload(evidence_refs=["prior_tool_result"])
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "ActionOutcomeEnvelope",
                    "args": envelope_args,
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


class _SequentialInterruptState(TypedDict, total=False):
    messages: list[object]
    structured_response: object


class _ParallelInterruptState(TypedDict, total=False):
    messages: list[object]
    effects: Annotated[list[str], operator.add]
    structured_response: object


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
                "args": {**_succeeded_envelope_payload(evidence_refs=evidence)},
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


class _LabeledOutcomeModel(_RecordingOutcomeModel):
    label: str

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.human_counts.append(sum(message.type == "human" for message in messages))
        return ChatResult(
            generations=[ChatGeneration(message=_success_message(evidence=[self.label]))]
        )


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
        completed = any(
            isinstance(message, ToolMessage) and message.name == "once" for message in messages
        )
        if not completed:
            message = AIMessage(
                content="",
                tool_calls=[{"name": "once", "args": {}, "id": "once-call", "type": "tool_call"}],
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


class _FinalManifestApprovalModel(_RecordingOutcomeModel):
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
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "write_file",
                                "args": {
                                    "path": "output/final_manifest.md",
                                    "content": "# Final evidence\n",
                                },
                                "id": "final-manifest-write",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


class _SequentialApprovalModel(_RecordingOutcomeModel):
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.human_counts.append(sum(message.type == "human" for message in messages))
        completed_tools = {message.name for message in messages if isinstance(message, ToolMessage)}
        if "deliver_a" not in completed_tools:
            tool_name = "deliver_a"
        elif "deliver_b" not in completed_tools:
            tool_name = "deliver_b"
        else:
            return ChatResult(generations=[ChatGeneration(message=_success_message())])
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": tool_name,
                                "args": {},
                                "id": f"{tool_name}-call",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


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


async def _pending_interrupt_ids(checkpoint_path: Path, thread_id: str) -> tuple[str, ...]:
    config = {"configurable": {"thread_id": thread_id}}
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        checkpoint = await saver.aget_tuple(config)
    assert checkpoint is not None
    return tuple(
        interrupt.id
        for _task_id, channel, value in checkpoint.pending_writes or ()
        if channel == "__interrupt__"
        for interrupt in value
        if isinstance(interrupt, Interrupt)
    )


def _hitl_interrupt(
    interrupt_id: str,
    *tool_names: str,
) -> Interrupt:
    return Interrupt(
        value={
            "action_requests": [{"name": tool_name, "args": {}} for tool_name in tool_names],
            "review_configs": [
                {
                    "action_name": tool_name,
                    "allowed_decisions": ["approve", "reject"],
                }
                for tool_name in tool_names
            ],
        },
        id=interrupt_id,
    )


def _state_snapshot(*tasks: PregelTask) -> StateSnapshot:
    return StateSnapshot(
        values={},
        next=tuple(task.name for task in tasks),
        config={"configurable": {"thread_id": "thread-1"}},
        metadata={},
        created_at=None,
        parent_config=None,
        tasks=tasks,
        interrupts=tuple(interrupt_value for task in tasks for interrupt_value in task.interrupts),
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
    assert first.outcome.kind == "paused"
    assert first.outcome.reason == "hitl"
    (pending_interrupt,) = first.outcome.pending_hitl_interrupts
    assert pending_interrupt.interrupt_id
    assert [
        (
            review.tool_name,
            review.arguments_json,
            review.allowed_decisions,
        )
        for review in pending_interrupt.action_reviews
    ] == [
        (
            "deliver",
            "{}",
            ("approve", "reject"),
        )
    ]
    assert tuple(
        runner.HitlDecision(decision=decision).decision
        for decision in pending_interrupt.action_reviews[0].allowed_decisions
    ) == ("approve", "reject")
    serialized = first.model_dump(mode="json")
    assert serialized["outcome"]["pending_hitl_interrupts"] == [
        {
            "interrupt_id": pending_interrupt.interrupt_id,
            "action_reviews": [
                {
                    "tool_name": "deliver",
                    "arguments_json": "{}",
                    "description": pending_interrupt.action_reviews[0].description,
                    "allowed_decisions": ["approve", "reject"],
                }
            ],
        }
    ]
    resumed = await runtime.run_action(
        _request(
            tmp_path,
            tools=(tool,),
            side_effects=True,
            approval_tools=("deliver",),
            resume=runner.HitlResume(
                interrupts=(
                    runner.HitlInterruptDecision(
                        interrupt_id=pending_interrupt.interrupt_id,
                        decisions=(runner.HitlDecision(decision="approve"),),
                    ),
                )
            ),
        )
    )

    assert resumed.outcome.kind == "succeeded"
    assert executions == 1
    assert set(model.human_counts) == {1}


@pytest.mark.asyncio
async def test_default_output_finalize_reaches_real_hitl_from_registry_policy(
    tmp_path: Path,
) -> None:
    """Catch default catalog Actions that discard their explicit approval policy."""
    project = scaffold_project(
        ScaffoldRequest(
            target_root=tmp_path,
            book_slug="hitl-default",
            source_lang="en",
            target_lang="zh-hans",
            source_target="en-zh-hans",
        ),
        root=tmp_path / "project",
    )
    runtime = _runtime(tmp_path, _FinalManifestApprovalModel(human_counts=[]))
    services = SimpleNamespace(agent=runtime)
    registry = build_action_registry(
        tool_context=ToolContext(
            project=project,
            services=services,  # type: ignore[arg-type]
            run_id="run-1",
            get_run_snapshot=lambda: RunSnapshot(
                run_id="run-1", status=RunStatus.RUNNING
            ),
        )
    )
    resolved = registry.resolve_json("output.finalize", "{}")
    assert resolved.definition.spec.approval_tools == ("write_file",)

    envelope = await resolved.definition.executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            snapshot=RunSnapshot(run_id="run-1", status=RunStatus.RUNNING),
            action_id="finalize-action",
            attempt=1,
            source_lang="en",
            target_lang="zh-hans",
            source_target="en-zh-hans",
            book_slug="hitl-default",
        ),
        resolved.parameters,
    )

    assert isinstance(envelope.outcome, Paused)
    (pending,) = envelope.outcome.pending_hitl_interrupts
    assert [review.tool_name for review in pending.action_reviews] == ["write_file"]
    assert not (project.root / "state/staging/finalize-action/1/output/final_manifest.md").exists()


@pytest.mark.asyncio
async def test_hitl_checkpoint_inspection_never_blindly_reexecutes_approved_tool(
    tmp_path: Path,
) -> None:
    """Catch CLAIMED recovery that invokes the graph instead of reading public state."""
    model = _ApprovalModel(human_counts=[])
    runtime = _runtime(tmp_path, model)
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    executions = 0

    def deliver() -> str:
        nonlocal executions
        executions += 1
        return "delivered"

    tool = ToolBinding("deliver", "Deliver after approval.", _NoopInput, deliver)
    first_request = _request(
        tmp_path,
        tools=(tool,),
        side_effects=True,
        approval_tools=("deliver",),
    )
    first = await runtime.run_action(first_request)
    (pending,) = first.outcome.pending_hitl_interrupts
    resume_request = _request(
        tmp_path,
        tools=(tool,),
        side_effects=True,
        approval_tools=("deliver",),
        resume=runner.HitlResume(
            interrupts=(
                runner.HitlInterruptDecision(
                    interrupt_id=pending.interrupt_id,
                    decisions=(runner.HitlDecision(decision="approve"),),
                ),
            )
        ),
    )

    before = await runtime.inspect_hitl_checkpoint(resume_request)
    assert before.disposition == "not_started"
    assert executions == 0

    resumed = await runtime.run_action(resume_request)
    after = await runtime.inspect_hitl_checkpoint(resume_request)

    assert resumed.outcome.kind == "succeeded"
    assert after.disposition == "outcome"
    assert after.outcome == resumed.outcome
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
    (interrupt_id,) = await _pending_interrupt_ids(
        tmp_path / "graph-checkpoints.sqlite", "run-1/a1/1"
    )
    rejected = await runtime.run_action(
        _request(
            tmp_path,
            tools=(tool,),
            side_effects=True,
            approval_tools=("deliver",),
            resume=runner.HitlResume(
                interrupts=(
                    runner.HitlInterruptDecision(
                        interrupt_id=interrupt_id,
                        decisions=(
                            runner.HitlDecision(decision="reject", feedback="not authorized"),
                        ),
                    ),
                )
            ),
        )
    )

    assert first.outcome.kind == "paused"
    assert rejected.outcome.kind == "succeeded"
    assert executions == 0
    assert set(model.human_counts) == {1}


@pytest.mark.asyncio
async def test_same_action_resumes_two_sequential_public_hitl_interrupts(
    tmp_path: Path,
) -> None:
    model = _SequentialApprovalModel(human_counts=[])
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
        ToolBinding("deliver_a", "First approved delivery.", _NoopInput, deliver_a),
        ToolBinding("deliver_b", "Second approved delivery.", _NoopInput, deliver_b),
    )
    request_args = {
        "tools": tools,
        "side_effects": True,
        "approval_tools": ("deliver_a", "deliver_b"),
    }

    first = await runtime.run_action(_request(tmp_path, **request_args))
    assert first.outcome.kind == "paused"
    assert first.tool_calls == 0
    assert first.tool_log == ()
    (first_interrupt,) = first.outcome.pending_hitl_interrupts
    assert [review.tool_name for review in first_interrupt.action_reviews] == ["deliver_a"]

    second = await runtime.run_action(
        _request(
            tmp_path,
            **request_args,
            resume=runner.HitlResume(
                interrupts=(
                    runner.HitlInterruptDecision(
                        interrupt_id=first_interrupt.interrupt_id,
                        decisions=(runner.HitlDecision(decision="approve"),),
                    ),
                )
            ),
        )
    )
    assert second.outcome.kind == "paused"
    assert second.tool_calls == 1
    assert [(record.name, record.arguments_json) for record in second.tool_log] == [
        ("deliver_a", "{}")
    ]
    assert executions == ["deliver_a"]
    (second_interrupt,) = second.outcome.pending_hitl_interrupts
    assert [review.tool_name for review in second_interrupt.action_reviews] == ["deliver_b"]
    completed = await runtime.run_action(
        _request(
            tmp_path,
            **request_args,
            resume=runner.HitlResume(
                interrupts=(
                    runner.HitlInterruptDecision(
                        interrupt_id=second_interrupt.interrupt_id,
                        decisions=(runner.HitlDecision(decision="approve"),),
                    ),
                )
            ),
        )
    )

    assert completed.outcome.kind == "succeeded"
    assert completed.tool_calls == 1
    assert [(record.name, record.arguments_json) for record in completed.tool_log] == [
        ("deliver_b", "{}")
    ]
    assert executions == ["deliver_a", "deliver_b"]
    assert set(model.human_counts) == {1}


@pytest.mark.asyncio
async def test_same_node_resumes_second_interrupt_despite_historical_resume_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    runtime = _runtime(tmp_path, _RecordingOutcomeModel(human_counts=[]))
    effects: list[str] = []

    def approval_node(_state: _SequentialInterruptState) -> _SequentialInterruptState:
        interrupt(_hitl_interrupt("ignored-provider-id", "deliver_a").value)
        if "deliver_a" not in effects:
            effects.append("deliver_a")
        interrupt(_hitl_interrupt("ignored-provider-id", "deliver_b").value)
        if "deliver_b" not in effects:
            effects.append("deliver_b")
        return {
            "structured_response": _succeeded_envelope_payload(
                evidence_refs=["two-approved-effects"]
            )
        }

    def real_node_agent(*args: Any, **kwargs: Any) -> Any:
        return (
            StateGraph(_SequentialInterruptState)
            .add_node("approval", approval_node)
            .add_edge(START, "approval")
            .add_edge("approval", END)
            .compile(checkpointer=kwargs["checkpointer"])
        )

    monkeypatch.setattr(runner, "create_agent", real_node_agent)
    tools = (
        ToolBinding("deliver_a", "First approved effect.", _NoopInput, lambda: "a"),
        ToolBinding("deliver_b", "Second approved effect.", _NoopInput, lambda: "b"),
    )
    request_args = {
        "tools": tools,
        "side_effects": True,
        "approval_tools": ("deliver_a", "deliver_b"),
    }

    first = await runtime.run_action(_request(tmp_path, **request_args))
    assert first.outcome.kind == "paused"
    (first_interrupt,) = first.outcome.pending_hitl_interrupts
    second = await runtime.run_action(
        _request(
            tmp_path,
            **request_args,
            resume=runner.HitlResume(
                interrupts=(
                    runner.HitlInterruptDecision(
                        interrupt_id=first_interrupt.interrupt_id,
                        decisions=(runner.HitlDecision(decision="approve"),),
                    ),
                )
            ),
        )
    )
    assert second.outcome.kind == "paused"
    (second_interrupt,) = second.outcome.pending_hitl_interrupts
    assert [review.tool_name for review in second_interrupt.action_reviews] == ["deliver_b"]

    completed = await runtime.run_action(
        _request(
            tmp_path,
            **request_args,
            resume=runner.HitlResume(
                interrupts=(
                    runner.HitlInterruptDecision(
                        interrupt_id=second_interrupt.interrupt_id,
                        decisions=(runner.HitlDecision(decision="approve"),),
                    ),
                )
            ),
        )
    )

    assert completed.outcome.kind == "succeeded"
    assert completed.outcome.evidence_refs == ("two-approved-effects",)
    assert effects == ["deliver_a", "deliver_b"]


def test_resume_boundary_rejects_untyped_dicts(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="typed"):
        _request(tmp_path, resume={"decision": "approve"})


def test_hitl_resume_rejects_unsupported_decisions() -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")

    with pytest.raises(ValueError):
        runner.HitlDecision(decision="edit")
    with pytest.raises(ValueError):
        runner.HitlInterruptDecision(interrupt_id="", decisions=())
    with pytest.raises(ValueError):
        runner.HitlResume(interrupts=())
    with pytest.raises(ValueError, match="unique"):
        runner.HitlResume(
            interrupts=(
                runner.HitlInterruptDecision(
                    interrupt_id="same",
                    decisions=(runner.HitlDecision(decision="approve"),),
                ),
                runner.HitlInterruptDecision(
                    interrupt_id="same",
                    decisions=(runner.HitlDecision(decision="reject"),),
                ),
            )
        )


def test_public_hitl_policy_intersects_unexpected_provider_decisions_or_repairs() -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")

    def provider_interrupt(interrupt_id: str, decisions: list[str]) -> Interrupt:
        return Interrupt(
            value={
                "action_requests": [{"name": "deliver", "args": {}}],
                "review_configs": [
                    {
                        "action_name": "deliver",
                        "allowed_decisions": decisions,
                    }
                ],
            },
            id=interrupt_id,
        )

    mixed = runner._parse_pending_hitl_interrupt(
        provider_interrupt("mixed", ["edit", "approve", "respond", "reject"]),
        task_id="task-a",
    )
    assert mixed is not None
    assert mixed.actions[0].allowed_decisions == ("approve", "reject")
    assert runner._public_pending_hitl_interrupts((mixed,))[0].action_reviews[
        0
    ].allowed_decisions == ("approve", "reject")

    unsupported_only = runner._parse_pending_hitl_interrupt(
        provider_interrupt("unsupported", ["edit", "respond"]),
        task_id="task-b",
    )
    assert unsupported_only is None
    with pytest.raises(ValueError):
        runner.PendingHitlActionReview(
            tool_name="deliver",
            arguments_json="{}",
            allowed_decisions=("edit",),
        )


@pytest.mark.asyncio
async def test_real_parallel_partial_resume_excludes_completed_task_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    checkpoint_path = tmp_path / "parallel-partial.sqlite"
    thread_id = "run-1/parallel/1"

    def approval_node(tool_name: str) -> Any:
        def node(_state: _ParallelInterruptState) -> dict[str, list[str]]:
            interrupt(_hitl_interrupt("provider-generated", tool_name).value)
            return {"effects": [tool_name]}

        return node

    def finish(state: _ParallelInterruptState) -> dict[str, object]:
        return {
            "structured_response": _succeeded_envelope_payload(
                evidence_refs=state["effects"], leaf="parallel/result.json"
            )
        }

    def build_graph(checkpointer: Any) -> Any:
        return (
            StateGraph(_ParallelInterruptState)
            .add_node("approval_a", approval_node("deliver_a"))
            .add_node("approval_b", approval_node("deliver_b"))
            .add_node("finish", finish)
            .add_edge(START, "approval_a")
            .add_edge(START, "approval_b")
            .add_edge("approval_a", "finish")
            .add_edge("approval_b", "finish")
            .add_edge("finish", END)
            .compile(checkpointer=checkpointer)
        )

    def real_parallel_agent(*args: Any, **kwargs: Any) -> Any:
        return build_graph(kwargs["checkpointer"])

    monkeypatch.setattr(runner, "create_agent", real_parallel_agent)
    tools = (
        ToolBinding("deliver_a", "First delivery.", _NoopInput, lambda: "a"),
        ToolBinding("deliver_b", "Second delivery.", _NoopInput, lambda: "b"),
    )
    request_args = {
        "checkpoint_path": checkpoint_path,
        "thread_id": thread_id,
        "tools": tools,
        "approval_tools": ("deliver_a", "deliver_b"),
    }
    runtime = _runtime(tmp_path, _RecordingOutcomeModel(human_counts=[]))

    first = await runtime.run_action(_request(tmp_path, **request_args))
    assert first.outcome.kind == "paused"
    public_by_tool = {
        pending.action_reviews[0].tool_name: pending.interrupt_id
        for pending in first.outcome.pending_hitl_interrupts
    }
    assert set(public_by_tool) == {"deliver_a", "deliver_b"}

    config = {"configurable": {"thread_id": thread_id}}
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        graph = build_graph(saver)
        partial = await graph.ainvoke(
            Command(resume={public_by_tool["deliver_a"]: {"decisions": [{"type": "approve"}]}}),
            config=config,
        )
        snapshot = await graph.aget_state(config)

    assert partial.get("__interrupt__")
    completed_tasks = [task for task in snapshot.tasks if task.result is not None]
    assert len(completed_tasks) == 1
    assert public_by_tool["deliver_a"] in {
        interrupt_value.id for interrupt_value in completed_tasks[0].interrupts
    }
    unfinished_ids = {
        interrupt_value.id
        for task in snapshot.tasks
        if task.result is None and task.error is None
        for interrupt_value in task.interrupts
    }
    assert unfinished_ids == {public_by_tool["deliver_b"]}

    completed = await runtime.run_action(
        _request(
            tmp_path,
            **request_args,
            resume=runner.HitlResume(
                interrupts=(
                    runner.HitlInterruptDecision(
                        interrupt_id=public_by_tool["deliver_b"],
                        decisions=(runner.HitlDecision(decision="approve"),),
                    ),
                )
            ),
        )
    )

    assert completed.outcome.kind == "succeeded"
    assert set(completed.outcome.evidence_refs) == {"deliver_a", "deliver_b"}


def test_hitl_resume_requires_exact_pending_interrupt_id_set(tmp_path: Path) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    tools = (
        ToolBinding("deliver_a", "First delivery.", _NoopInput, lambda: "a"),
        ToolBinding("deliver_b", "Second delivery.", _NoopInput, lambda: "b"),
    )
    snapshot = _state_snapshot(
        PregelTask(
            id="task-a",
            name="approval",
            path=(),
            interrupts=(_hitl_interrupt("interrupt-a", "deliver_a"),),
        ),
        PregelTask(
            id="task-b",
            name="approval",
            path=(),
            interrupts=(_hitl_interrupt("interrupt-b", "deliver_b"),),
        ),
    )
    one_group = runner.HitlInterruptDecision(
        interrupt_id="interrupt-a",
        decisions=(runner.HitlDecision(decision="approve"),),
    )
    two_groups = (
        one_group,
        runner.HitlInterruptDecision(
            interrupt_id="interrupt-b",
            decisions=(runner.HitlDecision(decision="reject"),),
        ),
    )

    missing = runner._validate_hitl_resume(
        _request(
            tmp_path,
            tools=tools,
            approval_tools=("deliver_a", "deliver_b"),
            resume=runner.HitlResume(interrupts=(one_group,)),
        ),
        snapshot,
    )
    exact = runner._validate_hitl_resume(
        _request(
            tmp_path,
            tools=tools,
            approval_tools=("deliver_a", "deliver_b"),
            resume=runner.HitlResume(interrupts=two_groups),
        ),
        snapshot,
    )

    assert missing is not None
    assert missing.outcome.kind == "repair_required"
    assert missing.outcome.defect_codes == ("hitl_interrupt_id_mismatch",)
    assert exact is None


def test_hitl_resume_maps_single_and_multiple_interrupts_by_id() -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    resume = runner.HitlResume(
        interrupts=(
            runner.HitlInterruptDecision(
                interrupt_id="interrupt-a",
                decisions=(runner.HitlDecision(decision="approve"),),
            ),
            runner.HitlInterruptDecision(
                interrupt_id="interrupt-b",
                decisions=(runner.HitlDecision(decision="reject", feedback="not authorized"),),
            ),
        )
    )

    command = runner._hitl_resume_command(resume)

    assert command.resume == {
        "interrupt-a": {"decisions": [{"type": "approve"}]},
        "interrupt-b": {"decisions": [{"type": "reject", "message": "not authorized"}]},
    }


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
    (interrupt_id,) = await _pending_interrupt_ids(
        tmp_path / "graph-checkpoints.sqlite", "run-1/a1/1"
    )
    resumed = await runtime.run_action(
        _request(
            tmp_path,
            **request_args,
            resume=runner.HitlResume(
                interrupts=(
                    runner.HitlInterruptDecision(
                        interrupt_id=interrupt_id,
                        decisions=tuple(
                            runner.HitlDecision(decision=decision) for decision in decisions
                        ),
                    ),
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
    (interrupt_id,) = await _pending_interrupt_ids(
        tmp_path / "graph-checkpoints.sqlite", "run-1/a1/1"
    )
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
                    interrupts=(
                        runner.HitlInterruptDecision(
                            interrupt_id=interrupt_id,
                            decisions=(runner.HitlDecision(decision="approve"),),
                        ),
                    )
                ),
            )
        )

    assert paused.outcome.kind == "paused"
    assert result.outcome.kind == "repair_required"
    assert result.outcome.defect_codes == ("hitl_decision_count_mismatch",)
    assert "two" in result.outcome.message.lower() or "2" in result.outcome.message
    assert graph_creations == 1
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
    result = await runtime.run_action(_request(tmp_path, tools=(tool,), side_effects=True))

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

    callback.on_chat_model_start({}, [[HumanMessage(content="one attempt")]], run_id=run_id)
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

    result = await runtime.run_action(_request(tmp_path, resume=runner.CheckpointResume()))

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
        interrupts=(
            runner.HitlInterruptDecision(
                interrupt_id="missing-interrupt",
                decisions=(runner.HitlDecision(decision="approve"),),
            ),
        )
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
async def test_hitl_resume_rejects_non_hitl_checkpoint_before_model_or_tool_work(
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
                interrupts=(
                    runner.HitlInterruptDecision(
                        interrupt_id="not-a-hitl-interrupt",
                        decisions=(runner.HitlDecision(decision="approve"),),
                    ),
                )
            ),
        )
    )

    assert first.outcome.kind == ("paused" if checkpoint_state == "budget" else "succeeded")
    assert result.outcome.kind == "repair_required"
    assert result.outcome.defect_codes == ("checkpoint_not_resumable",)
    assert result.llm_calls == 0
    assert result.tool_calls == 0
    assert result.tool_log == ()
    assert graph_creations == 1
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
    (interrupt_id,) = await _pending_interrupt_ids(
        tmp_path / "graph-checkpoints.sqlite", "run-1/a1/1"
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
                interrupts=(
                    runner.HitlInterruptDecision(
                        interrupt_id=interrupt_id,
                        decisions=(runner.HitlDecision(decision="approve"),),
                    ),
                )
            ),
        )
    )

    assert paused.outcome.kind == "paused"
    assert result.outcome.kind == "repair_required"
    assert result.outcome.defect_codes == ("hitl_tool_not_allowed",)
    assert result.llm_calls == 0
    assert result.tool_calls == 0
    assert handler_calls == 0
    assert graph_creations == 1


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

    monkeypatch.setattr(runner.AsyncSqliteSaver, "from_conn_string", tracked_from_conn_string)
    resume: object
    if resume_kind == "checkpoint":
        resume = runner.CheckpointResume()
    else:
        resume = runner.HitlResume(
            interrupts=(
                runner.HitlInterruptDecision(
                    interrupt_id="missing-interrupt",
                    decisions=(runner.HitlDecision(decision="approve"),),
                ),
            )
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

    result = await runtime.run_action(_request(tmp_path, checkpoint_path=checkpoint_path))

    assert result.outcome.kind == "succeeded"
    assert checkpoint_path.is_file()
    with sqlite3.connect(checkpoint_path) as connection:
        marker = connection.execute(
            "SELECT format_version FROM abi_checkpoint_metadata WHERE marker_key = ?",
            ("abi_action_checkpoint",),
        ).fetchone()
    assert marker == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_stage",
    ["write", "file_fsync", "publish", "parent_fsync"],
)
async def test_owner_sidecar_publish_failure_leaves_no_final_and_fresh_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    checkpoint_path = tmp_path / "atomic-owner" / "graph.sqlite"
    checkpoint_path.parent.mkdir()
    owner_path = checkpoint_path.with_name(f"{checkpoint_path.name}.abi-owner")
    real_write = runner.os.write
    real_fsync = runner.os.fsync
    fsync_calls = 0

    def injected_write(file_descriptor: int, data: object) -> int:
        if failure_stage == "write":
            raise OSError("injected sidecar write failure")
        return real_write(file_descriptor, data)

    def injected_fsync(file_descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if failure_stage == "file_fsync" and fsync_calls == 1:
            raise OSError("injected sidecar file fsync failure")
        if failure_stage == "parent_fsync" and fsync_calls == 2:
            raise OSError("injected sidecar parent fsync failure")
        real_fsync(file_descriptor)

    with monkeypatch.context() as failure_patch:
        failure_patch.setattr(runner.os, "write", injected_write)
        failure_patch.setattr(runner.os, "fsync", injected_fsync)
        if failure_stage == "publish":

            def fail_link(*args: object, **kwargs: object) -> None:
                raise OSError("injected sidecar publish failure")

            failure_patch.setattr(runner.os, "link", fail_link)
        failed = runner._claim_checkpoint_owner(checkpoint_path)

    assert failed is not None
    assert failed.outcome.kind == "repair_required"
    assert owner_path.exists() is False
    assert list(checkpoint_path.parent.iterdir()) == []

    runtime = _runtime(tmp_path, _RecordingOutcomeModel(human_counts=[]))
    recovered = await runtime.run_action(_request(tmp_path, checkpoint_path=checkpoint_path))

    assert recovered.outcome.kind == "succeeded"
    assert owner_path.read_bytes() == b"abi_action_checkpoint:1\n"
    assert set(checkpoint_path.parent.iterdir()) == {checkpoint_path, owner_path}


@pytest.mark.parametrize("owner_bytes", [b"abi_action_checkpoint:1\n", b"foreign-owner\n"])
def test_owner_sidecar_claim_never_overwrites_existing_final(
    tmp_path: Path,
    owner_bytes: bytes,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    checkpoint_path = tmp_path / "existing-owner" / "graph.sqlite"
    checkpoint_path.parent.mkdir()
    owner_path = checkpoint_path.with_name(f"{checkpoint_path.name}.abi-owner")
    owner_path.write_bytes(owner_bytes)
    inode_before = owner_path.stat().st_ino

    result = runner._claim_checkpoint_owner(checkpoint_path)

    if owner_bytes == b"abi_action_checkpoint:1\n":
        assert result is None
    else:
        assert result is not None
        assert result.outcome.defect_codes == ("checkpoint_foreign_database",)
    assert owner_path.read_bytes() == owner_bytes
    assert owner_path.stat().st_ino == inode_before
    assert set(checkpoint_path.parent.iterdir()) == {owner_path}


@pytest.mark.asyncio
async def test_resume_rejects_unmarked_external_sqlite_as_invalid_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    checkpoint_path = tmp_path / "unrelated.sqlite"
    with sqlite3.connect(checkpoint_path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT NOT NULL)")
        connection.execute("INSERT INTO unrelated VALUES ('preserve-me')")
        connection.commit()
    setup_calls = 0
    real_setup = runner.AsyncSqliteSaver.setup

    async def tracked_setup(self: Any) -> None:
        nonlocal setup_calls
        setup_calls += 1
        await real_setup(self)

    monkeypatch.setattr(runner.AsyncSqliteSaver, "setup", tracked_setup)
    runtime = _runtime(
        tmp_path,
        _ExplodingModel(error=AssertionError("model must not run")),
    )

    result = await runtime.run_action(
        _request(
            tmp_path,
            checkpoint_path=checkpoint_path,
            resume=runner.CheckpointResume(),
        )
    )

    assert result.outcome.kind == "repair_required"
    assert result.outcome.defect_codes == ("checkpoint_foreign_database",)
    assert setup_calls == 0


@pytest.mark.asyncio
async def test_shared_first_initialization_hides_schema_until_marker_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    checkpoint_path = tmp_path / "shared" / "graph.sqlite"
    schema_ready = asyncio.Event()
    allow_marker_commit = asyncio.Event()
    second_started = asyncio.Event()
    real_initializer = runner._initialize_checkpoint_database
    real_from_conn_string = runner.AsyncSqliteSaver.from_conn_string
    saver_factories = 0

    async def paused_initializer(checkpointer: Any) -> None:
        await checkpointer.setup()
        schema_ready.set()
        await allow_marker_commit.wait()
        await real_initializer(checkpointer)

    def tracked_from_conn_string(path: str) -> Any:
        nonlocal saver_factories
        saver_factories += 1
        return real_from_conn_string(path)

    monkeypatch.setattr(runner, "_initialize_checkpoint_database", paused_initializer)
    monkeypatch.setattr(
        runner.AsyncSqliteSaver,
        "from_conn_string",
        tracked_from_conn_string,
    )
    first_model = _RecordingOutcomeModel(human_counts=[])
    second_model = _RecordingOutcomeModel(human_counts=[])
    first_runtime = _runtime(tmp_path, first_model)
    second_runtime = _runtime(tmp_path, second_model)
    real_second_event = second_runtime._events.event

    def track_second_start(event_type: str, **payload: Any) -> None:
        real_second_event(event_type, **payload)
        if event_type == "agent.run.start":
            second_started.set()

    monkeypatch.setattr(second_runtime._events, "event", track_second_start)
    first_task = asyncio.create_task(
        first_runtime.run_action(
            _request(
                tmp_path,
                checkpoint_path=checkpoint_path,
                thread_id="run-1/a1/1",
            )
        )
    )
    await schema_ready.wait()
    second_task = asyncio.create_task(
        second_runtime.run_action(
            _request(
                tmp_path,
                checkpoint_path=checkpoint_path,
                thread_id="run-1/a2/1",
            )
        )
    )
    await second_started.wait()
    second_waited_before_opening = saver_factories == 1 and not second_task.done()
    allow_marker_commit.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert second_waited_before_opening
    assert saver_factories == 2
    assert first.outcome.kind == "succeeded"
    assert second.outcome.kind == "succeeded"
    assert first_model.human_counts == [1]
    assert second_model.human_counts == [1]


def test_shared_initialization_crosses_threads_and_event_loops_without_leaking_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    checkpoint_path = tmp_path / "cross-loop" / "graph.sqlite"
    initialization_started = threading.Event()
    release_initialization = threading.Event()
    probe_waiting = threading.Event()
    probe_cancelled = threading.Event()
    second_acquire_entered = threading.Event()
    second_acquire_returned = threading.Event()
    first_loop: list[asyncio.AbstractEventLoop] = []
    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}
    result_guard = threading.Lock()
    real_initializer = runner._initialize_checkpoint_database
    real_acquire = runner._acquire_checkpoint_initialization
    initialization_calls = 0
    initialization_guard = threading.Lock()

    async def paused_first_initializer(checkpointer: Any) -> None:
        nonlocal initialization_calls
        with initialization_guard:
            initialization_calls += 1
            first_call = initialization_calls == 1
        if first_call:
            await checkpointer.setup()
            first_loop.append(asyncio.get_running_loop())
            initialization_started.set()
            await asyncio.to_thread(release_initialization.wait)
        await real_initializer(checkpointer)

    async def tracked_acquire(path: Path) -> Any:
        if threading.current_thread().name == "checkpoint-loop-b":
            second_acquire_entered.set()
            try:
                return await real_acquire(path)
            finally:
                second_acquire_returned.set()
        return await real_acquire(path)

    monkeypatch.setattr(
        runner,
        "_initialize_checkpoint_database",
        paused_first_initializer,
    )
    monkeypatch.setattr(runner, "_acquire_checkpoint_initialization", tracked_acquire)

    def thread_target(label: str) -> None:
        async def run() -> object:
            runtime_dir = tmp_path / f"runtime-{label}"
            runtime_dir.mkdir()
            runtime = _runtime(runtime_dir, _RecordingOutcomeModel(human_counts=[]))
            return await runtime.run_action(
                _request(
                    runtime_dir,
                    checkpoint_path=checkpoint_path,
                    thread_id=f"run-1/{label}/1",
                )
            )

        try:
            value = asyncio.run(run())
        except BaseException as error:
            with result_guard:
                errors[label] = error
        else:
            with result_guard:
                results[label] = value

    first_thread = threading.Thread(
        target=thread_target,
        args=("a",),
        name="checkpoint-loop-a",
        daemon=True,
    )
    second_thread = threading.Thread(
        target=thread_target,
        args=("b",),
        name="checkpoint-loop-b",
        daemon=True,
    )
    first_thread.start()
    try:
        assert initialization_started.wait(timeout=5)

        async def cancelled_same_loop_waiter() -> None:
            waiter = asyncio.create_task(real_acquire(checkpoint_path))
            await asyncio.sleep(0)
            probe_waiting.set()
            try:
                await waiter
            finally:
                probe_cancelled.set()

        probe_future = asyncio.run_coroutine_threadsafe(
            cancelled_same_loop_waiter(),
            first_loop[0],
        )
        assert probe_waiting.wait(timeout=5)
        probe_future.cancel()
        assert probe_cancelled.wait(timeout=5)
        with runner._CHECKPOINT_INITIALIZATIONS_GUARD:
            assert runner._CHECKPOINT_INITIALIZATIONS[str(checkpoint_path)].users == 1

        second_thread.start()
        assert second_acquire_entered.wait(timeout=5)
        returned_while_first_held = second_acquire_returned.wait(timeout=0.25)
    finally:
        release_initialization.set()
        first_thread.join(timeout=5)
        if second_thread.ident is not None:
            second_thread.join(timeout=5)

    assert returned_while_first_held is False
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == {}
    assert set(results) == {"a", "b"}
    assert all(result.outcome.kind == "succeeded" for result in results.values())
    with runner._CHECKPOINT_INITIALIZATIONS_GUARD:
        assert runner._CHECKPOINT_INITIALIZATIONS == {}


@pytest.mark.asyncio
async def test_failed_first_initialization_can_be_completed_by_fresh_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    checkpoint_path = tmp_path / "recoverable" / "graph.sqlite"
    real_initializer = runner._initialize_checkpoint_database
    initialization_attempts = 0

    async def fail_after_schema_once(checkpointer: Any) -> None:
        nonlocal initialization_attempts
        initialization_attempts += 1
        if initialization_attempts == 1:
            await checkpointer.setup()
            raise RuntimeError("injected failure before ABI marker commit")
        await real_initializer(checkpointer)

    monkeypatch.setattr(
        runner,
        "_initialize_checkpoint_database",
        fail_after_schema_once,
    )
    first_runtime = _runtime(tmp_path, _RecordingOutcomeModel(human_counts=[]))
    retry_model = _RecordingOutcomeModel(human_counts=[])
    retry_runtime = _runtime(tmp_path, retry_model)

    failed = await first_runtime.run_action(
        _request(
            tmp_path,
            checkpoint_path=checkpoint_path,
            thread_id="run-1/a1/1",
        )
    )
    recovered = await retry_runtime.run_action(
        _request(
            tmp_path,
            checkpoint_path=checkpoint_path,
            thread_id="run-1/a2/1",
        )
    )

    assert failed.outcome.kind == "repair_required"
    assert failed.outcome.defect_codes == ("checkpoint_read_failed",)
    assert recovered.outcome.kind == "succeeded"
    assert initialization_attempts == 2
    assert retry_model.human_counts == [1]


@pytest.mark.asyncio
async def test_multiple_runtimes_share_first_initialization_and_isolate_threads(
    tmp_path: Path,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    checkpoint_path = tmp_path / "concurrent" / "graph.sqlite"
    labels = tuple(f"thread-{index}" for index in range(4))
    models = tuple(_LabeledOutcomeModel(human_counts=[], label=label) for label in labels)
    runtimes = tuple(_runtime(tmp_path, model) for model in models)

    fresh_results = await asyncio.gather(
        *(
            runtime.run_action(
                _request(
                    tmp_path,
                    checkpoint_path=checkpoint_path,
                    thread_id=f"run-1/{label}/1",
                )
            )
            for runtime, label in zip(runtimes, labels, strict=True)
        )
    )

    assert [result.outcome.kind for result in fresh_results] == ["succeeded"] * 4
    assert [result.outcome.evidence_refs for result in fresh_results] == [
        (label,) for label in labels
    ]
    assert [model.human_counts for model in models] == [[1], [1], [1], [1]]

    resume_runtimes = tuple(
        _runtime(
            tmp_path,
            _ExplodingModel(error=AssertionError("cached thread must not call a model")),
        )
        for _label in labels
    )
    resumed_results = await asyncio.gather(
        *(
            runtime.run_action(
                _request(
                    tmp_path,
                    checkpoint_path=checkpoint_path,
                    thread_id=f"run-1/{label}/1",
                    resume=runner.CheckpointResume(),
                )
            )
            for runtime, label in zip(resume_runtimes, labels, strict=True)
        )
    )

    assert [result.outcome.kind for result in resumed_results] == ["succeeded"] * 4
    assert [result.outcome.evidence_refs for result in resumed_results] == [
        (label,) for label in labels
    ]
    assert all(result.llm_calls == 0 for result in resumed_results)


@pytest.mark.asyncio
@pytest.mark.parametrize("alias_kind", ["file", "parent"])
async def test_resume_rejects_symlink_in_any_checkpoint_path_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alias_kind: str,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    target_dir = tmp_path / "checkpoint-target"
    target_path = target_dir / "graph.sqlite"
    runtime = _runtime(tmp_path, _RecordingOutcomeModel(human_counts=[]))
    first = await runtime.run_action(_request(tmp_path, checkpoint_path=target_path))
    assert first.outcome.kind == "succeeded"
    target_before = target_path.read_bytes()
    if alias_kind == "file":
        alias_path = tmp_path / "checkpoint-alias.sqlite"
        try:
            alias_path.symlink_to(target_path)
        except OSError as error:
            pytest.skip(f"symlink creation is unavailable: {type(error).__name__}")
    else:
        alias_parent = tmp_path / "checkpoint-parent-alias"
        try:
            alias_parent.symlink_to(target_dir, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"symlink creation is unavailable: {type(error).__name__}")
        alias_path = alias_parent / "graph.sqlite"
    saver_opens = 0
    real_from_conn_string = runner.AsyncSqliteSaver.from_conn_string

    def tracked_from_conn_string(path: str) -> Any:
        nonlocal saver_opens
        saver_opens += 1
        return real_from_conn_string(path)

    monkeypatch.setattr(runner.AsyncSqliteSaver, "from_conn_string", tracked_from_conn_string)

    result = await runtime.run_action(
        _request(
            tmp_path,
            checkpoint_path=alias_path,
            resume=runner.CheckpointResume(),
        )
    )

    assert result.outcome.kind == "repair_required"
    assert result.outcome.defect_codes == ("checkpoint_path_unsafe",)
    assert saver_opens == 0
    assert target_path.read_bytes() == target_before


@pytest.mark.asyncio
async def test_resume_precheck_and_invoke_share_one_saver_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    checkpoint_path = tmp_path / "graph-checkpoints.sqlite"
    runtime = _runtime(tmp_path, _RecordingOutcomeModel(human_counts=[]))
    first = await runtime.run_action(_request(tmp_path, checkpoint_path=checkpoint_path))
    assert first.outcome.kind == "succeeded"
    real_from_conn_string = runner.AsyncSqliteSaver.from_conn_string
    saver_opens = 0
    replacement_hook_ran = False

    def adversarial_from_conn_string(path: str) -> Any:
        nonlocal saver_opens, replacement_hook_ran
        saver_opens += 1
        if saver_opens > 1:
            replacement_hook_ran = True
            checkpoint_path.unlink()
            with sqlite3.connect(checkpoint_path) as connection:
                connection.execute("CREATE TABLE replacement (value TEXT)")
        return real_from_conn_string(path)

    monkeypatch.setattr(
        runner.AsyncSqliteSaver,
        "from_conn_string",
        adversarial_from_conn_string,
    )

    resumed = await runtime.run_action(
        _request(
            tmp_path,
            checkpoint_path=checkpoint_path,
            resume=runner.CheckpointResume(),
        )
    )

    assert resumed.outcome == first.outcome
    assert saver_opens == 1
    assert replacement_hook_ran is False


def _sqlite_read_error(
    error_type: type[sqlite3.Error], sqlite_errorcode: int, secret: str
) -> sqlite3.Error:
    error = error_type(secret)
    error.sqlite_errorcode = sqlite_errorcode
    return error


@pytest.mark.parametrize(
    ("raw_code", "kind", "checkpoint_code"),
    [
        (3338, "repair_required", "checkpoint_permission_denied"),
        (7178, "repair_required", "checkpoint_permission_denied"),
        (266, "repair_required", "checkpoint_read_failed"),
        (261, "retryable_failure", "checkpoint_busy"),
        (262, "retryable_failure", "checkpoint_busy"),
        (779, "repair_required", "checkpoint_corrupt"),
        (sqlite3.SQLITE_NOTADB, "repair_required", "checkpoint_corrupt"),
    ],
)
def test_checkpoint_sqlite_raw_codes_preserve_extended_meaning_before_base_code(
    raw_code: int,
    kind: str,
    checkpoint_code: str,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    error = _sqlite_read_error(sqlite3.DatabaseError, raw_code, "storage-secret")

    result = runner._checkpoint_read_failure(error)

    assert result.outcome.kind == kind
    if kind == "retryable_failure":
        assert result.outcome.error_code == checkpoint_code
    else:
        assert result.outcome.defect_codes == (checkpoint_code,)
    assert "storage-secret" not in result.outcome.message


def test_checkpoint_sqlite_missing_extension_constants_fall_back_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    for name in ("SQLITE_IOERR_ACCESS", "SQLITE_IOERR_AUTH", "SQLITE_IOERR_READ"):
        monkeypatch.delattr(sqlite3, name, raising=False)
    error = _sqlite_read_error(sqlite3.DatabaseError, 3338, "storage-secret")

    result = runner._checkpoint_read_failure(error)

    assert result.outcome.kind == "repair_required"
    assert result.outcome.defect_codes == ("checkpoint_read_failed",)
    assert "storage-secret" not in result.outcome.message


@pytest.mark.asyncio
async def test_checkpoint_preflight_preserves_busy_storage_classification() -> None:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    error = _sqlite_read_error(
        sqlite3.OperationalError,
        sqlite3.SQLITE_BUSY,
        "preflight-storage-secret",
    )

    class _FailingConnection:
        def execute(self, *args: Any, **kwargs: Any) -> Any:
            raise error

    class _FailingSaver:
        conn = _FailingConnection()

    result = await runner._preflight_checkpoint_database(_FailingSaver())

    assert result is not None
    assert result.outcome.kind == "retryable_failure"
    assert result.outcome.error_code == "checkpoint_busy"
    assert "preflight-storage-secret" not in result.outcome.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "kind", "checkpoint_code"),
    [
        (
            _sqlite_read_error(sqlite3.OperationalError, sqlite3.SQLITE_BUSY, "locked-secret"),
            "retryable_failure",
            "checkpoint_busy",
        ),
        (
            _sqlite_read_error(sqlite3.OperationalError, sqlite3.SQLITE_LOCKED, "busy-secret"),
            "retryable_failure",
            "checkpoint_busy",
        ),
        (
            TimeoutError("checkpoint-timeout-secret"),
            "retryable_failure",
            "checkpoint_read_timeout",
        ),
        (
            _sqlite_read_error(sqlite3.DatabaseError, sqlite3.SQLITE_CORRUPT, "corrupt-secret"),
            "repair_required",
            "checkpoint_corrupt",
        ),
        (
            _sqlite_read_error(sqlite3.DatabaseError, sqlite3.SQLITE_NOTADB, "malformed-secret"),
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
    initializer = _runtime(tmp_path, _RecordingOutcomeModel(human_counts=[]))
    initialized = await initializer.run_action(_request(tmp_path, checkpoint_path=checkpoint_path))
    assert initialized.outcome.kind == "succeeded"
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
    resumed = await runtime.run_action(_request(tmp_path, resume=runner.CheckpointResume()))

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
