"""Executable boundary tests for business tools and SDK adapters."""

from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from abi.providers.agent_runtime.tooling import to_langchain_tool
from abi.tools.belt import build_belt
from abi.tools.content import make_content_tools
from abi.tools.subagent import make_subagent_tools
from abi.types._base import FrozenModel
from abi.types.orchestration import RunSnapshot, RunStatus, Succeeded
from abi.types.tools import ToolBinding


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
async def test_review_subagent_uses_fresh_threads_unless_explicitly_resumed(
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
    spawn = make_subagent_tools(context)[0].callable  # type: ignore[arg-type]

    first = json.loads(await spawn(agent_label="agent_a", instructions="review round one"))
    second = json.loads(await spawn(agent_label="agent_a", instructions="review round two"))
    resumed = json.loads(
        await spawn(
            agent_label="agent_a",
            instructions="continue round one",
            resume_thread_id=first["thread_id"],
        )
    )

    assert first["thread_id"] != second["thread_id"]
    assert resumed["thread_id"] == first["thread_id"]
    assert requests[0].resume is None  # type: ignore[union-attr]
    assert requests[1].resume is None  # type: ignore[union-attr]
    assert requests[2].resume.kind == "checkpoint"  # type: ignore[union-attr]
