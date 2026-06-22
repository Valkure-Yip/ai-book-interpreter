"""Eval domain models (frozen) + default length-ratio bands.

Bands mirror ``docs/QUALITY_SCORE.md``; calibration (``calibration.py``) can
override them per language pair from real reference distributions.
"""

from __future__ import annotations

from abi.types._base import FrozenModel

# Default expected ``len(target)/len(source)`` character-ratio bands, keyed by
# ``{source}-{target_base}`` (target region/script stripped, e.g. zh-Hans -> zh).
# Source: QUALITY_SCORE.md (v0.1 thresholds, to be recalibrated on >=500 samples).
DEFAULT_LENGTH_BANDS: dict[str, tuple[float, float]] = {
    "en-zh": (0.22, 0.55),
    "zh-en": (1.5, 4.0),
    "ja-zh": (0.45, 1.0),
    "en-ja": (0.6, 1.4),
    "zh-ja": (0.9, 1.6),
}

# Fallback band when a language pair has no calibrated/default entry: permissive,
# so an unknown pair never silently fails everything on length alone.
FALLBACK_BAND: tuple[float, float] = (0.2, 5.0)


def target_base(target_lang: str) -> str:
    """``"zh-Hans"`` -> ``"zh"``; the script/region tag is dropped for band lookup."""
    return target_lang.split("-", 1)[0]


def band_key(source_lang: str, target_lang: str) -> str:
    return f"{source_lang}-{target_base(target_lang)}"


class LengthBand(FrozenModel):
    """A calibrated or default length-ratio acceptance band for one language pair."""

    source_target: str
    lo: float
    hi: float
    n: int = 0
    method: str = "default"  # "default" | "calibrated"


class EvalTriple(FrozenModel):
    """One aligned evaluation unit: source + any of reference/abi/baseline."""

    paragraph_id: str
    source: str
    source_lang: str
    target_lang: str
    reference: str | None = None
    abi: str | None = None
    baseline: str | None = None
    domain: str | None = None
    document_id: str | None = None
    align_failed: bool = False


class MechanicalScores(FrozenModel):
    """Deterministic per-paragraph scores for one system's translation."""

    length_ratio: float
    length_ratio_ok: float
    anchor_preservation: float
    no_refusal_no_residue: float
    term_compliance: float | None
    completeness: float
    para_score: float
    flags: list[str]


class CalibrationResult(FrozenModel):
    """Length-ratio distribution summary + a suggested band for one language pair."""

    source_target: str
    band_key: str
    n: int
    ratio_p05: float
    ratio_p10: float
    ratio_p50: float
    ratio_p90: float
    ratio_p95: float
    ratio_mean: float
    suggested_lo: float
    suggested_hi: float
    current_lo: float
    current_hi: float
    current_method: str
