"""Sub-agent spawning tool: run an independent reviewer with isolated context.

The two random-spot-check reviewers (agent_a / agent_b) must not see each
other's reasoning, so each runs as a fresh :meth:`AgentRuntime.run` with its own
message history and a read-only-ish tool subset.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from abi.tools.context import ToolContext
from abi.tools.fs import make_fs_tools
from abi.tools.permissions import ActionPathPermissions
from abi.types._base import FrozenModel
from abi.types.tools import ReviewActionIdentity, ToolBinding


class SpawnReviewAgentInput(FrozenModel):
    agent_label: Literal["agent_a", "agent_b"] = Field(
        description="ABI-assigned isolated reviewer identifier."
    )
    instructions: str = Field(description="Complete review assignment and output path.")


def make_subagent_tools(
    ctx: ToolContext,
    *,
    permissions: ActionPathPermissions | None = None,
    action_identity: ReviewActionIdentity | None = None,
    capability: str | None = None,
) -> list[ToolBinding]:
    def reviewer_permissions(agent_label: str) -> tuple[ActionPathPermissions, str]:
        if permissions is None:
            raise PermissionError("review sub-agent requires explicit Action permissions")
        if capability == "review.independent":
            output = f"reviews/{agent_label}/review.md"
            return permissions.model_copy(update={"write_files": (output,), "write_dirs": ()}), output
        if capability == "review.spotcheck":
            rounds = sorted(ctx.project.random_spotcheck_dir.glob("round_*"))
            if not rounds:
                raise RuntimeError("select a spot-check round before spawning reviewers")
            round_rel = ctx.project.rel(rounds[-1])
            review = f"{round_rel}/reviews/{agent_label}_review.md"
            summary = f"{round_rel}/reviews/{agent_label}_summary.json"
            scoped = permissions.model_copy(
                update={
                    "read_files": (
                        *permissions.read_files,
                        f"{round_rel}/samples/{agent_label}/samples.md",
                        f"{round_rel}/samples/{agent_label}/samples.json",
                    ),
                    "write_files": (review, summary),
                    "write_dirs": (),
                }
            )
            return scoped, f"{review} and {summary}"
        raise PermissionError("review sub-agent is unavailable for this capability")

    async def spawn_review_agent(
        agent_label: str,
        instructions: str,
    ) -> str:
        """Spawn an independent review sub-agent with an isolated context.

        agent_label: short id, e.g. 'agent_a'. instructions: the review task
        (what to read, how to score). The sub-agent gets read-only filesystem
        tools and returns its final written report text.
        """
        system = (
            "You are an independent translation/EPUB quality reviewer. You work in "
            "isolation and must not assume any other reviewer's conclusions. Read the "
            "assigned samples and reference standards with the provided filesystem "
            "tools, then write your scored review to the requested path. Score every "
            "sample 0-100 with problem type, priority (P0/P1/P2), rework flag, and "
            "rationale. Be strict: any single item <80 or any P0/P1/P2 is a FAIL."
        )
        if action_identity is None:
            raise RuntimeError("review sub-agent requires controller-owned Action identity")
        scoped_permissions, output = reviewer_permissions(agent_label)
        tools = make_fs_tools(ctx, permissions=scoped_permissions)
        from abi.providers.agent_runtime import AgentActionRequest, CheckpointResume

        thread_id = (
            f"review:{action_identity.run_id}:{action_identity.action_id}:{agent_label}"
        )

        result = await ctx.services.agent.run_action(
            AgentActionRequest(
                system_prompt=system,
                user_prompt=f"{instructions}\n\nWrite only to {output}.",
                tools=tuple(tools),
                agent_name=f"review_{agent_label}",
                checkpoint_path=ctx.project.graph_checkpoints,
                max_iterations=30,
                thread_id=thread_id,
                may_have_side_effects=True,
                resume=CheckpointResume() if action_identity.attempt > 1 else None,
            )
        )
        return json.dumps(
            {
                "thread_id": thread_id,
                "outcome": result.outcome.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    return [
        ToolBinding(
            "spawn_review_agent",
            spawn_review_agent.__doc__ or "Spawn an isolated review agent.",
            SpawnReviewAgentInput,
            spawn_review_agent,
        ),
    ]
