"""Sub-agent spawning tool: run an independent reviewer with isolated context.

The two random-spot-check reviewers (agent_a / agent_b) must not see each
other's reasoning, so each runs as a fresh :meth:`AgentRuntime.run` with its own
message history and a read-only-ish tool subset.
"""

from __future__ import annotations

import json
import uuid

from pydantic import Field

from abi.tools.context import ToolContext
from abi.tools.fs import make_fs_tools
from abi.types._base import FrozenModel
from abi.types.tools import ToolBinding


class SpawnReviewAgentInput(FrozenModel):
    agent_label: str = Field(description="Short isolated reviewer identifier.")
    instructions: str = Field(description="Complete review assignment and output path.")
    resume_thread_id: str | None = Field(
        default=None,
        description="Exact prior review thread to resume; omit for a fresh isolated review.",
    )


def make_subagent_tools(ctx: ToolContext) -> list[ToolBinding]:
    async def spawn_review_agent(
        agent_label: str,
        instructions: str,
        resume_thread_id: str | None = None,
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
        # Read-only-ish subset: fs tools (the reviewer writes only its own report).
        tools = make_fs_tools(ctx)
        from abi.providers.agent_runtime import AgentActionRequest, CheckpointResume

        thread_id = resume_thread_id or f"review_{agent_label}_{uuid.uuid4().hex}"

        result = await ctx.services.agent.run_action(
            AgentActionRequest(
                system_prompt=system,
                user_prompt=instructions,
                tools=tuple(tools),
                agent_name=f"review_{agent_label}",
                checkpoint_path=ctx.project.graph_checkpoints,
                max_iterations=30,
                thread_id=thread_id,
                may_have_side_effects=True,
                resume=CheckpointResume() if resume_thread_id is not None else None,
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
