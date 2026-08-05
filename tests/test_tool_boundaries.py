"""Executable boundary tests for business tools and SDK adapters."""

from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from abi.providers.agent_runtime.tooling import to_langchain_tool
from abi.providers.llm.budget import BudgetGate
from abi.tools.belt import build_belt
from abi.tools.content import make_content_tools
from abi.tools.permissions import ActionPathPermissions
from abi.tools.subagent import make_subagent_tools
from abi.types._base import FrozenModel
from abi.types.orchestration import RunSnapshot, RunStatus, Succeeded
from abi.types.tools import ReviewActionIdentity, ToolBinding


class _Project:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.graph_checkpoints = root / "state" / "graph-checkpoints.sqlite"

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def within(self, path: Path) -> bool:
        return path.is_relative_to(self.root)

    def append_log(self, _: str) -> None:
        return None


def _context(tmp_path: Path) -> SimpleNamespace:
    project = _Project(tmp_path)
    return SimpleNamespace(project=project, resolve=lambda path: tmp_path / path)


def test_business_tool_factories_return_abi_bindings(tmp_path: Path) -> None:
    context = _context(tmp_path)
    belt = build_belt(context)  # type: ignore[arg-type]
    tools = belt.all()

    assert tools
    assert all(isinstance(tool, ToolBinding) for tool in tools)


def test_content_tools_expose_only_read_only_run_snapshot(tmp_path: Path) -> None:
    snapshot = RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
    tools = make_content_tools(
        _context(tmp_path),  # type: ignore[arg-type]
        get_run_snapshot=lambda: snapshot,
    )

    names = {tool.name for tool in tools}
    assert "get_run_snapshot" in names
    assert names.isdisjoint({"get_state", "set_state", "record_gate"})
    get_snapshot = next(tool for tool in tools if tool.name == "get_run_snapshot")
    payload = json.loads(get_snapshot.callable())
    assert payload == snapshot.model_dump(mode="json")


class _EchoInput(FrozenModel):
    value: str


@pytest.mark.asyncio
async def test_provider_adapter_executes_sync_and_async_bindings() -> None:
    async def async_echo(value: str) -> str:
        await asyncio.sleep(0)
        return f"async:{value}"

    sync_tool = to_langchain_tool(
        ToolBinding("sync_echo", "Echo synchronously.", _EchoInput, lambda value: f"sync:{value}")
    )
    async_tool = to_langchain_tool(
        ToolBinding("async_echo", "Echo asynchronously.", _EchoInput, async_echo)
    )

    assert sync_tool.invoke({"value": "x"}) == "sync:x"
    assert await async_tool.ainvoke({"value": "y"}) == "async:y"


@pytest.mark.asyncio
async def test_provider_adapter_awaits_async_callable_objects_and_wrappers() -> None:
    class AsyncEcho:
        async def __call__(self, value: str) -> str:
            await asyncio.sleep(0)
            return f"object:{value}"

    async def async_echo(value: str) -> str:
        await asyncio.sleep(0)
        return f"wrapped:{value}"

    @functools.wraps(async_echo)
    def wrapped_echo(value: str):  # type: ignore[no-untyped-def]
        return async_echo(value)

    object_tool = to_langchain_tool(
        ToolBinding("object_echo", "Echo through async __call__.", _EchoInput, AsyncEcho())
    )
    wrapped_tool = to_langchain_tool(
        ToolBinding("wrapped_echo", "Echo through a wrapped async call.", _EchoInput, wrapped_echo)
    )

    assert await object_tool.ainvoke({"value": "x"}) == "object:x"
    assert await wrapped_tool.ainvoke({"value": "y"}) == "wrapped:y"


@pytest.mark.asyncio
async def test_actual_start_hook_runs_after_schema_validation_for_all_callable_shapes() -> None:
    starts: list[tuple[str, dict[str, object]]] = []

    def started(binding: ToolBinding, arguments: dict[str, object]) -> None:
        starts.append((binding.name, arguments))

    async def async_echo(value: str) -> str:
        return value

    class AsyncEcho:
        async def __call__(self, value: str) -> str:
            return value

    bindings = (
        ToolBinding("sync", "sync", _EchoInput, lambda value: value),
        ToolBinding("async", "async", _EchoInput, async_echo),
        ToolBinding("object", "object", _EchoInput, AsyncEcho()),
    )
    tools = tuple(
        to_langchain_tool(binding, on_actual_start=started) for binding in bindings
    )

    for tool in tools:
        with pytest.raises(ValidationError):
            await tool.ainvoke({"wrong": "not validated"})
    assert starts == []

    for tool in tools:
        assert await tool.ainvoke({"value": tool.name}) == tool.name
    assert starts == [
        ("sync", {"value": "sync"}),
        ("async", {"value": "async"}),
        ("object", {"value": "object"}),
    ]


@pytest.mark.asyncio
async def test_review_subagent_uses_abi_owned_stable_distinct_threads(
    tmp_path: Path,
) -> None:
    requests: list[object] = []

    class RecordingAgent:
        async def run_action(self, request: object) -> object:
            requests.append(request)
            return SimpleNamespace(
                outcome=Succeeded(staging_relpath="state/staging/review.json")
            )

    context = _context(tmp_path)
    context.services = SimpleNamespace(agent=RecordingAgent())
    spawn = make_subagent_tools(
        context,
        permissions=ActionPathPermissions(read_dirs=("reviews",), write_dirs=()),
        action_identity=ReviewActionIdentity(run_id="run-1", action_id="review-1"),
        capability="review.independent",
    )[0].callable  # type: ignore[arg-type]

    first = json.loads(await spawn(agent_label="agent_a", instructions="review round one"))
    second = json.loads(await spawn(agent_label="agent_b", instructions="review round two"))

    assert first["thread_id"] != second["thread_id"]
    assert first["thread_id"] == "review:run-1:review-1:agent_a"
    assert second["thread_id"] == "review:run-1:review-1:agent_b"
    assert requests[0].resume is None  # type: ignore[union-attr]
    assert requests[1].resume is None  # type: ignore[union-attr]
    with pytest.raises(TypeError, match="resume_thread_id"):
        await spawn(
            agent_label="agent_a",
            instructions="attempt fake resume",
            resume_thread_id="attacker-controlled",
        )


@pytest.mark.asyncio
async def test_review_subagent_retry_keeps_thread_and_requests_checkpoint_resume(
    tmp_path: Path,
) -> None:
    requests: list[object] = []

    class RecordingAgent:
        async def run_action(self, request: object) -> object:
            requests.append(request)
            return SimpleNamespace(outcome=Succeeded(staging_relpath="reviews/agent_a/review.md"))

    context = _context(tmp_path)
    context.services = SimpleNamespace(agent=RecordingAgent())
    spawn = make_subagent_tools(
        context,
        permissions=ActionPathPermissions(read_dirs=("reviews",), write_dirs=()),
        action_identity=ReviewActionIdentity(run_id="run-1", action_id="review-1", attempt=2),
        capability="review.independent",
    )[0].callable  # type: ignore[arg-type]

    payload = json.loads(await spawn(agent_label="agent_a", instructions="continue"))

    assert payload["thread_id"] == "review:run-1:review-1:agent_a"
    assert requests[0].resume.kind == "checkpoint"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_spotcheck_subagents_receive_only_exact_reviewer_outputs(
    tmp_path: Path,
) -> None:
    requests: list[object] = []

    class RecordingAgent:
        async def run_action(self, request: object) -> object:
            requests.append(request)
            return SimpleNamespace(outcome=Succeeded(staging_relpath="review"))

    context = _context(tmp_path)
    context.services = SimpleNamespace(agent=RecordingAgent())
    context.project.random_spotcheck_dir = tmp_path / "reviews/random_spotcheck"
    samples = tmp_path / "reviews/random_spotcheck/round_001/samples/agent_a"
    samples.mkdir(parents=True)
    (samples / "samples.md").write_text("sample", encoding="utf-8")
    (samples / "samples.json").write_text("[]", encoding="utf-8")
    permissions = ActionPathPermissions(read_dirs=("reviews",), write_dirs=())
    spawn = make_subagent_tools(
        context,
        permissions=permissions,
        action_identity=ReviewActionIdentity(run_id="run-1", action_id="spotcheck-1"),
        capability="review.spotcheck",
    )[0].callable  # type: ignore[arg-type]

    await spawn(agent_label="agent_a", instructions="review")
    write_file = next(tool for tool in requests[0].tools if tool.name == "write_file")  # type: ignore[union-attr]
    write_file.callable(
        path="reviews/random_spotcheck/round_001/reviews/agent_a_summary.json",
        content="{}",
    )
    with pytest.raises(PermissionError, match="not allowed to write"):
        write_file.callable(
            path="reviews/random_spotcheck/round_001/validation_report.json",
            content='{"status":"PASS"}',
        )


@pytest.mark.asyncio
async def test_composite_reviewers_share_the_run_budget_gate(tmp_path: Path) -> None:
    budget = BudgetGate(None)
    observed_budget_ids: list[int] = []

    class BudgetAwareAgent:
        def __init__(self) -> None:
            self.budget = budget

        async def run_action(self, request: object) -> object:
            observed_budget_ids.append(id(self.budget))
            return SimpleNamespace(outcome=Succeeded(staging_relpath="review"))

    context = _context(tmp_path)
    agent = BudgetAwareAgent()
    context.services = SimpleNamespace(agent=agent, budget=budget)
    spawn = make_subagent_tools(
        context,
        permissions=ActionPathPermissions(read_dirs=("reviews",), write_dirs=()),
        action_identity=ReviewActionIdentity(run_id="run-1", action_id="review-1"),
        capability="review.independent",
    )[0].callable  # type: ignore[arg-type]

    first = json.loads(await spawn(agent_label="agent_a", instructions="review"))
    second = json.loads(await spawn(agent_label="agent_b", instructions="review"))

    assert first["thread_id"] != second["thread_id"]
    assert observed_budget_ids == [id(context.services.budget), id(context.services.budget)]
