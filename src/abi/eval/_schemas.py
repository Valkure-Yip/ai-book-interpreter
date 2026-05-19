"""Pydantic schemas for evaluation-pipeline LLM agents."""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field

from abi.types._base import FrozenModel


class _SideScore(FrozenModel):
    adequacy: int = Field(ge=1, le=5)
    fluency: int = Field(ge=1, le=5)
    coherence: int = Field(ge=1, le=5)
    style: int = Field(ge=1, le=5)
    rationale: str = ""


class JudgeLikertOutput(FrozenModel):
    """2-way Likert (ABI vs Baseline). Used when no reference is available."""

    a: _SideScore
    b: _SideScore


class JudgeLikert3Output(FrozenModel):
    """3-way Likert (ABI / Baseline / Reference). Used when a reference exists."""

    a: _SideScore
    b: _SideScore
    c: _SideScore


class JudgePairwiseOutput(FrozenModel):
    """2-way pairwise verdict."""

    verdict: Literal["A", "B", "tie"]
    rationale: str = ""


class JudgePairwise3Output(FrozenModel):
    """3-way pairwise: three verdicts in one call.

    Each field compares one pair from {A, B, C}. The mapping from A/B/C to
    ``abi/baseline/reference`` is decided by the caller and randomized per
    sample to avoid positional bias.
    """

    a_vs_b: Literal["A", "B", "tie"]
    a_vs_c: Literal["A", "C", "tie"]
    b_vs_c: Literal["B", "C", "tie"]
    rationale: str = ""


class BaselineChunkOutput(FrozenModel):
    """Wrapper so we can use the structured-output path for baseline too.

    The model returns the translation as a single string; we strip whitespace
    and split on blank lines downstream.

    Some providers (deepseek-v4-flash et al.) tend to emit ``"translation"``
    or ``"text"`` instead of the declared field name. We accept those as
    aliases to keep the baseline robust without dropping the strict-schema
    safety net.
    """

    translated_text: str = Field(
        validation_alias=AliasChoices("translated_text", "translation", "text", "output"),
    )
