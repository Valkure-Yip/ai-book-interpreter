"""Executable boundary tests for business tools and SDK adapters."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from abi.providers.agent_runtime.tooling import to_langchain_tool
from abi.tools.belt import build_belt
from abi.tools.content import make_content_tools
from abi.types._base import FrozenModel
from abi.types.orchestration import RunSnapshot, RunStatus
from abi.types.tools import ToolBinding


class _Project:
    def __init__(self, root: Path) -> None:
        self.root = root

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
