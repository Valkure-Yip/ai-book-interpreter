"""Behavior tests for the provider-owned Action harness."""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

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


def _runtime(tmp_path: Path, model: BaseChatModel, *, cap: float | None = None) -> Any:
    runner = importlib.import_module("abi.providers.agent_runtime.runner")
    assert hasattr(runner, "AgentActionRequest")
    return runner.AgentRuntime(
        config=LLMConfig(model="gpt-4o-mini"),
        api_key="test-key",
        budget=BudgetGate(cap),
        events=EventLogger(tmp_path / "events.jsonl", "run-1"),
        metrics=MetricsAggregator(tmp_path / "metrics.json", "run-1", "book-1"),
        langfuse_handler=None,
        langfuse_status=LangfuseStatus(False, False, "", "test"),
        sem=asyncio.Semaphore(1),
        model=model,
    )


def _request(
    tmp_path: Path,
    *,
    side_effects: bool = False,
    thread_id: str = "run-1/a1/1",
    tools: tuple[ToolBinding, ...] = (),
    max_iterations: int = 2,
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
    )


@pytest.mark.asyncio
async def test_action_harness_resumes_only_the_same_sqlite_thread(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, _HistoryAwareModel())

    first = await runtime.run_action(_request(tmp_path))
    second = await runtime.run_action(_request(tmp_path))
    isolated = await runtime.run_action(_request(tmp_path, thread_id="run-1/a2/1"))

    assert first.outcome.kind == "paused"
    assert first.tool_calls == 0
    assert second.outcome.kind == "succeeded"
    assert second.outcome.evidence_refs == ("prior_tool_result",)
    assert second.tool_calls == 0
    assert isolated.outcome.kind == "paused"
    assert (tmp_path / "graph-checkpoints.sqlite").is_file()


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
        (TimeoutError("delivery status unknown"), True, "indeterminate", None),
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
