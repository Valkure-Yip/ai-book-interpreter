"""Build the bounded, metadata-only context presented to the Planner."""

from __future__ import annotations

from abi.actions.registry import ActionRegistry
from abi.project.run_ledger import RunLedger
from abi.types.orchestration import IncidentView, RunSnapshot


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
    ) -> None:
        if artifact_limit < 0:
            raise ValueError("artifact_limit must be non-negative; configure a valid sample count")
        if incident_limit < 0:
            raise ValueError("incident_limit must be non-negative; configure a valid sample count")
        if rejection_limit < 0:
            raise ValueError("rejection_limit must be non-negative; configure a valid sample count")
        self._ledger = ledger
        self._registry = registry
        self._artifact_limit = artifact_limit
        self._incident_limit = incident_limit
        self._rejection_limit = rejection_limit

    async def build(self, run_id: str) -> RunSnapshot:
        """Return bounded evidence facts plus capabilities currently eligible to run."""
        ledger_snapshot = await self._ledger.load_snapshot(
            run_id, rejection_limit=self._rejection_limit
        )
        return ledger_snapshot.model_copy(
            update={
                "artifacts": ledger_snapshot.artifacts[: self._artifact_limit],
                "incidents": tuple(
                    self._truncate_incident(incident)
                    for incident in ledger_snapshot.incidents[: self._incident_limit]
                ),
                "eligible_actions": self._registry.eligible(ledger_snapshot),
            }
        )

    @staticmethod
    def _truncate_incident(incident: IncidentView) -> IncidentView:
        return incident.model_copy(update={"message": incident.message[:500]})
