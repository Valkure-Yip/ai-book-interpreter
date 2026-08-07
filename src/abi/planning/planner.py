"""Structured, proposal-only planning over a compressed RunSnapshot."""

from __future__ import annotations

from typing import Any, Protocol

from abi.prompts.planner import PLANNER_SYSTEM_PROMPT
from abi.providers.llm.factory import system_message, user_message
from abi.types.orchestration import PlanningContext, PlanPatch, ProposedAction


class StructuredPlanRouter(Protocol):
    """The structured provider capability required by the proposal-only Planner."""

    async def invoke_structured(
        self,
        schema: type[PlanPatch],
        messages: list[Any],
        *,
        agent_name: str,
        prompt_version: str,
        metadata: dict[str, Any],
        max_retries: int,
    ) -> tuple[PlanPatch, object]: ...


class Planner:
    """Request a short PlanPatch through the observable LLM provider boundary."""

    def __init__(self, *, router: StructuredPlanRouter, horizon: int = 5) -> None:
        if not 1 <= horizon <= 5:
            raise ValueError("planner horizon must be between 1 and 5; configure a safe horizon")
        self._router = router
        self._horizon = horizon

    async def plan(self, context: PlanningContext) -> PlanPatch:
        """Produce one bounded proposal without granting it any mutable authority."""
        messages: list[Any] = [
            system_message(PLANNER_SYSTEM_PROMPT),
            user_message(context.planner_snapshot.model_dump_json()),
        ]
        patch, _ = await self._router.invoke_structured(
            PlanPatch,
            messages,
            agent_name="orchestration.planner",
            prompt_version="dynamic-plan-v1",
            metadata={
                "run_id": context.policy_snapshot.run_id,
                "logical_invocation_id": (
                    f"planner:{context.policy_snapshot.run_id}:"
                    f"plan:{context.policy_snapshot.plan_version + 1}"
                )
            },
            max_retries=2,
        )
        if not 1 <= len(patch.proposed_actions) <= self._horizon:
            count = len(patch.proposed_actions)
            return PlanPatch(
                objective="record an invalid provider planning contract response",
                proposed_actions=(
                    ProposedAction(
                        proposal_id="planner-contract-invalid",
                        capability="planner.contract.invalid",
                    ),
                ),
                rationale=(
                    f"The structured provider returned {count} actions outside the bounded "
                    f"1..{self._horizon} horizon. Persist a policy rejection so the next "
                    "controller cycle can use its deterministic frontier fallback."
                ),
            )
        return patch
