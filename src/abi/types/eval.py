"""Evaluation pipeline domain models.

All eval artifacts (baseline metadata, alignment, samples, judge scores, the
aggregated report) are persisted using these models so that re-loading and
diff'ing across runs is mechanical.

Three-way comparison
--------------------

When a human reference is available (e.g. from a HF dataset adapter like
``google/wmt24pp``), the eval pipeline scores ABI / baseline / reference
side by side. Pairwise judgments produce three winrates: ABI vs Baseline,
ABI vs Reference, Baseline vs Reference. ``has_reference`` on the aligned
tuple signals which path is in use.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from abi.types._base import FrozenModel

JudgeDimension = Literal["adequacy", "fluency", "coherence", "style"]
JudgeVerdict = Literal["A", "B", "tie"]
System = Literal["abi", "baseline", "reference"]


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
    """Source paragraph aligned with ABI + baseline (+ optional human reference).

    ``baseline_text`` may be empty if alignment failed for this index — that
    counts as a baseline completeness failure. ``reference_text`` is filled in
    when the dataset adapter provides human references; ``has_reference``
    flips on accordingly so judge/metrics paths can branch cleanly.

    The legacy name ``AlignedTriple`` is kept (rather than renamed
    ``AlignedTuple``) so older eval artifacts on disk still validate.
    """

    paragraph_id: str
    position: int
    section_id: str
    heading_trail: list[str]
    source_text: str
    abi_text: str
    baseline_text: str
    aligned: bool
    reference_text: str = ""
    has_reference: bool = False
    document_id: str = ""


class AlignmentReport(FrozenModel):
    """Whole-book alignment statistics."""

    strategy: Literal["positional", "soft", "failed"]
    source_paragraphs: int
    abi_paragraphs: int
    baseline_paragraphs: int
    aligned_pairs: int
    unaligned_pairs: int
    reference_paragraphs: int = 0


class MechanicalScore(FrozenModel):
    """Mechanical quality numbers for one system (ABI / baseline / reference).

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
    reference: MechanicalScore | None = None


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
    """All judge outputs for one sampled paragraph.

    Two paths:

    - 2-way (legacy, no reference): ``likert_abi`` + ``likert_baseline`` and a
      single ``pairwise_verdict`` covering ABI vs Baseline.
    - 3-way (dataset-driven): all three Likert blocks filled and three
      pairwise verdicts (``pairwise_abi_vs_ref`` / ``pairwise_baseline_vs_ref``
      in addition to ``pairwise_verdict`` = ABI vs Baseline).

    ``label_mapping`` records which slot (A/B/C in the 3-way prompt) each
    system occupied, so de-anonymization at aggregate time is mechanical.
    """

    paragraph_id: str
    position: int
    section_id: str
    abi_label: Literal["A", "B", "C"]
    likert_abi: LikertScore
    likert_baseline: LikertScore
    likert_reference: LikertScore | None = None
    pairwise_verdict: JudgeVerdict
    pairwise_rationale: str = ""
    pairwise_abi_vs_ref: JudgeVerdict | None = None
    pairwise_baseline_vs_ref: JudgeVerdict | None = None
    label_mapping: dict[str, Literal["abi", "baseline", "reference"]] = Field(
        default_factory=dict
    )
    judge_model: str = ""
    judge_latency_ms: int = 0
    judge_cost_usd: float = 0.0
    langfuse_trace_id: str = ""


class JudgeAggregate(FrozenModel):
    """Aggregated judge scores across all samples.

    ``pairwise_*_winrate`` interprets ties as 0.5 (chess scoring) so a single
    number in [0, 1] always means "this is better than that". When no
    reference is available the reference-related fields stay at their
    defaults (0/empty).
    """

    samples: int
    likert_abi: dict[str, float] = Field(default_factory=dict)
    likert_baseline: dict[str, float] = Field(default_factory=dict)
    likert_reference: dict[str, float] = Field(default_factory=dict)
    likert_delta: dict[str, float] = Field(default_factory=dict)
    # ABI vs Baseline (default path).
    pairwise_abi_wins: int = 0
    pairwise_baseline_wins: int = 0
    pairwise_ties: int = 0
    pairwise_abi_winrate: float = 0.0
    # ABI vs Reference.
    pairwise_abi_vs_ref_wins: int = 0
    pairwise_abi_vs_ref_losses: int = 0
    pairwise_abi_vs_ref_ties: int = 0
    pairwise_abi_vs_ref_winrate: float = 0.0
    # Baseline vs Reference.
    pairwise_baseline_vs_ref_wins: int = 0
    pairwise_baseline_vs_ref_losses: int = 0
    pairwise_baseline_vs_ref_ties: int = 0
    pairwise_baseline_vs_ref_winrate: float = 0.0


class EvalConfig(FrozenModel):
    """User-controllable knobs for one eval invocation.

    ``judge_model`` overrides ``LLM_MODEL`` only for the judge calls. The
    judge endpoint and API key are always reused from the main LLM config —
    the eval pipeline does NOT spin up a second LLM router, it just passes
    a per-call model override into the existing one.

    ``dataset_spec``, ``limit_docs``, ``auto_translate`` drive the
    HF-dataset path. When ``dataset_spec`` is set the pipeline pulls source +
    human references from the adapter instead of the local book file.
    """

    samples: int = 30
    judge_model: str | None = None
    baseline_chunk_tokens: int = 50_000
    skip_baseline: bool = False
    random_seed: int = 1729
    # Dataset / three-way config.
    dataset_spec: str | None = None
    limit_docs: int | None = None
    auto_translate: bool = False
    # Langfuse experiment integration.
    langfuse_experiment: bool = True
    langfuse_dataset_name: str | None = None


class EvalReport(FrozenModel):
    """Top-level eval artifact persisted to ``report.json``."""

    eval_id: str
    book_id: str
    abi_run_id: str
    created_at: datetime
    eval_config: EvalConfig
    source_paragraphs: int
    reference_paragraphs: int = 0
    alignment: AlignmentReport
    baseline: BaselineMeta
    mechanical: MechanicalReport
    judge: JudgeAggregate
    judge_model: str
    translate_model: str = ""
    dataset_spec: str | None = None
    notes: list[str] = Field(default_factory=list)
    langfuse_dataset_name: str | None = None
    langfuse_dataset_run_id: str | None = None
    langfuse_dataset_run_url: str | None = None
