"""Behavior tests for the provider-owned Action harness."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
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


class _NoopInput(FrozenModel):
    """No arguments are accepted."""


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
) -> Any:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    assert hasattr(runner, "AgentActionRequest")
    return runner.AgentActionRequest(
        system_prompt="Return a typed Action outcome.",
        user_prompt="Continue the Action.",
        tools=tools,
        agent_name="test-action",
        thread_id=thread_id,
        checkpoint_path=tmp_path / "graph-checkpoints.sqlite",
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
            resume=runner.HitlResume(decision="approve"),
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
            resume=runner.HitlResume(decision="reject", feedback="not authorized"),
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
        runner.HitlResume(decision="edit")


@pytest.mark.asyncio
async def test_action_harness_maps_real_graph_recursion_limit(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _LoopingModel())
    noop = ToolBinding("noop", "Continue the test loop.", _NoopInput, lambda: "continue")

    result = await runtime.run_action(_request(tmp_path, tools=(noop,), max_iterations=1))

    assert result.outcome.kind == "retryable_failure"
    assert result.outcome.error_code == "iteration_limit"
    assert result.stopped_reason == "iteration_limit"


@pytest.mark.asyncio
async def test_action_harness_maps_budget_callback_to_pause(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _HistoryAwareModel(), cap=0.0)

    result = await runtime.run_action(_request(tmp_path))

    assert result.outcome.kind == "paused"
    assert result.outcome.reason == "budget"
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
    assert result.tool_calls == 1
    assert result.stopped_reason == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "side_effects", "kind", "error_code"),
    [
        (
            ConnectionError("provider unavailable"),
            False,
            "retryable_failure",
            "transient_provider_error",
        ),
        (TimeoutError("provider timed out"), False, "retryable_failure", "provider_timeout"),
        (
            TimeoutError("provider timed out before any tool call"),
            True,
            "retryable_failure",
            "provider_timeout",
        ),
        (BudgetExceeded(0.0, 0.0, 0.1), False, "paused", None),
        (RuntimeError("unexpected"), False, "permanent_failure", "unclassified_exception"),
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
