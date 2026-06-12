"""Assemble categorized tool belts for the agent stages."""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.tools import BaseTool

from abi.tools.content import make_content_tools
from abi.tools.context import ToolContext
from abi.tools.fs import make_fs_tools
from abi.tools.gates import make_gate_tools
from abi.tools.subagent import make_subagent_tools


@dataclass
class ToolBelt:
    fs: list[BaseTool] = field(default_factory=list)
    content: list[BaseTool] = field(default_factory=list)
    gates: list[BaseTool] = field(default_factory=list)
    subagent: list[BaseTool] = field(default_factory=list)

    def all(self) -> list[BaseTool]:
        return [*self.fs, *self.content, *self.gates, *self.subagent]

    def authoring(self) -> list[BaseTool]:
        """Tools for stages that read references and write artifacts."""
        return [*self.fs, *self.content]

    def production(self) -> list[BaseTool]:
        """Tools for build / gate stages."""
        return [*self.fs, *self.content, *self.gates]

    def review(self) -> list[BaseTool]:
        """Tools for the spot-check / independent-review stages."""
        return [*self.fs, *self.content, *self.gates, *self.subagent]


def build_belt(ctx: ToolContext) -> ToolBelt:
    return ToolBelt(
        fs=make_fs_tools(ctx),
        content=make_content_tools(ctx),
        gates=make_gate_tools(ctx),
        subagent=make_subagent_tools(ctx),
    )
