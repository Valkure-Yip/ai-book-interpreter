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


def plan_random_review_passages(
    project: BookProject,
    *,
    round_id: str,
    reviewers: tuple[str, ...],
    chapters: tuple[str, ...],
    samples_per_agent: int,
    seed: int,
) -> tuple[GateResult, dict[str, bytes]]:
    """Build an authorized spot-check sample set without writing any files."""
    all_units = extract_units(project)
    available = {unit.chapter for unit in all_units}
    requested = set(chapters)
    if available & requested != requested:
        missing = sorted(requested - available)
        return GateResult(False, f"spot-check chapters are missing: {missing}"), {}
    units = [unit for unit in all_units if unit.chapter in requested]
    if not units:
        return GateResult(False, "no audit units found for authorized chapters"), {}
    strata = stratify(units)
    root = f"reviews/random_spotcheck/{round_id}"
    outputs: dict[str, bytes] = {}
    per_reviewer: dict[str, int] = {}
    for reviewer_index, reviewer in enumerate(reviewers):
        rng = random.Random(seed + reviewer_index)
        picked: list[AuditUnit] = []
        for stratum, pool in strata.items():
            if not pool:
                continue
            count = _budget(stratum, len(pool))
            if stratum == "paragraph":
                count = min(count, samples_per_agent)
            picked.extend(_sample(rng, pool, count))
        picked.sort(key=lambda item: item.unit_id)
        per_reviewer[reviewer] = len(picked)
        lines = [
            f"# Random spot-check {round_id} — {reviewer}",
            "",
            f"Score every sample 0-100. seed={seed + reviewer_index}.",
            "",
        ]
        sample_index: list[dict[str, str]] = []
        for unit in picked:
            lines.extend(
                (
                    f"## {unit.unit_id}  [{unit.stratum}]  ({unit.chapter})",
                    "",
                    unit.text,
                    "",
                )
            )
            sample_index.append(
                {
                    "unit_id": unit.unit_id,
                    "stratum": unit.stratum,
                    "chapter": unit.chapter,
                }
            )
        sample_root = f"{root}/samples/{reviewer}"
        outputs[f"{sample_root}/samples.md"] = "\n".join(lines).encode()
        outputs[f"{sample_root}/samples.json"] = json.dumps(
            sample_index, ensure_ascii=False, indent=2, sort_keys=True
        ).encode()

    manifest = {
        "round_id": round_id,
        "seed": seed,
        "reviewers": list(reviewers),
        "chapters": list(chapters),
        "samples_per_agent": samples_per_agent,
        "population": {name: len(pool) for name, pool in strata.items()},
        "total_units": len(units),
        "per_reviewer": per_reviewer,
    }
    outputs[f"{root}/round_manifest.json"] = json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True
    ).encode()
    return (
        GateResult(
            True,
            f"{round_id}: {len(units)} units, reviewers={list(reviewers)}",
            details=manifest,
        ),
        outputs,
    )


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
