"""Aggregate eval results and render machine- + human-readable reports."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from abi.eval.types import CalibrationResult, MechanicalScores


def _percentile(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] * (1 - (pos - lo)) + s[hi] * (pos - lo)


def aggregate_mechanical(scores: list[MechanicalScores]) -> dict[str, Any]:
    """Roll up per-paragraph mechanical scores for one system."""
    if not scores:
        return {"n": 0}
    paras = [s.para_score for s in scores]
    flags: Counter[str] = Counter()
    for s in scores:
        flags.update(s.flags)
    completeness = sum(s.completeness for s in scores) / len(scores)
    return {
        "n": len(scores),
        "score_avg": round(sum(paras) / len(paras), 4),
        "score_p10": round(_percentile(paras, 0.10), 4),
        "score_min": round(min(paras), 4),
        "completeness": round(completeness, 4),
        "length_ratio_avg": round(
            sum(s.length_ratio for s in scores) / len(scores), 4
        ),
        "flag_counts": dict(flags),
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def render_calibration_md(results: list[CalibrationResult]) -> str:
    lines = [
        "# Length-ratio calibration",
        "",
        "| 语言对 | n | p05 | p10 | p50 | p90 | p95 | 均值 | 建议区间 | 当前区间 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| {r.source_target} | {r.n} | {r.ratio_p05} | {r.ratio_p10} | "
            f"{r.ratio_p50} | {r.ratio_p90} | {r.ratio_p95} | {r.ratio_mean} | "
            f"[{r.suggested_lo}, {r.suggested_hi}] | "
            f"[{r.current_lo}, {r.current_hi}] ({r.current_method}) |"
        )
    lines += [
        "",
        "> 建议区间 = [p10, p90]（robust）。n < 50 的语言对不会写入 bands（回退到默认）。",
    ]
    return "\n".join(lines) + "\n"


def render_trace_md(report: Any) -> str:
    r = report  # TraceReport (avoid import cycle)
    lines = [
        f"# Eval trace — {r.book}",
        "",
        f"- 总判定: **{r.verdict}**",
        f"- 状态: {r.status}  (done={r.is_done})",
        f"- gate_integrity: {'OK' if r.gate_integrity_ok else 'FAIL'}",
        f"- reached_states: {'OK' if r.reached_states_ok else 'FAIL'}",
        f"- path_conformance: {'OK' if r.path_conformance_ok else 'FAIL'}",
        "",
        "## Gate integrity (replayed vs recorded)",
        "",
        "| gate | produces | recorded | replay_ok | consistent |",
        "| --- | --- | --- | --- | --- |",
    ]
    for g in r.gate_integrity:
        if not g.verifiable:
            mark = "n/a (auxiliary)"
        elif g.consistent:
            mark = "✓"
        else:
            mark = "✗ " + g.replay_reason[:60]
        lines.append(
            f"| {g.gate} | {g.produces} | {g.recorded} | {g.replay_ok} | {mark} |"
        )
    if r.skipped_states:
        lines += ["", f"⚠ skipped states: {', '.join(r.skipped_states)}"]
    if r.reached_states_failures:
        lines += ["", "## Reached-state replay failures"]
        lines += [f"- {f}" for f in r.reached_states_failures]
    lines += [
        "",
        "## System metrics",
        "",
        f"- cost_usd: {r.cost_usd}",
        f"- tokens: in={r.tokens_in} out={r.tokens_out}",
        f"- duration_s: {r.duration_s}  llm_calls: {r.llm_calls}",
        f"- first_pass_rate: {r.first_pass_rate}  recursion_caps: {r.recursion_caps}  "
        f"budget_stops: {r.budget_stops}",
        f"- stage_attempts: {r.stage_attempts}",
    ]
    return "\n".join(lines) + "\n"
