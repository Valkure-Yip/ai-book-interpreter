"""Mechanical quality metrics.

All deterministic, all derivable without further LLM calls. Cover:

- glossary compliance: was each locked term rendered in the expected target?
- length ratio:        is the translation length within a sane band?
- anchor preservation: did numbers / URLs / proper-noun spans survive?
- completeness:        any empty / passthrough / missing translations?

Scores are in [0, 1]. We compute them separately for the ABI side and the
baseline side over the same aligned triples.
"""

from __future__ import annotations

import re

from abi.types.eval import AlignedTriple, MechanicalScore
from abi.types.glossary import Glossary

# Anchor patterns for eval. Deliberately broader than ``translate.validator``'s
# structural-only patterns — at eval time we also care about numbers, dates,
# URLs, and quoted strings, because dropping them is a hallmark of naive
# baselines that paraphrase instead of preserve.
_EVAL_ANCHOR_PATTERNS = [
    re.compile(r"\b\d{4}\b"),                     # years
    re.compile(r"\b\d+(?:[.,]\d+)+\b"),            # decimals / numbers like 1.5, 3,000
    re.compile(r"\b\d{2,}\b"),                     # multi-digit numbers
    re.compile(r"https?://\S+"),                   # URLs
    re.compile(r"\[\d+\]"),                        # footnote markers
    re.compile(r"\[\w+\d+\]"),                     # citations [Smith2020]
    re.compile(r"\bFig(?:ure|\.)\s*\d+(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\bTable\s*\d+(?:\.\d+)?\b", re.IGNORECASE),
]


def _extract_eval_anchors(text: str) -> list[str]:
    found: list[str] = []
    for pat in _EVAL_ANCHOR_PATTERNS:
        found.extend(m.group(0) for m in pat.finditer(text))
    seen: set[str] = set()
    out: list[str] = []
    for a in found:
        if a not in seen:
            out.append(a)
            seen.add(a)
    return out

# Length-ratio bands mirrored from translate/validator.py; we keep a local
# copy to avoid pulling validator's pydantic context-builder dependencies.
_LENGTH_RATIOS: dict[tuple[str, str], tuple[float, float]] = {
    ("en", "zh"): (0.22, 0.55),
    ("zh", "en"): (1.5, 4.0),
    ("ja", "zh"): (0.45, 1.0),
    ("en", "ja"): (0.6, 1.4),
    ("zh", "ja"): (0.9, 1.6),
}


def _length_band(src: str, tgt: str) -> tuple[float, float]:
    return _LENGTH_RATIOS.get((src, tgt), (0.4, 2.0))


def _glossary_compliance(
    triples: list[AlignedTriple],
    glossary: Glossary,
    pick_abi: bool,
) -> tuple[int, int, float]:
    """Return ``(checked, violations, score)``.

    A "check" happens whenever a locked surface form appears in the source
    paragraph; a "violation" is when the expected target rendering does NOT
    appear (case-insensitive substring) in that paragraph's translation.

    Translations with empty text are skipped (counted by completeness, not
    glossary compliance — punishing a missing-translation twice would
    double-charge the same defect).
    """
    surfaces = glossary.all_surfaces()
    if not surfaces:
        return 0, 0, 1.0

    checked = 0
    violations = 0
    for t in triples:
        translated = (t.abi_text if pick_abi else t.baseline_text).strip()
        if not translated:
            continue
        haystack = t.source_text.lower()
        translated_lower = translated.lower()
        for surface, entry in surfaces.items():
            if not surface or surface not in haystack:
                continue
            checked += 1
            if entry.target.lower() not in translated_lower:
                violations += 1
    if checked == 0:
        return 0, 0, 1.0
    score = 1.0 - violations / checked
    return checked, violations, max(0.0, min(1.0, score))


def _length_ratio(
    triples: list[AlignedTriple],
    source_lang: str,
    target_lang: str,
    pick_abi: bool,
) -> tuple[float, float]:
    """Return ``(fraction_in_band, mean_ratio)``."""
    lo, hi = _length_band(source_lang, target_lang)
    in_band = 0
    total = 0
    ratios: list[float] = []
    for t in triples:
        translated = (t.abi_text if pick_abi else t.baseline_text).strip()
        if not translated or not t.source_text:
            continue
        ratio = len(translated) / len(t.source_text)
        ratios.append(ratio)
        total += 1
        if lo <= ratio <= hi:
            in_band += 1
    if total == 0:
        return 1.0, 0.0
    return in_band / total, sum(ratios) / total


def _anchor_preservation(
    triples: list[AlignedTriple],
    pick_abi: bool,
) -> tuple[int, float]:
    """Return ``(checked_paragraphs, score)``.

    ``checked_paragraphs`` is the number of triples that had ≥1 anchor in
    source; ``score`` is the average preservation rate across those.
    """
    rates: list[float] = []
    for t in triples:
        anchors = _extract_eval_anchors(t.source_text)
        if not anchors:
            continue
        translated = (t.abi_text if pick_abi else t.baseline_text)
        if not translated:
            rates.append(0.0)
            continue
        present = sum(1 for a in anchors if a in translated)
        rates.append(present / len(anchors))
    if not rates:
        return 0, 1.0
    return len(rates), sum(rates) / len(rates)


def _completeness(triples: list[AlignedTriple], pick_abi: bool) -> tuple[int, float]:
    """Return ``(missing_count, score)``.

    A paragraph is "complete" if its translation has at least a few non-space
    characters relative to the source. Empty / whitespace-only output counts
    as missing.
    """
    missing = 0
    for t in triples:
        translated = (t.abi_text if pick_abi else t.baseline_text).strip()
        if not translated:
            missing += 1
            continue
        # Heuristic: if the translation is < 5% of source length, treat as
        # truncated/missing too (catches cases where the LLM emitted "..." or
        # an ellipsis or one stray word).
        if t.source_text and len(translated) < max(2, int(0.05 * len(t.source_text))):
            missing += 1
    score = 1.0 - missing / max(1, len(triples))
    return missing, max(0.0, min(1.0, score))


def compute_mechanical(
    *,
    triples: list[AlignedTriple],
    glossary: Glossary,
    source_language: str,
    target_language: str,
    system: str,
) -> MechanicalScore:
    """Compute one :class:`MechanicalScore` for either ABI or baseline."""
    pick_abi = system == "abi"

    gloss_checked, gloss_viol, gloss_score = _glossary_compliance(
        triples, glossary, pick_abi
    )
    length_ok, length_mean = _length_ratio(
        triples, source_language, target_language, pick_abi
    )
    anchor_checked, anchor_score = _anchor_preservation(triples, pick_abi)
    missing, completeness_score = _completeness(triples, pick_abi)

    return MechanicalScore(
        system=system,  # type: ignore[arg-type]
        n_paragraphs=len(triples),
        glossary_compliance=gloss_score,
        glossary_checked=gloss_checked,
        glossary_violations=gloss_viol,
        length_ratio_ok=length_ok,
        length_ratio_mean=length_mean,
        anchor_preservation=anchor_score,
        anchor_checked=anchor_checked,
        completeness=completeness_score,
        completeness_missing=missing,
    )
