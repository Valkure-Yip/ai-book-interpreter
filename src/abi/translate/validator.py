"""Validate a paragraph translation and produce QualityFlags."""

from __future__ import annotations

import re

from abi.translate.context_builder import TranslationContext, extract_anchors
from abi.types.book import ParagraphKind
from abi.types.glossary import Glossary
from abi.types.translation import (
    ParagraphTranslationOutput,
    QualityFlag,
    QualityFlagCode,
    TermUsage,
)

# Length ratio expectations: (source language, target language) -> (lo, hi)
# Calibrated against academic translation samples. Chinese characters carry
# more information per char than English letters, so en→zh ratios cluster low.
_LENGTH_RATIOS: dict[tuple[str, str], tuple[float, float]] = {
    ("en", "zh"): (0.22, 0.55),
    ("zh", "en"): (1.5, 4.0),
    ("ja", "zh"): (0.45, 1.0),
    ("en", "ja"): (0.6, 1.4),
    ("zh", "ja"): (0.9, 1.6),
}

_REFUSAL_PATTERNS = [
    re.compile(r"\bi cannot (?:translate|fulfill|assist)\b", re.IGNORECASE),
    re.compile(r"\bas an? ai\b", re.IGNORECASE),
    re.compile(r"无法翻译"),
    re.compile(r"作为(?:一个)?ai", re.IGNORECASE),
]


def _length_ratio_band(src: str, tgt: str) -> tuple[float, float]:
    return _LENGTH_RATIOS.get((src, tgt), (0.4, 2.0))


def _term_compliance(
    output: ParagraphTranslationOutput,
    source_text: str,
    glossary: Glossary,
) -> tuple[float, list[TermUsage], list[str]]:
    """Return (score, normalized terms_used, violations)."""
    surfaces = glossary.all_surfaces()
    haystack = source_text.lower()

    # Locked terms found in source.
    found = []
    for surface, entry in surfaces.items():
        if surface and surface in haystack:
            found.append(entry)
    # Dedup by entry.term
    seen: set[str] = set()
    found_terms = []
    for e in found:
        if e.term not in seen:
            found_terms.append(e)
            seen.add(e.term)

    if not found_terms:
        return 1.0, output.terms_used, []

    by_term = {e.term: e for e in found_terms}
    declared = {u.term: u for u in output.terms_used}

    violations: list[str] = []
    final_usage: list[TermUsage] = []
    translated_lower = output.translated_text.lower()

    for term, entry in by_term.items():
        usage = declared.get(term)
        # Determine actual rendering: explicit declaration takes precedence.
        rendered = (usage.rendered_as if usage else "").strip()
        expected = entry.target.strip()
        compliant: bool
        if rendered:
            compliant = rendered == expected
        else:
            # Best effort: did the expected target appear in the translation?
            compliant = expected.lower() in translated_lower
            rendered = expected if compliant else "(missing)"
        if not compliant:
            violations.append(f"term '{term}' should render as '{expected}', got '{rendered}'")
        final_usage.append(
            TermUsage(term=term, rendered_as=rendered, compliant=compliant)
        )

    compliant_count = sum(1 for u in final_usage if u.compliant)
    score = compliant_count / max(1, len(final_usage))
    return score, final_usage, violations


def _length_ratio_score(
    src_text: str, tgt_text: str, src_lang: str, tgt_lang: str
) -> tuple[float, float]:
    if not src_text:
        return 1.0, 0.0
    ratio = len(tgt_text) / len(src_text)
    lo, hi = _length_ratio_band(src_lang, tgt_lang)
    if lo <= ratio <= hi:
        return 1.0, ratio
    # Linear decay outside band to half of band.
    if lo * 0.7 <= ratio < lo:
        return 0.5 + 0.5 * (ratio - lo * 0.7) / (lo * 0.3), ratio
    if hi < ratio <= hi * 1.3:
        return 0.5 + 0.5 * (hi * 1.3 - ratio) / (hi * 0.3), ratio
    return 0.2, ratio


def _anchors_preserved(source: str, translated: str) -> tuple[float, list[str]]:
    src_anchors = extract_anchors(source)
    if not src_anchors:
        return 1.0, []
    missing = [a for a in src_anchors if a not in translated]
    score = (len(src_anchors) - len(missing)) / len(src_anchors)
    return score, missing


def _refusal_or_residue(text: str) -> tuple[float, list[str]]:
    issues: list[str] = []
    for pat in _REFUSAL_PATTERNS:
        if pat.search(text):
            issues.append("refusal_phrase")
            break
    return (0.0 if issues else 1.0), issues


def validate_translation(
    *,
    output: ParagraphTranslationOutput,
    context: TranslationContext,
    glossary: Glossary,
    kind: ParagraphKind,
) -> tuple[float, list[QualityFlag], list[TermUsage], list[str]]:
    """Return (score, flags, normalized terms_used, problem_messages)."""
    source = context.target.source
    target = output.translated_text or ""

    flags: list[QualityFlag] = []
    problems: list[str] = []

    # Term compliance
    term_score, terms_used, term_violations = _term_compliance(output, source, glossary)
    if term_violations:
        flags.append(QualityFlag(code="term_drift", detail="; ".join(term_violations)))
        problems.extend(term_violations)

    # Length ratio
    length_score, _ratio = _length_ratio_score(
        source, target, context.source_language, context.target_language
    )
    if length_score < 0.5:
        flags.append(
            QualityFlag(
                code="length_ratio_outlier",
                detail=f"ratio={_ratio:.2f}",
            )
        )
        problems.append(f"length ratio out of band: {_ratio:.2f}")

    # Anchors
    anchor_score, missing_anchors = _anchors_preserved(source, target)
    if missing_anchors:
        flags.append(
            QualityFlag(
                code="anchor_missing",
                detail=", ".join(missing_anchors),
            )
        )
        problems.append(
            f"missing anchors that must appear verbatim: {', '.join(missing_anchors)}"
        )

    # Refusal / untranslated residue
    no_refusal_score, refusal_issues = _refusal_or_residue(target)
    if refusal_issues:
        flags.append(QualityFlag(code="refusal_detected", detail=";".join(refusal_issues)))
        problems.append("translation contains refusal phrasing")

    # LLM self-confidence
    self_conf = max(0.0, min(1.0, output.confidence))

    # No residue: simple heuristic — if target language is zh, the translation should be
    # majority CJK characters; if too little CJK present, flag.
    residue_score = 1.0
    if context.target_language == "zh" and target:
        cjk = sum(1 for c in target if "\u4e00" <= c <= "\u9fff")
        if cjk / max(1, len(target)) < 0.3 and len(target) > 20:
            residue_score = 0.0
            flags.append(QualityFlag(code="untranslated_residue", detail="too few CJK chars"))
            problems.append("translation appears to contain large source-language residue")

    schema_score = 1.0  # parsed by pydantic == valid by definition

    score = (
        0.30 * schema_score
        + 0.25 * term_score
        + 0.15 * length_score
        + 0.10 * anchor_score
        + 0.10 * self_conf
        + 0.10 * min(no_refusal_score, residue_score)
    )

    if score < 0.7 and not any(f.code == "low_confidence" for f in flags):
        flags.append(QualityFlag(code="low_confidence", detail=f"score={score:.2f}"))

    return score, flags, terms_used, problems


__all__ = ["QualityFlagCode", "validate_translation"]
