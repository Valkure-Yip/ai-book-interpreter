"""Build the bounded, metadata-only context presented to the Planner."""

from __future__ import annotations

from abi.actions.registry import ActionRegistry
from abi.project.run_ledger import RunLedger
from abi.types.orchestration import IncidentView, PlanningContext


class SnapshotBuilder:
    """Compress durable ledger facts without opening canonical book artifacts."""

    def __init__(
        self,
        *,
        ledger: RunLedger,
        registry: ActionRegistry,
        artifact_limit: int = 20,
        incident_limit: int = 20,
        rejection_limit: int = 20,
        action_limit: int = 20,
        gate_evidence_limit: int = 20,
    ) -> None:
        if artifact_limit < 0:
            raise ValueError("artifact_limit must be non-negative; configure a valid sample count")
        if incident_limit < 0:
            raise ValueError("incident_limit must be non-negative; configure a valid sample count")
        if rejection_limit < 0:
            raise ValueError("rejection_limit must be non-negative; configure a valid sample count")
        if action_limit < 0:
            raise ValueError("action_limit must be non-negative; configure a valid sample count")
        if gate_evidence_limit < 0:
            raise ValueError("gate_evidence_limit must be non-negative; configure a valid sample count")
        self._ledger = ledger
        self._registry = registry
        self._artifact_limit = artifact_limit
        self._incident_limit = incident_limit
        self._rejection_limit = rejection_limit
        self._action_limit = action_limit
        self._gate_evidence_limit = gate_evidence_limit

    async def build(self, run_id: str) -> PlanningContext:
        """Return full policy facts and a separately bounded Planner-only evidence view."""
        ledger_snapshot = await self._ledger.load_snapshot(run_id, rejection_limit=None)
        policy_snapshot = ledger_snapshot.model_copy(
            update={"eligible_actions": self._registry.eligible(ledger_snapshot)}
        )
        planner_snapshot = policy_snapshot.model_copy(
            update={
                "actions": policy_snapshot.actions[: self._action_limit],
                "artifacts": policy_snapshot.artifacts[: self._artifact_limit],
                "gate_evidence": policy_snapshot.gate_evidence[: self._gate_evidence_limit],
                "incidents": tuple(
                    self._truncate_incident(incident)
                    for incident in policy_snapshot.incidents[: self._incident_limit]
                ),
                "plan_rejections": policy_snapshot.plan_rejections[: self._rejection_limit],
                "failure_signatures": policy_snapshot.failure_signatures[: self._action_limit],
            }
        )
        return PlanningContext(policy_snapshot=policy_snapshot, planner_snapshot=planner_snapshot)

    @staticmethod
    def _truncate_incident(incident: IncidentView) -> IncidentView:
        return incident.model_copy(update={"message": incident.message[:500]})
