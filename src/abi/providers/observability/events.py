"""Local structured event stream (events.jsonl) and live metrics aggregation (metrics.json)."""

from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


class EventLogger:
    """Append-only JSONL writer. Thread-safe."""

    def __init__(self, path: Path, run_id: str) -> None:
        self._path = path
        self._run_id = run_id
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Touch the file so consumers can tail immediately.
        self._path.touch(exist_ok=True)

    def event(self, name: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            "run_id": self._run_id,
            "event": name,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


class MetricsAggregator:
    """In-memory running totals + atomic flush to metrics.json."""

    def __init__(self, path: Path, run_id: str, book_id: str) -> None:
        self._path = path
        self._run_id = run_id
        self._book_id = book_id
        self._lock = threading.Lock()
        self._started_at = datetime.utcnow()
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
            now = datetime.utcnow()
            return {
                "run_id": self._run_id,
                "book_id": self._book_id,
                "started_at": self._started_at.isoformat(timespec="seconds") + "Z",
                "updated_at": now.isoformat(timespec="seconds") + "Z",
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
