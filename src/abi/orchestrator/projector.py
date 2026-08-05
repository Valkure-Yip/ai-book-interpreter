"""Rebuildable local projections driven by the durable event outbox."""

from __future__ import annotations

import json
from pathlib import Path

from abi.project.run_ledger import LedgerError, RunLedger
from abi.providers.observability.events import EventLogger


class OutboxProjector:
    """Append ordered outbox events once, then refresh non-authoritative projections."""

    def __init__(
        self,
        *,
        ledger: RunLedger,
        events: EventLogger,
        status_path: Path,
        metrics_path: Path | None = None,
    ) -> None:
        self._ledger = ledger
        self._events = events
        self._status_path = status_path
        self._metrics_path = metrics_path

    async def flush(self, run_id: str) -> int:
        """Project every currently undelivered row in sequence order."""
        delivered = 0
        last_sequence = await self._ledger.latest_event_sequence(run_id)
        for event in await self._ledger.undelivered_events(run_id):
            try:
                payload = json.loads(event.payload_json)
            except json.JSONDecodeError as exc:
                raise LedgerError(
                    f"outbox event {event.event_id} contains invalid JSON; repair the ledger row "
                    "before rebuilding events.jsonl"
                ) from exc
            if not isinstance(payload, dict) or not all(
                isinstance(key, str) for key in payload
            ):
                raise LedgerError(
                    f"outbox event {event.event_id} payload is not an object; repair the ledger "
                    "row before rebuilding events.jsonl"
                )
            record: dict[str, object] = {
                **payload,
                "event": event.event_name,
                "aggregate_id": event.aggregate_id,
                "sequence": event.sequence,
            }
            self._events.append_record(event.event_id, record)
            await self._ledger.mark_event_delivered(event.event_id, run_id=run_id)
            delivered += 1
            last_sequence = max(last_sequence, event.sequence)
        snapshot = await self._ledger.rebuild_status_projection(run_id, self._status_path)
        if self._metrics_path is not None:
            self._metrics_path.parent.mkdir(parents=True, exist_ok=True)
            projection = {
                "run_id": run_id,
                "status": snapshot.status.value,
                "plan_version": snapshot.plan_version,
                "actions": len(snapshot.actions),
                "artifacts": len(snapshot.artifacts),
                "open_incidents": len(snapshot.incidents),
                "last_event_sequence": last_sequence,
            }
            temporary = self._metrics_path.with_suffix(
                self._metrics_path.suffix + ".tmp"
            )
            temporary.write_text(
                json.dumps(projection, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self._metrics_path)
        return delivered
