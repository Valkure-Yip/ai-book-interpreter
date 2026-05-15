"""Pydantic schemas for evaluation-pipeline LLM agents."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from abi.types._base import FrozenModel


class _SideScore(FrozenModel):
    adequacy: int = Field(ge=1, le=5)
    fluency: int = Field(ge=1, le=5)
    coherence: int = Field(ge=1, le=5)
    style: int = Field(ge=1, le=5)
    rationale: str = ""


class JudgeLikertOutput(FrozenModel):
    a: _SideScore
    b: _SideScore


class JudgePairwiseOutput(FrozenModel):
    verdict: Literal["A", "B", "tie"]
    rationale: str = ""


class BaselineChunkOutput(FrozenModel):
    """Wrapper so we can use the structured-output path for baseline too.

    The model returns the translation as a single string; we strip whitespace
    and split on blank lines downstream.
    """

    translated_text: str
