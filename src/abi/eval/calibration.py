"""Calibrate ``length_ratio`` bands from real reference distributions.

Given reference-bearing :class:`EvalTriple`s (e.g. WMT24++ post-edits), compute
``len(reference)/len(source)`` per language pair and summarise the distribution.
The suggested band is ``[p10, p90]`` — robust to outliers, generous enough not to
flag legitimate prose. This is a *calibration* output, never a regression gate.
"""

from __future__ import annotations

from collections import defaultdict

from abi.eval.mechanical import char_len
from abi.eval.types import (
    DEFAULT_LENGTH_BANDS,
    FALLBACK_BAND,
    CalibrationResult,
    EvalTriple,
    LengthBand,
    band_key,
)


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolation percentile; ``q`` in [0, 1]. ``sorted_vals`` non-empty."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def calibrate(triples: list[EvalTriple]) -> list[CalibrationResult]:
    """One :class:`CalibrationResult` per language pair present in ``triples``."""
    by_pair: dict[str, list[float]] = defaultdict(list)
    label: dict[str, str] = {}
    for t in triples:
        if not t.reference:
            continue
        src_len = char_len(t.source)
        if src_len == 0:
            continue
        key = band_key(t.source_lang, t.target_lang)
        by_pair[key].append(char_len(t.reference) / src_len)
        label[key] = f"{t.source_lang}-{t.target_lang}"

    results: list[CalibrationResult] = []
    for key, ratios in sorted(by_pair.items()):
        ratios.sort()
        cur_lo, cur_hi = DEFAULT_LENGTH_BANDS.get(key, FALLBACK_BAND)
        cur_method = "default" if key in DEFAULT_LENGTH_BANDS else "fallback"
        results.append(
            CalibrationResult(
                source_target=label.get(key, key),
                band_key=key,
                n=len(ratios),
                ratio_p05=round(_percentile(ratios, 0.05), 4),
                ratio_p10=round(_percentile(ratios, 0.10), 4),
                ratio_p50=round(_percentile(ratios, 0.50), 4),
                ratio_p90=round(_percentile(ratios, 0.90), 4),
                ratio_p95=round(_percentile(ratios, 0.95), 4),
                ratio_mean=round(sum(ratios) / len(ratios), 4),
                suggested_lo=round(_percentile(ratios, 0.10), 4),
                suggested_hi=round(_percentile(ratios, 0.90), 4),
                current_lo=cur_lo,
                current_hi=cur_hi,
                current_method=cur_method,
            )
        )
    return results


def bands_from_calibration(
    results: list[CalibrationResult], *, min_samples: int = 50
) -> dict[str, LengthBand]:
    """Turn calibration results into a usable bands map.

    Only pairs with ``n >= min_samples`` are emitted as ``method="calibrated"``;
    sparser pairs are skipped so they fall back to the QUALITY_SCORE defaults.
    """
    out: dict[str, LengthBand] = {}
    for r in results:
        if r.n < min_samples:
            continue
        out[r.band_key] = LengthBand(
            source_target=r.band_key,
            lo=r.suggested_lo,
            hi=r.suggested_hi,
            n=r.n,
            method="calibrated",
        )
    return out


def load_bands(path_text: str) -> dict[str, LengthBand]:
    """Load a persisted bands JSON (``{band_key: {lo,hi,n,method}}``) -> map."""
    import json

    data = json.loads(path_text)
    out: dict[str, LengthBand] = {}
    for key, v in data.items():
        if key.startswith("_"):
            continue
        out[key] = LengthBand(
            source_target=key,
            lo=float(v["lo"]),
            hi=float(v["hi"]),
            n=int(v.get("n", 0)),
            method=str(v.get("method", "calibrated")),
        )
    return out
