"""Assemble categorized tool belts for the agent stages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from abi.tools.content import make_content_tools
from abi.tools.context import ToolContext
from abi.tools.fs import make_fs_tools
from abi.tools.gates import make_gate_tools
from abi.tools.subagent import make_subagent_tools
from abi.types.orchestration import RunSnapshot
from abi.types.tools import ToolBinding


@dataclass
class ToolBelt:
    fs: list[ToolBinding] = field(default_factory=list)
    content: list[ToolBinding] = field(default_factory=list)
    gates: list[ToolBinding] = field(default_factory=list)
    subagent: list[ToolBinding] = field(default_factory=list)

    def all(self) -> list[ToolBinding]:
        return [*self.fs, *self.content, *self.gates, *self.subagent]

    def authoring(self) -> list[ToolBinding]:
        """Tools for stages that read references and write artifacts."""
        return [*self.fs, *self.content]

    def production(self) -> list[ToolBinding]:
        """Tools for build / gate stages."""
        return [*self.fs, *self.content, *self.gates]

    def review(self) -> list[ToolBinding]:
        """Tools for the spot-check / independent-review stages."""
        return [*self.fs, *self.content, *self.gates, *self.subagent]


def build_belt(
    ctx: ToolContext,
    *,
    get_run_snapshot: Callable[[], RunSnapshot] | None = None,
) -> ToolBelt:
    return ToolBelt(
        fs=make_fs_tools(ctx),
        content=make_content_tools(ctx, get_run_snapshot=get_run_snapshot),
        gates=make_gate_tools(ctx),
        subagent=make_subagent_tools(ctx),
    )
