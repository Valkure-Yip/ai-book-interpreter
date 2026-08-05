"""Run a single pipeline stage as a bounded agent loop + deterministic gate.

The agent is given the stage prompt and the stage's tool profile, runs until it
believes the stage is done (or hits its iteration cap), then a deterministic
:func:`abi.stages.validators.validate` confirms the exit condition. On failure
the validator's reason is fed back and the stage retried up to ``max_attempts``.
"""

from __future__ import annotations

from dataclasses import dataclass

from abi.prompts.stages import StageSpec, get_stage_registry
from abi.providers.agent_runtime import AgentActionRequest
from abi.stages.validators import validate
from abi.tools.belt import ToolBelt
from abi.tools.context import ToolContext
from abi.types.tools import ToolBinding


@dataclass
class StageOutcome:
    stage_id: str
    ok: bool
    reason: str
    attempts: int
    cost_usd: float


def _prompt_vars(ctx: ToolContext) -> dict[str, object]:
    st = ctx.state()
    return {
        "source_lang": st.source_lang,
        "target_lang": st.target_lang,
        "source_target": st.source_target,
        "publication_mode": st.publication_mode,
        "book_slug": st.book_slug,
        "profile": st.profile,
    }


def _tools_for(spec: StageSpec, belt: ToolBelt) -> list[ToolBinding]:
    if spec.tool_profile == "production":
        return belt.production()
    if spec.tool_profile == "review":
        return belt.review()
    return belt.authoring()


async def run_stage(
    *,
    spec: StageSpec,
    ctx: ToolContext,
    belt: ToolBelt,
    max_attempts: int = 3,
) -> StageOutcome:
    registry = get_stage_registry()
    base_vars = _prompt_vars(ctx)
    system_prompt = registry.system_prompt(**base_vars)
    stage_prompt = registry.stage_prompt(spec, **base_vars)
    tools = _tools_for(spec, belt)

    total_cost = 0.0
    last_reason = ""
    for attempt in range(1, max_attempts + 1):
        user_prompt = stage_prompt
        if attempt > 1:
            user_prompt = (
                stage_prompt + f"\n\n## RETRY (attempt {attempt})\nThe previous attempt did not "
                f"satisfy the deterministic gate. Reason: {last_reason}\nInspect the "
                "current files, fix the gap, and finish the stage."
            )
        result = await ctx.services.agent.run_action(
            AgentActionRequest(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools=tuple(tools),
                agent_name=spec.stage_id,
                max_iterations=spec.max_iterations,
                thread_id=f"{spec.stage_id}#{attempt}",
                checkpoint_path=ctx.project.graph_checkpoints,
            )
        )
        total_cost += result.cost_usd

        check = validate(ctx.project, spec.produces)
        if check.ok:
            st = ctx.state()
            if st.status != spec.produces:
                st.advance(spec.produces, step=spec.stage_id, note=f"gate ok ({attempt} attempt)")
            if spec.gate:
                st.record_gate(spec.gate, "PASS")
            ctx.save_state(st)
            return StageOutcome(spec.stage_id, True, "ok", attempt, total_cost)

        last_reason = check.reason
        if spec.gate:
            st = ctx.state()
            st.record_gate(spec.gate, "FAIL")
            ctx.save_state(st)

    return StageOutcome(spec.stage_id, False, last_reason, max_attempts, total_cost)
