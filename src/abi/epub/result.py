"""Shared PASS/FAIL result type for gate / build tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GateResult:
    ok: bool
    message: str
    hard_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def summary(self) -> str:
        head = "PASS" if self.ok else "FAIL"
        lines = [f"{head}: {self.message}"]
        for e in self.hard_errors[:30]:
            lines.append(f"  ERROR: {e}")
        for w in self.warnings[:15]:
            lines.append(f"  warn: {w}")
        return "\n".join(lines)

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ok": self.ok,
            "message": self.message,
            "hard_errors": len(self.hard_errors),
            "errors": self.hard_errors,
            "warnings": self.warnings,
            "details": self.details,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
