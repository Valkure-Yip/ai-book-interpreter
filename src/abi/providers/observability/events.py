"""Local structured event stream (events.jsonl) and live metrics aggregation (metrics.json)."""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


class EventLogger:
    """Append-only JSONL writer. Thread-safe."""

    def __init__(self, path: Path, run_id: str) -> None:
        self._path = path
        self._run_id = run_id
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Touch the file so consumers can tail immediately.
        self._path.touch(exist_ok=True)
        self._seen_event_ids = self._load_seen_event_ids()

    def event(self, name: str, *, event_id: str | None = None, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": _utc_timestamp(timespec="milliseconds"),
            "run_id": self._run_id,
            "event": name,
            **fields,
        }
        if event_id is not None:
            self.append_record(event_id, record)
            return
        self._append_line(record)

    def append_record(self, event_id: str, record: Mapping[str, object]) -> bool:
        """Append one stable outbox/provider event once, including after restart."""
        if not event_id:
            raise ValueError(
                "event_id must be stable and non-empty; derive it from the call or Action attempt"
            )
        with self._lock, self._path.open("a", encoding="utf-8") as f:
            if event_id in self._seen_event_ids:
                return False
            materialized: dict[str, object] = {
                "ts": _utc_timestamp(timespec="milliseconds"),
                "run_id": self._run_id,
                **record,
                "event_id": event_id,
            }
            line = json.dumps(materialized, ensure_ascii=False, default=str)
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
            self._seen_event_ids.add(event_id)
        return True

    def _append_line(self, record: Mapping[str, object]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _load_seen_event_ids(self) -> set[str]:
        seen: set[str] = set()
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError(
                f"cannot read event projection {self._path}; repair its permissions before startup"
            ) from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"event projection contains invalid JSON on line {line_number}; repair or "
                    "rebuild events.jsonl from the ledger outbox"
                ) from exc
            if not isinstance(parsed, dict):
                raise ValueError(
                    f"event projection line {line_number} is not an object; rebuild events.jsonl "
                    "from the ledger outbox"
                )
            event_id = parsed.get("event_id")
            if event_id is not None and not isinstance(event_id, str):
                raise ValueError(
                    f"event projection line {line_number} has an invalid event_id; rebuild "
                    "events.jsonl from the ledger outbox"
                )
            if event_id:
                seen.add(event_id)
        return seen


class MetricsAggregator:
    """In-memory running totals + atomic flush to metrics.json."""

    def __init__(self, path: Path, run_id: str, book_id: str) -> None:
        self._path = path
        self._run_id = run_id
        self._book_id = book_id
        self._lock = threading.Lock()
        self._started_at = datetime.now(UTC)
        self._tokens_in = 0
        self._tokens_out = 0
        self._tokens_cached = 0
        self._cost_usd = 0.0
        self._llm_calls = 0
        self._paragraphs_total = 0
        self._paragraphs_done = 0
        self._paragraphs_flagged = 0
        self._paragraphs_failed = 0
        self._flag_counts: Counter[str] = Counter()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.flush()

    def record_llm_call(
        self,
        *,
        tokens_in: int,
        tokens_out: int,
        tokens_cached: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        with self._lock:
            self._llm_calls += 1
            self._tokens_in += tokens_in
            self._tokens_out += tokens_out
            self._tokens_cached += tokens_cached
            self._cost_usd += cost_usd

    def set_paragraphs_total(self, n: int) -> None:
        with self._lock:
            self._paragraphs_total = n

    def increment_paragraph(self, *, flagged: bool, failed: bool, flags: list[str] | None = None
                            ) -> None:
        with self._lock:
            self._paragraphs_done += 1
            if flagged:
                self._paragraphs_flagged += 1
            if failed:
                self._paragraphs_failed += 1
            for f in flags or []:
                self._flag_counts[f] += 1

    def total_cost(self) -> float:
        with self._lock:
            return self._cost_usd

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = datetime.now(UTC)
            return {
                "run_id": self._run_id,
                "book_id": self._book_id,
                "started_at": self._started_at.isoformat(timespec="seconds").replace(
                    "+00:00", "Z"
                ),
                "updated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "duration_s": int((now - self._started_at).total_seconds()),
                "llm_calls": self._llm_calls,
                "tokens": {
                    "input": self._tokens_in,
                    "output": self._tokens_out,
                    "cached": self._tokens_cached,
                },
                "cost_usd": round(self._cost_usd, 6),
                "paragraphs": {
                    "total": self._paragraphs_total,
                    "done": self._paragraphs_done,
                    "flagged": self._paragraphs_flagged,
                    "failed": self._paragraphs_failed,
                },
                "flag_counts": dict(self._flag_counts),
            }

    def flush(self) -> None:
        snap = self.snapshot()
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
        tmp.replace(self._path)


def _utc_timestamp(*, timespec: Literal["seconds", "milliseconds"]) -> str:
    return datetime.now(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")
