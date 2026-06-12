"""Deterministic stratified random sampler for the post-EPUB spot-check.

Creates a new ``reviews/random_spotcheck/round_NNN/`` with a fresh stored seed,
a manifest, and an independent stratified sample per review agent. Re-sampling a
new round uses a new seed (per the spot-check policy) but is reproducible from
the stored seed.
"""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass

from abi.epub.result import GateResult
from abi.project.layout import BookProject
from abi.qa.units import AuditUnit, Stratum, extract_units, stratify

# Per-agent / per-round budgets (references/stratified_random_spotcheck.md).
_PARA_PER_AGENT = 120
_FULL_THRESHOLD = {"table": 80, "figure": 80, "formula": 100, "caption_note": 120}
_SAMPLE_IF_OVER = 20


@dataclass
class _RoundPlan:
    round_dir_name: str
    seed: int


def _next_round(project: BookProject) -> int:
    existing = sorted(project.random_spotcheck_dir.glob("round_*"))
    nums = [int(p.name.split("_")[1]) for p in existing if p.name.split("_")[1].isdigit()]
    return (max(nums) + 1) if nums else 1


def _sample(rng: random.Random, units: list[AuditUnit], k: int) -> list[AuditUnit]:
    if k >= len(units):
        return list(units)
    return rng.sample(units, k)


def _budget(stratum: Stratum, n: int) -> int:
    if stratum == "paragraph":
        return min(n, _PARA_PER_AGENT)
    if n <= _FULL_THRESHOLD[stratum]:
        return n
    return _SAMPLE_IF_OVER


def select_random_review_passages(
    project: BookProject,
    *,
    agents: int = 2,
    samples_per_agent: int = 120,
    target_confidence: float = 0.80,
) -> GateResult:
    units = extract_units(project)
    if not units:
        return GateResult(False, "no audit units found in chapters/final/")
    strata = stratify(units)

    round_no = _next_round(project)
    round_dir = project.random_spotcheck_dir / f"round_{round_no:03d}"
    round_dir.mkdir(parents=True, exist_ok=True)
    seed = uuid.uuid4().int % (2**31)

    (round_dir / "seed.json").write_text(
        json.dumps({"round": round_no, "seed": seed, "target_confidence": target_confidence},
                   indent=2),
        encoding="utf-8",
    )

    manifest = {
        "round": round_no,
        "seed": seed,
        "agents": agents,
        "target_confidence": target_confidence,
        "population": {s: len(u) for s, u in strata.items()},
        "total_units": len(units),
    }
    (round_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    agent_labels = [f"agent_{chr(ord('a') + i)}" for i in range(agents)]
    per_agent_counts: dict[str, int] = {}
    for ai, label in enumerate(agent_labels):
        rng = random.Random(seed + ai)
        samples_dir = round_dir / "samples" / label
        samples_dir.mkdir(parents=True, exist_ok=True)
        picked: list[AuditUnit] = []
        for stratum, pool in strata.items():
            if not pool:
                continue
            k = _budget(stratum, len(pool))
            if stratum == "paragraph":
                k = min(k, samples_per_agent)
            picked.extend(_sample(rng, pool, k))
        per_agent_counts[label] = len(picked)

        lines = [f"# Random spot-check round {round_no} — {label}", "",
                 f"Score every sample 0-100. seed={seed + ai}.", ""]
        sample_index = []
        for u in picked:
            lines += [f"## {u.unit_id}  [{u.stratum}]  ({u.chapter})", "", u.text, ""]
            sample_index.append({"unit_id": u.unit_id, "stratum": u.stratum, "chapter": u.chapter})
        (samples_dir / "samples.md").write_text("\n".join(lines), encoding="utf-8")
        (samples_dir / "samples.json").write_text(
            json.dumps(sample_index, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    (round_dir / "reviews").mkdir(exist_ok=True)
    (round_dir / "fixes").mkdir(exist_ok=True)
    (round_dir / "verification").mkdir(exist_ok=True)

    return GateResult(
        True,
        f"round {round_no}: {len(units)} units, {agents} agents, "
        f"samples/agent={per_agent_counts}",
        details={"round_dir": project.rel(round_dir), "round": round_no,
                 "agents": agent_labels, "per_agent": per_agent_counts},
    )
