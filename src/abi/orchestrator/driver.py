"""Macro orchestrator: walk the 28-state machine stage by stage to DONE.

This is the deterministic spine. It picks the next unfinished stage based on the
persisted ``PipelineState``, runs it via :func:`abi.stages.run_stage` (an agent
loop + gate), and advances. Resumable: re-running reloads state and continues
from wherever the project left off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from abi.project.state import HAPPY_PATH, Status, happy_index
from abi.prompts.stages import STAGE_SEQUENCE, StageSpec
from abi.providers.llm.budget import BudgetExceeded
from abi.stages.runner import StageOutcome, run_stage
from abi.tools.belt import build_belt
from abi.tools.context import ToolContext

_log = logging.getLogger(__name__)


@dataclass
class OrchestrationResult:
    final_status: Status
    stages_run: list[StageOutcome] = field(default_factory=list)
    cost_usd: float = 0.0
    blocked_reason: str | None = None


def _next_stage(current: Status) -> StageSpec | None:
    """First stage whose ``produces`` is later on the happy path than ``current``."""
    cur_idx = happy_index(current)
    if cur_idx < 0:
        # Off-path (e.g. a *_FAILED status); restart from the matching stage.
        cur_idx = -1
    for spec in STAGE_SEQUENCE:
        if happy_index(spec.produces) > cur_idx:
            return spec
    return None


class Orchestrator:
    def __init__(self, ctx: ToolContext, *, max_stage_attempts: int = 3) -> None:
        self._ctx = ctx
        self._belt = build_belt(ctx)
        self._max_stage_attempts = max_stage_attempts

    async def run(self, *, until: Status | None = None) -> OrchestrationResult:
        ctx = self._ctx
        outcomes: list[StageOutcome] = []
        cost = 0.0
        result = OrchestrationResult(final_status=ctx.state().status, stages_run=outcomes)

        ctx.services.events.event("pipeline.start", status=ctx.state().status.value)
        try:
            while True:
                st = ctx.state()
                if st.status in (Status.DONE, Status.FAILED):
                    break
                if until is not None and happy_index(st.status) >= happy_index(until) >= 0:
                    break
                spec = _next_stage(st.status)
                if spec is None:
                    if st.status != Status.DONE:
                        st.advance(Status.DONE, step="done", note="all stages complete")
                        ctx.save_state(st)
                    break

                _log.info("stage %s (from %s)", spec.stage_id, st.status.value)
                ctx.services.events.event(
                    "stage.start", stage=spec.stage_id, produces=spec.produces.value
                )
                outcome = await run_stage(
                    spec=spec, ctx=ctx, belt=self._belt,
                    max_attempts=self._max_stage_attempts,
                )
                outcomes.append(outcome)
                cost += outcome.cost_usd
                ctx.services.events.event(
                    "stage.end", stage=spec.stage_id, ok=outcome.ok,
                    attempts=outcome.attempts, reason=outcome.reason,
                )
                if not outcome.ok:
                    fail_st = ctx.state()
                    fail_st.fail(step=spec.stage_id, error=outcome.reason)
                    ctx.save_state(fail_st)
                    result.blocked_reason = f"{spec.stage_id}: {outcome.reason}"
                    break
        except BudgetExceeded as exc:
            ctx.services.events.event("pipeline.budget", detail=str(exc))
            result.blocked_reason = f"budget: {exc}"
        finally:
            ctx.services.metrics.flush()
            ctx.services.flush()

        result.final_status = ctx.state().status
        result.cost_usd = cost
        ctx.services.events.event(
            "pipeline.end", status=result.final_status.value, cost_usd=round(cost, 6)
        )
        return result


__all__ = ["HAPPY_PATH", "OrchestrationResult", "Orchestrator"]
