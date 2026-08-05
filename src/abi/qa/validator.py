"""Validate the latest random spot-check round against the excellence gate.

Reads each agent's machine-readable summary (``reviews/agent_X_summary.json``),
enforces avg>=92, min>=88, no single item <80, no open P0/P1/P2, and
release_confidence = min over agents of confidence >= 0.80. Also requires
``current_run_pass_rounds_count >= required`` (default 2) consecutive passing
rounds in the current run. Writes ``validation_report.json`` into the round dir.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from abi.epub.result import GateResult
from abi.project.layout import BookProject

AVG_FLOOR = 92.0
MIN_FLOOR = 88.0
ITEM_FLOOR = 80.0
CONFIDENCE_FLOOR = 0.80


def _required_pass_rounds() -> int:
    raw = os.environ.get("ABI_SPOTCHECK_PASS_ROUNDS", "2")
    try:
        return max(1, int(raw))
    except ValueError:
        return 2


@dataclass
class _AgentEval:
    label: str
    ok: bool
    reasons: list[str]
    confidence: float


def _eval_agent(label: str, data: dict[str, Any]) -> _AgentEval:
    reasons: list[str] = []
    avg = float(data.get("average_score", 0) or 0)
    low = float(data.get("lowest_score", 0) or 0)
    open_pri = int(data.get("open_p0_p1_p2", data.get("open_p0_p1_p2_count", 0)) or 0)
    confidence = float(data.get("confidence", 0) or 0)
    if avg < AVG_FLOOR:
        reasons.append(f"{label}: average {avg} < {AVG_FLOOR}")
    if low < MIN_FLOOR:
        reasons.append(f"{label}: lowest {low} < {MIN_FLOOR}")
    if open_pri > 0:
        reasons.append(f"{label}: {open_pri} open P0/P1/P2")
    for s in data.get("samples", []):
        sc = float(s.get("score", 100) or 0)
        if sc < ITEM_FLOOR:
            reasons.append(f"{label}: item {s.get('unit_id')} scored {sc} < {ITEM_FLOOR}")
            break
    return _AgentEval(label, not reasons, reasons, confidence)


def evaluate_spotcheck_summaries(
    *,
    round_id: str,
    summaries: dict[str, dict[str, Any]],
    prior_rounds: tuple[dict[str, dict[str, Any]], ...] = (),
    require_pass: bool = True,
) -> tuple[GateResult, bytes]:
    """Evaluate reviewer summaries and serialize the report without filesystem writes."""
    evals = [_eval_agent(label, summaries[label]) for label in sorted(summaries)]
    reasons = [reason for item in evals for reason in item.reasons]
    release_confidence = min((item.confidence for item in evals), default=0.0)
    if release_confidence < CONFIDENCE_FLOOR:
        reasons.append(f"release_confidence {release_confidence:.2f} < {CONFIDENCE_FLOOR}")
    this_round_pass = not reasons
    consecutive = 1 if this_round_pass else 0
    if this_round_pass:
        for prior in prior_rounds:
            prior_evals = [_eval_agent(label, prior[label]) for label in sorted(prior)]
            if (
                prior_evals
                and all(item.ok for item in prior_evals)
                and min(item.confidence for item in prior_evals) >= CONFIDENCE_FLOOR
            ):
                consecutive += 1
            else:
                break
    required = _required_pass_rounds()
    if this_round_pass and consecutive < required:
        reasons.append(
            f"need {required} consecutive passing rounds; have {consecutive} "
            "(run another spot-check round)"
        )
    status = "PASS" if this_round_pass and consecutive >= required else "FAIL"
    report = {
        "round": round_id,
        "status": status,
        "release_confidence": round(release_confidence, 4),
        "this_round_pass": this_round_pass,
        "current_run_pass_rounds_count": consecutive,
        "current_run_pass_rounds_required": required,
        "agents": [
            {"label": item.label, "ok": item.ok, "confidence": item.confidence}
            for item in evals
        ],
        "reasons": reasons,
    }
    result = GateResult(
        status == "PASS" if require_pass else True,
        f"{round_id}: {status}, confidence={release_confidence:.2f}, "
        f"pass_rounds={consecutive}/{required}",
        hard_errors=reasons,
        details=report,
    )
    return result, json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True).encode()


def _latest_round(project: BookProject) -> Path | None:
    rounds = sorted(project.random_spotcheck_dir.glob("round_*"))
    return rounds[-1] if rounds else None


def _round_quality_passed(round_dir: Path) -> bool:
    """Whether a prior round met the quality bar (independent of the consecutive
    rounds requirement, recomputed from reviewer-owned summaries rather than a
    potentially forged validation report."""
    summaries = sorted((round_dir / "reviews").glob("*summary*.json"))
    if len(summaries) < 2:
        return False
    evals: list[_AgentEval] = []
    for summary in summaries:
        try:
            data = json.loads(summary.read_text(encoding="utf-8"))
        except Exception:
            return False
        evals.append(_eval_agent(summary.stem, data))
    return (
        len(evals) >= 2
        and all(item.ok for item in evals)
        and min(item.confidence for item in evals) >= CONFIDENCE_FLOOR
    )


def validate_random_spotcheck(project: BookProject, *, require_pass: bool = True) -> GateResult:
    round_dir = _latest_round(project)
    if round_dir is None:
        return GateResult(False, "no spot-check round found — call select_random_review_passages")

    reviews_dir = round_dir / "reviews"
    summaries = sorted(reviews_dir.glob("*summary*.json"))
    if len(summaries) < 2:
        return GateResult(
            False,
            f"need >=2 agent summary JSONs in {project.rel(reviews_dir)}; found {len(summaries)}. "
            "Each reviewer must write reviews/agent_X_summary.json "
            "{average_score,lowest_score,open_p0_p1_p2,confidence,samples:[{unit_id,score}]}.",
        )

    evals: list[_AgentEval] = []
    reasons: list[str] = []
    for s in summaries:
        try:
            data = json.loads(s.read_text(encoding="utf-8"))
        except Exception as exc:
            reasons.append(f"{s.name}: invalid JSON ({exc})")
            continue
        ev = _eval_agent(s.stem, data)
        evals.append(ev)
        reasons.extend(ev.reasons)

    release_confidence = min((e.confidence for e in evals), default=0.0)
    if release_confidence < CONFIDENCE_FLOOR:
        reasons.append(f"release_confidence {release_confidence:.2f} < {CONFIDENCE_FLOOR}")

    this_round_pass = not reasons

    # Consecutive passing rounds in the current run (this round counts if it passes).
    required = _required_pass_rounds()
    prior_rounds = sorted(project.random_spotcheck_dir.glob("round_*"))
    consecutive = 1 if this_round_pass else 0
    for rd in reversed(prior_rounds[:-1]):
        if _round_quality_passed(rd):
            consecutive += 1
        else:
            break
    rounds_ok = consecutive >= required
    if this_round_pass and not rounds_ok:
        reasons.append(
            f"need {required} consecutive passing rounds; have {consecutive} "
            "(run another spot-check round)"
        )

    status = "PASS" if (this_round_pass and rounds_ok) else "FAIL"
    report = {
        "round": round_dir.name,
        "status": status,
        "release_confidence": round(release_confidence, 4),
        "this_round_pass": this_round_pass,
        "current_run_pass_rounds_count": consecutive,
        "current_run_pass_rounds_required": required,
        "agents": [{"label": e.label, "ok": e.ok, "confidence": e.confidence} for e in evals],
        "reasons": reasons,
    }
    (round_dir / "validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ok = status == "PASS"
    return GateResult(
        ok if require_pass else True,
        f"{round_dir.name}: {status}, confidence={release_confidence:.2f}, "
        f"pass_rounds={consecutive}/{required}",
        hard_errors=reasons,
        details=report,
    )
