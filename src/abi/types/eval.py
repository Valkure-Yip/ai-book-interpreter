"""Evaluation pipeline domain models.

All eval artifacts (baseline metadata, alignment, samples, judge scores, the
aggregated report) are persisted using these models so that re-loading and
diff'ing across runs is mechanical.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from abi.types._base import FrozenModel

JudgeDimension = Literal["adequacy", "fluency", "coherence", "style"]
JudgeVerdict = Literal["A", "B", "tie"]
System = Literal["abi", "baseline"]


class BaselineMeta(FrozenModel):
    """Metadata for a single baseline generation."""

    model: str
    base_url: str
    chunks: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    chunk_token_budget: int
    output_chars: int


class AlignedTriple(FrozenModel):
    """Source paragraph aligned with ABI + baseline translations.

    ``baseline_text`` may be empty if alignment failed for this index — that
    counts as a baseline completeness failure.
    """

    paragraph_id: str
    position: int
    section_id: str
    heading_trail: list[str]
    source_text: str
    abi_text: str
    baseline_text: str
    aligned: bool


class AlignmentReport(FrozenModel):
    """Whole-book alignment statistics."""

    strategy: Literal["positional", "soft", "failed"]
    source_paragraphs: int
    abi_paragraphs: int
    baseline_paragraphs: int
    aligned_pairs: int
    unaligned_pairs: int


class MechanicalScore(FrozenModel):
    """Mechanical quality numbers for one system (ABI or baseline).

    All scores are in [0, 1]. ``-1`` means "not applicable" (e.g. no anchors
    to check).
    """

    system: System
    n_paragraphs: int
    glossary_compliance: float = 0.0
    glossary_checked: int = 0
    glossary_violations: int = 0
    length_ratio_ok: float = 0.0
    length_ratio_mean: float = 0.0
    anchor_preservation: float = 0.0
    anchor_checked: int = 0
    completeness: float = 0.0
    completeness_missing: int = 0


class MechanicalReport(FrozenModel):
    abi: MechanicalScore
    baseline: MechanicalScore


class LikertScore(FrozenModel):
    """Likert scores assigned by a judge to ONE system for one sample.

    Each dimension is on 1-5 (5 = best). A negative value means "judge declined
    to score" and is excluded from aggregation.
    """

    adequacy: float
    fluency: float
    coherence: float
    style: float
    rationale: str = ""

    def mean(self) -> float:
        vals = [self.adequacy, self.fluency, self.coherence, self.style]
        return sum(vals) / len(vals)


class JudgeSampleResult(FrozenModel):
    """All judge outputs for one sampled paragraph."""

    paragraph_id: str
    position: int
    section_id: str
    abi_label: Literal["A", "B"]  # which side was ABI in the pairwise prompt
    likert_abi: LikertScore
    likert_baseline: LikertScore
    pairwise_verdict: JudgeVerdict
    pairwise_rationale: str = ""
    judge_model: str = ""
    judge_latency_ms: int = 0
    judge_cost_usd: float = 0.0


class JudgeAggregate(FrozenModel):
    """Aggregated judge scores across all samples."""

    samples: int
    likert_abi: dict[str, float] = Field(default_factory=dict)
    likert_baseline: dict[str, float] = Field(default_factory=dict)
    likert_delta: dict[str, float] = Field(default_factory=dict)
    pairwise_abi_wins: int = 0
    pairwise_baseline_wins: int = 0
    pairwise_ties: int = 0
    pairwise_abi_winrate: float = 0.0


class EvalConfig(FrozenModel):
    """User-controllable knobs for one eval invocation."""

    samples: int = 30
    judge_model: str | None = None
    judge_base_url: str | None = None
    baseline_chunk_tokens: int = 50_000
    skip_baseline: bool = False
    random_seed: int = 1729  # reproducible A/B label flipping + sampling


class EvalReport(FrozenModel):
    """Top-level eval artifact persisted to ``report.json``."""

    eval_id: str
    book_id: str
    abi_run_id: str
    created_at: datetime
    eval_config: EvalConfig
    source_paragraphs: int
    alignment: AlignmentReport
    baseline: BaselineMeta
    mechanical: MechanicalReport
    judge: JudgeAggregate
    judge_model: str
    notes: list[str] = Field(default_factory=list)
