"""Sub-agent spawning tool: run an independent reviewer with isolated context.

The two random-spot-check reviewers (agent_a / agent_b) must not see each
other's reasoning, so each runs as a fresh :meth:`AgentRuntime.run` with its own
message history and a read-only-ish tool subset.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

from pydantic import Field

from abi.actions.builtins.inputs import SpotcheckInput
from abi.project.artifacts import AttemptStagingWriter, BufferedAttemptWriter
from abi.tools.context import ToolContext
from abi.tools.fs import make_fs_tools
from abi.tools.permissions import ActionPathPermissions
from abi.types._base import FrozenModel
from abi.types.orchestration import ExpectedArtifact
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
    writer: AttemptStagingWriter | BufferedAttemptWriter | None = None,
    spotcheck_input: SpotcheckInput | None = None,
    expected_artifacts: Mapping[str, ExpectedArtifact] | None = None,
) -> list[ToolBinding]:
    def reviewer_permissions(agent_label: str) -> tuple[ActionPathPermissions, str]:
        if permissions is None:
            raise PermissionError("review sub-agent requires explicit Action permissions")
        if capability == "review.independent":
            output = f"reviews/{agent_label}/review.md"
            return permissions.model_copy(update={"write_files": (output,), "write_dirs": ()}), output
        if capability == "review.spotcheck":
            if spotcheck_input is None:
                raise RuntimeError("spot-check reviewer requires frozen controller input")
            if agent_label not in spotcheck_input.reviewers:
                raise PermissionError(f"reviewer {agent_label!r} is not authorized for this round")
            round_rel = f"reviews/random_spotcheck/{spotcheck_input.round_id}"
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
            "rationale. Be strict: any single item <80 or any P0/P1/P2 is a FAIL. "
            "The report MUST end with exactly one plain, unwrapped machine-readable "
            "line: `result: PASS` or `result: FAIL`. A heading, table cell, bold verdict, "
            "localized verdict, or emoji does not replace that terminal line."
        )
        if capability == "review.independent":
            system += (
                " This review runs before release.prepare. Versioned release files are "
                "therefore intentionally absent and strictly out of scope; never fail or "
                "deduct points because output/release does not exist. Evaluate the built "
                "EPUB and the evidence you are authorized to read. Do not infer that an "
                "unavailable directory or file is missing project evidence."
            )
        elif capability == "review.spotcheck":
            if spotcheck_input is None:
                raise RuntimeError("spot-check reviewer requires frozen controller input")
            system += (
                f" The controller-authorized sample count is exactly "
                f"{spotcheck_input.samples_per_agent} per reviewer; the supplied sample "
                "file is a complete sample set for this round. Confidence measures your "
                "confidence in the reviewed passage assessments, not whole-book coverage. "
                "Do not lower confidence because the authorized sample count is small, and "
                "do not claim a sample lacks content when its source and translation are "
                "present."
            )
        if action_identity is None:
            raise RuntimeError("review sub-agent requires controller-owned Action identity")
        scoped_permissions, output = reviewer_permissions(agent_label)
        tools = make_fs_tools(
            ctx,
            permissions=scoped_permissions,
            writer=writer,
            expected_artifacts=expected_artifacts,
        )
        from abi.providers.agent_runtime import AgentActionRequest

        thread_id = (
            f"review:{action_identity.run_id}:{action_identity.action_id}:"
            f"{action_identity.attempt}:{agent_label}"
        )

        result = await ctx.services.agent.run_action(
            AgentActionRequest(
                system_prompt=system,
                user_prompt=(
                    f"{instructions}\n\nWrite only to {output}. End the written file with "
                    "exactly `result: PASS` or `result: FAIL` on its own line."
                ),
                tools=tuple(tools),
                agent_name=f"review_{agent_label}",
                checkpoint_path=ctx.project.action_checkpoints,
                max_iterations=30,
                thread_id=thread_id,
                may_have_side_effects=True,
                resume=None,
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
