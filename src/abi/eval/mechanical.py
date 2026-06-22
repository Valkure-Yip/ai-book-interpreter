"""Deterministic per-paragraph metrics + ``para_score`` (eval-standard.md §4.2).

Pure functions, no LLM calls. ``para_score`` reweights to whichever subscores
apply (``term_compliance`` is dropped + renormalised when no glossary is given).
Formula version is ``abi.eval.FORMULA_VERSION``.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources

from abi.eval.types import (
    DEFAULT_LENGTH_BANDS,
    FALLBACK_BAND,
    LengthBand,
    MechanicalScores,
    band_key,
)

# --- anchors: tokens that must survive translation verbatim ---
_ANCHOR_RES = [
    re.compile(r"https?://\S+"),                       # URLs
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),        # emails
    re.compile(r"\d[\d,.\u2009 ]*\d|\d"),              # numbers / years
]
_REFUSAL_RE = re.compile(
    r"\b(i\s+(?:cannot|can't|am\s+unable|won't)\b|as\s+an\s+ai\b)|"
    r"(无法翻译|抱歉，我|我不能翻译|作为(?:一个)?\s*ai)",
    re.IGNORECASE,
)
_LATIN_RUN_RE = re.compile(r"[A-Za-z]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")
_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def char_len(text: str) -> int:
    """Whitespace-normalised character length (the unit for length ratios)."""
    return len(_norm(text))


@lru_cache(maxsize=1)
def packaged_calibrated_bands() -> dict[str, LengthBand]:
    """Committed calibrated bands (``assets/length_bands.json``), or empty if absent."""
    try:
        text = resources.files("abi.eval.assets").joinpath("length_bands.json").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, ModuleNotFoundError):
        return {}
    data = json.loads(text)
    out: dict[str, LengthBand] = {}
    for key, v in data.items():
        if key.startswith("_"):
            continue
        out[key] = LengthBand(
            source_target=key, lo=float(v["lo"]), hi=float(v["hi"]),
            n=int(v.get("n", 0)), method=str(v.get("method", "calibrated")),
        )
    return out


def resolve_band(
    source_lang: str, target_lang: str, bands: dict[str, LengthBand] | None = None
) -> LengthBand:
    """Resolve the length band, in precedence order:

    1. explicit ``bands`` arg (e.g. a fresh calibration run),
    2. committed calibrated bands (``assets/length_bands.json``),
    3. QUALITY_SCORE.md defaults,
    4. permissive fallback.
    """
    key = band_key(source_lang, target_lang)
    if bands and key in bands:
        return bands[key]
    packaged = packaged_calibrated_bands()
    if key in packaged:
        return packaged[key]
    if key in DEFAULT_LENGTH_BANDS:
        lo, hi = DEFAULT_LENGTH_BANDS[key]
        return LengthBand(source_target=key, lo=lo, hi=hi, method="default")
    lo, hi = FALLBACK_BAND
    return LengthBand(source_target=key, lo=lo, hi=hi, method="fallback")


def length_ratio_ok(ratio: float, band: LengthBand) -> float:
    """Smooth acceptance per QUALITY_SCORE.md: 1.0 in-band, linear to 0.5 in the
    +/-30%/-30% shoulders, 0.2 beyond."""
    lo, hi = band.lo, band.hi
    if lo <= ratio <= hi:
        return 1.0
    lo_floor, hi_ceil = lo * 0.7, hi * 1.3
    if lo_floor <= ratio < lo:
        # 0.5 at lo_floor -> 1.0 at lo
        return 0.5 + 0.5 * (ratio - lo_floor) / (lo - lo_floor)
    if hi < ratio <= hi_ceil:
        # 1.0 at hi -> 0.5 at hi_ceil
        return 1.0 - 0.5 * (ratio - hi) / (hi_ceil - hi)
    return 0.2


def _anchors(source: str) -> list[str]:
    found: list[str] = []
    for rx in _ANCHOR_RES:
        for m in rx.findall(source):
            tok = m.strip() if isinstance(m, str) else m
            if tok and len(str(tok)) >= 1:
                found.append(str(tok))
    # De-dup preserving order; drop pure single-digit noise duplicates.
    seen: set[str] = set()
    out: list[str] = []
    for t in found:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def anchor_preservation(source: str, target: str) -> float:
    anchors = _anchors(source)
    if not anchors:
        return 1.0
    norm_target = _norm(target)
    kept = sum(1 for a in anchors if a in norm_target)
    return kept / len(anchors)


def no_refusal_no_residue(source: str, target: str, *, source_lang: str, target_lang: str) -> float:
    """1.0 if no refusal phrase and no large wrong-script residue, else 0.0."""
    if _REFUSAL_RE.search(target):
        return 0.0
    if not target.strip():
        return 0.0
    tgt_is_cjk = target_lang.split("-", 1)[0] in {"zh", "ja"}
    letters = _norm(target)
    if not letters:
        return 0.0
    if tgt_is_cjk:
        # Too much latin script in a CJK target = untranslated residue.
        latin_chars = sum(len(m) for m in _LATIN_RUN_RE.findall(target))
        if latin_chars / max(1, len(_norm(target))) > 0.5:
            return 0.0
    else:
        # CJK residue in a non-CJK target.
        cjk_chars = len(_CJK_RE.findall(target))
        if cjk_chars / max(1, len(_norm(target))) > 0.3:
            return 0.0
    return 1.0


def term_compliance(source: str, target: str, glossary: dict[str, str] | None) -> float | None:
    """``1 - violations/occurrences`` over locked terms; None when no glossary.

    ``glossary`` maps source surface form -> required target form. A violation is
    a source term present in ``source`` whose required target form is absent in
    ``target``.
    """
    if not glossary:
        return None
    total = 0
    violations = 0
    low_src = source.lower()
    for src_term, tgt_term in glossary.items():
        occ = low_src.count(src_term.lower())
        if occ == 0:
            continue
        total += occ
        if tgt_term and tgt_term not in target:
            violations += occ
    if total == 0:
        return 1.0
    return max(0.0, 1.0 - violations / total)


# Subscore weights (eval-standard.md §4.2). term_compliance handled separately.
_W_TERM = 0.35
_W_LEN = 0.25
_W_ANCHOR = 0.20
_W_RESIDUE = 0.20


def score_paragraph(
    source: str,
    target: str | None,
    *,
    source_lang: str,
    target_lang: str,
    bands: dict[str, LengthBand] | None = None,
    glossary: dict[str, str] | None = None,
) -> MechanicalScores:
    """Score one aligned (source, target) pair. ``target=None`` => align/empty fail."""
    if target is None or not target.strip():
        return MechanicalScores(
            length_ratio=0.0,
            length_ratio_ok=0.0,
            anchor_preservation=0.0,
            no_refusal_no_residue=0.0,
            term_compliance=None,
            completeness=0.0,
            para_score=0.0,
            flags=["completeness_fail"],
        )

    band = resolve_band(source_lang, target_lang, bands)
    src_len = max(1, char_len(source))
    ratio = char_len(target) / src_len
    lr_ok = length_ratio_ok(ratio, band)
    anchor = anchor_preservation(source, target)
    residue = no_refusal_no_residue(
        source, target, source_lang=source_lang, target_lang=target_lang
    )
    term = term_compliance(source, target, glossary)

    if term is None:
        denom = _W_LEN + _W_ANCHOR + _W_RESIDUE
        para = (_W_LEN * lr_ok + _W_ANCHOR * anchor + _W_RESIDUE * residue) / denom
    else:
        para = _W_TERM * term + _W_LEN * lr_ok + _W_ANCHOR * anchor + _W_RESIDUE * residue

    flags: list[str] = []
    if term is not None and term < 1.0:
        flags.append("term_drift")
    if lr_ok < 0.5:
        flags.append("length_ratio_outlier")
    if residue == 0.0 and _REFUSAL_RE.search(target):
        flags.append("refusal_detected")
    elif residue == 0.0:
        flags.append("untranslated_residue")
    if para < 0.7:
        flags.append("low_score")

    return MechanicalScores(
        length_ratio=round(ratio, 4),
        length_ratio_ok=round(lr_ok, 4),
        anchor_preservation=round(anchor, 4),
        no_refusal_no_residue=residue,
        term_compliance=None if term is None else round(term, 4),
        completeness=1.0,
        para_score=round(para, 4),
        flags=flags,
    )
