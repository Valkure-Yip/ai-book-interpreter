"""Assemble categorized tool belts for the agent stages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from abi.tools.content import make_content_tools
from abi.tools.context import ToolContext
from abi.tools.fs import make_fs_tools
from abi.tools.gates import make_gate_tools
from abi.tools.permissions import ActionPathPermissions
from abi.tools.subagent import make_subagent_tools
from abi.types.orchestration import RunSnapshot
from abi.types.tools import GateRuntimeMetadata, ReviewActionIdentity, ToolBinding


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

    def resolve(self, allowlist: tuple[str, ...]) -> tuple[ToolBinding, ...]:
        """Resolve a declared allowlist exactly, failing closed on missing tools."""
        by_name = {tool.name: tool for tool in self.all()}
        missing = tuple(name for name in allowlist if name not in by_name)
        if missing:
            raise ValueError(
                f"Action tool allowlist contains unavailable tools {missing}; "
                "register them before execution"
            )
        return tuple(by_name[name] for name in allowlist)


def build_belt(
    ctx: ToolContext,
    *,
    get_run_snapshot: Callable[[], RunSnapshot] | None = None,
    permissions: ActionPathPermissions | None = None,
    gate_permissions: ActionPathPermissions | None = None,
    action_identity: ReviewActionIdentity | None = None,
    capability: str | None = None,
    runtime_metadata: GateRuntimeMetadata | None = None,
) -> ToolBelt:
    return ToolBelt(
        fs=make_fs_tools(ctx, permissions=permissions),
        content=make_content_tools(
            ctx,
            get_run_snapshot=get_run_snapshot,
            permissions=permissions,
        ),
        gates=make_gate_tools(
            ctx,
            permissions=gate_permissions or permissions,
            runtime_metadata=runtime_metadata,
        ),
        subagent=make_subagent_tools(
            ctx,
            permissions=permissions,
            action_identity=action_identity,
            capability=capability,
        ),
    )
