"""Structured, proposal-only planning over a compressed RunSnapshot."""

from __future__ import annotations

from typing import Any, Protocol

from abi.prompts.planner import PLANNER_SYSTEM_PROMPT
from abi.providers.llm.factory import system_message, user_message
from abi.types.orchestration import PlanPatch, RunSnapshot


class StructuredPlanRouter(Protocol):
    """The structured provider capability required by the proposal-only Planner."""

    async def invoke_structured(
        self,
        schema: type[PlanPatch],
        messages: list[Any],
        *,
        agent_name: str,
        prompt_version: str,
        max_retries: int,
    ) -> tuple[PlanPatch, object]: ...


class Planner:
    """Request a short PlanPatch through the observable LLM provider boundary."""

    def __init__(self, *, router: StructuredPlanRouter, horizon: int = 5) -> None:
        if not 1 <= horizon <= 5:
            raise ValueError("planner horizon must be between 1 and 5; configure a safe horizon")
        self._router = router
        self._horizon = horizon

    async def plan(self, snapshot: RunSnapshot) -> PlanPatch:
        """Produce one bounded proposal without granting it any mutable authority."""
        messages: list[Any] = [
            system_message(PLANNER_SYSTEM_PROMPT),
            user_message(snapshot.model_dump_json()),
        ]
        patch, _ = await self._router.invoke_structured(
            PlanPatch,
            messages,
            agent_name="orchestration.planner",
            prompt_version="dynamic-plan-v1",
            max_retries=2,
        )
        if not 1 <= len(patch.proposed_actions) <= self._horizon:
            raise ValueError(
                f"planner returned {len(patch.proposed_actions)} actions; return one to at most "
                f"{self._horizon} actions"
            )
        return patch
