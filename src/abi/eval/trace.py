"""L1 process-conformance replay (eval-standard.md §2 + §3).

Independently replays the deterministic gate validators over a finished/partial
book project and compares the replay to what ``pipeline_state.json`` recorded.
This is the ``gate_integrity`` check that proves invariant 3 (the agent cannot
self-declare PASS). Also folds in the crosscutting system metrics from
``metrics.json`` / ``events.jsonl``. No LLM calls.
"""

from __future__ import annotations

import json
from typing import Any

from abi.project.layout import BookProject
from abi.project.state import HAPPY_PATH, Status, happy_index
from abi.prompts.stages import STAGE_SEQUENCE
from abi.stages.validators import validate
from abi.types._base import FrozenModel

# gate name -> the Status it guards (from the stage table).
_GATE_PRODUCES: dict[str, Status] = {
    spec.gate: spec.produces for spec in STAGE_SEQUENCE if spec.gate
}


class GateIntegrityItem(FrozenModel):
    gate: str
    produces: str
    recorded: str
    replay_ok: bool
    replay_reason: str
    consistent: bool
    verifiable: bool  # False for auxiliary gates not bound in STAGE_SEQUENCE


class TraceReport(FrozenModel):
    book: str
    status: str
    is_done: bool
    blocked_reason: str | None
    # L1
    gate_integrity: list[GateIntegrityItem]
    gate_integrity_ok: bool
    reached_states_ok: bool
    reached_states_failures: list[str]
    path_conformance_ok: bool
    skipped_states: list[str]
    # crosscutting
    cost_usd: float | None
    tokens_in: int | None
    tokens_out: int | None
    duration_s: int | None
    llm_calls: int | None
    stage_attempts: dict[str, int]
    first_pass_rate: float | None
    recursion_caps: int
    budget_stops: int
    # rollup
    verdict: str  # PASS | WARN | FAIL


def _replay_gates(project: BookProject, recorded: dict[str, str]) -> list[GateIntegrityItem]:
    items: list[GateIntegrityItem] = []
    for gate, rec in sorted(recorded.items()):
        produces = _GATE_PRODUCES.get(gate)
        if produces is None:
            # Auxiliary gate (e.g. rights_check / ingest) set by tools/scaffold,
            # not bound to a Status in STAGE_SEQUENCE -> not replayable here.
            items.append(
                GateIntegrityItem(
                    gate=gate, produces="-", recorded=rec, replay_ok=False,
                    replay_reason="auxiliary gate (no STAGE_SEQUENCE binding)",
                    consistent=True, verifiable=False,
                )
            )
            continue
        check = validate(project, produces)
        rec_pass = rec.upper() == "PASS"
        consistent = (check.ok == rec_pass)
        items.append(
            GateIntegrityItem(
                gate=gate, produces=produces.value, recorded=rec,
                replay_ok=check.ok, replay_reason=check.reason,
                consistent=consistent, verifiable=True,
            )
        )
    return items


def _replay_reached_states(project: BookProject, status: Status) -> tuple[bool, list[str]]:
    """Replay validate() for every happy-path state up to ``status``."""
    cur = happy_index(status)
    failures: list[str] = []
    if cur < 0:  # off-path (e.g. FAILED): replay everything that has artifacts
        cur = len(HAPPY_PATH) - 1
    for st in HAPPY_PATH[1 : cur + 1]:
        if st in (Status.DONE,):
            continue
        check = validate(project, st)
        if not check.ok:
            failures.append(f"{st.value}: {check.reason}")
    return (not failures), failures


def _path_conformance(history_statuses: list[Status]) -> tuple[bool, list[str]]:
    """Flag forward skips on the happy path (jumping >1 stage ahead of the max)."""
    skipped: list[str] = []
    prev_max = 0
    for st in history_statuses:
        idx = happy_index(st)
        if idx < 0:
            continue  # off-path routing (FAILED / *_FAILED / REVISION_ROUTING)
        if idx > prev_max + 1:
            for j in range(prev_max + 1, idx):
                skipped.append(HAPPY_PATH[j].value)
        prev_max = max(prev_max, idx)
    return (not skipped), skipped


def _read_metrics(project: BookProject) -> dict[str, Any] | None:
    p = project.root / "metrics.json"
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _read_events(project: BookProject) -> tuple[dict[str, int], float | None, int, int]:
    p = project.root / "events.jsonl"
    attempts: dict[str, int] = {}
    caps = 0
    budget = 0
    if not p.exists():
        return attempts, None, caps, budget
    total = 0
    ok_first = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        name = ev.get("event")
        if name == "stage.end":
            stage = str(ev.get("stage", "?"))
            n = int(ev.get("attempts", 0) or 0)
            attempts[stage] = n
            total += 1
            if n == 1 and ev.get("ok"):
                ok_first += 1
        elif name == "agent.run.capped":
            caps += 1
        elif name == "pipeline.budget":
            budget += 1
    rate = (ok_first / total) if total else None
    return attempts, rate, caps, budget


def trace_project(project: BookProject) -> TraceReport:
    """Build the L1 + crosscutting :class:`TraceReport` for a book project."""
    state = project.load_state()
    integrity = _replay_gates(project, state.gates)
    integrity_ok = all(i.consistent for i in integrity if i.verifiable)
    reached_ok, reached_fail = _replay_reached_states(project, state.status)
    path_ok, skipped = _path_conformance([e.status for e in state.history])

    metrics = _read_metrics(project)
    attempts, first_pass, caps, budget = _read_events(project)

    # Verdict: gate_integrity is a hard CRITICAL (invariant 3); reached/path
    # failures are FAIL; an incomplete-but-clean run is WARN; else PASS.
    if not integrity_ok or not reached_ok or not path_ok:
        verdict = "FAIL"
    elif state.status != Status.DONE:
        verdict = "WARN"
    else:
        verdict = "PASS"

    tok = (metrics or {}).get("tokens", {}) if metrics else {}
    return TraceReport(
        book=project.root.name,
        status=state.status.value,
        is_done=state.status == Status.DONE,
        blocked_reason=state.last_error,
        gate_integrity=integrity,
        gate_integrity_ok=integrity_ok,
        reached_states_ok=reached_ok,
        reached_states_failures=reached_fail,
        path_conformance_ok=path_ok,
        skipped_states=skipped,
        cost_usd=(metrics or {}).get("cost_usd") if metrics else None,
        tokens_in=tok.get("input") if tok else None,
        tokens_out=tok.get("output") if tok else None,
        duration_s=(metrics or {}).get("duration_s") if metrics else None,
        llm_calls=(metrics or {}).get("llm_calls") if metrics else None,
        stage_attempts=attempts,
        first_pass_rate=round(first_pass, 4) if first_pass is not None else None,
        recursion_caps=caps,
        budget_stops=budget,
        verdict=verdict,
    )
