"""Sub-agent spawning tool: run an independent reviewer with isolated context.

The two random-spot-check reviewers (agent_a / agent_b) must not see each
other's reasoning, so each runs as a fresh :meth:`AgentRuntime.run` with its own
message history and a read-only-ish tool subset.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool

from abi.tools.context import ToolContext
from abi.tools.fs import make_fs_tools


def make_subagent_tools(ctx: ToolContext) -> list[BaseTool]:
    async def spawn_review_agent(agent_label: str, instructions: str) -> str:
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
        result = await ctx.services.agent.run(
            system_prompt=system,
            user_prompt=instructions,
            tools=tools,
            agent_name=f"review_{agent_label}",
            max_iterations=30,
            thread_id=f"review_{agent_label}",
        )
        return result.final_text or f"(sub-agent {agent_label} finished: {result.stopped_reason})"

    return [
        StructuredTool.from_function(coroutine=spawn_review_agent),
    ]
