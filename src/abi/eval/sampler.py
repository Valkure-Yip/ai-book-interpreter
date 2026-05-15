"""Stratified sampler: pick N paragraphs spread evenly across chapters."""

from __future__ import annotations

import random
from collections import defaultdict

from abi.types.eval import AlignedTriple

_MIN_CHARS = 30  # ignore paragraphs that carry essentially no information


def stratified_sample(
    triples: list[AlignedTriple],
    *,
    n_samples: int,
    seed: int = 1729,
) -> list[AlignedTriple]:
    """Pick ``n_samples`` triples roughly proportional to per-chapter paragraph count.

    Guarantees:
    - only aligned triples are considered
    - paragraphs shorter than ``_MIN_CHARS`` source are excluded
    - each chapter that has any eligible paragraphs contributes its first
      paragraph (so we cover chapter-start register)
    - remaining slots are filled proportionally, deterministic given ``seed``

    If fewer eligible paragraphs exist than ``n_samples``, returns all of them.
    """
    eligible = [
        t for t in triples
        if t.aligned and len(t.source_text) >= _MIN_CHARS
    ]
    if not eligible:
        return []
    if len(eligible) <= n_samples:
        return list(eligible)

    # Group by top-level chapter (first heading in trail, or section_id fallback).
    groups: dict[str, list[AlignedTriple]] = defaultdict(list)
    for t in eligible:
        key = t.heading_trail[0] if t.heading_trail else t.section_id
        groups[key].append(t)
    for k in groups:
        groups[k].sort(key=lambda t: t.position)

    rng = random.Random(seed)
    chosen: list[AlignedTriple] = []
    seen_ids: set[str] = set()

    # Force-include first paragraph of each chapter as long as budget allows.
    for k in groups:
        if len(chosen) >= n_samples:
            break
        first = groups[k][0]
        if first.paragraph_id not in seen_ids:
            chosen.append(first)
            seen_ids.add(first.paragraph_id)

    # Distribute remaining budget proportionally.
    remaining = n_samples - len(chosen)
    if remaining > 0:
        total = sum(len(v) for v in groups.values())
        per_group: dict[str, int] = {}
        for k, v in groups.items():
            per_group[k] = max(0, round(remaining * len(v) / total))

        # Tweak so per_group sums to exactly ``remaining`` (rounding drift).
        drift = remaining - sum(per_group.values())
        if drift != 0:
            keys = sorted(groups.keys(), key=lambda k: -len(groups[k]))
            for k in keys:
                if drift == 0:
                    break
                step = 1 if drift > 0 else -1
                if per_group[k] + step >= 0:
                    per_group[k] += step
                    drift -= step

        for k, take in per_group.items():
            candidates = [t for t in groups[k] if t.paragraph_id not in seen_ids]
            rng.shuffle(candidates)
            for t in candidates[:take]:
                chosen.append(t)
                seen_ids.add(t.paragraph_id)

    chosen.sort(key=lambda t: t.position)
    return chosen[:n_samples]
