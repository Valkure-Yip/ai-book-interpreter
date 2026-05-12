"""Pass 2 (Translate) artifacts: TranslationUnit and friends."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from abi.types._base import FrozenModel
from abi.types.book import ParagraphKind

QualityFlagCode = Literal[
    "term_drift",
    "length_ratio_outlier",
    "schema_error",
    "refusal_detected",
    "low_confidence",
    "untranslated_residue",
    "anchor_missing",
    "passthrough",
    "skipped",
]


class QualityFlag(FrozenModel):
    code: QualityFlagCode
    detail: str = ""


class TermUsage(FrozenModel):
    term: str
    rendered_as: str
    compliant: bool


class TokenUsage(FrozenModel):
    input: int = 0
    output: int = 0
    cached: int = 0


class ContextWindowMeta(FrozenModel):
    k_before: int
    j_after: int
    glossary_size: int
    crossed_chapter_boundary: bool = False
    trimmed_reasons: list[str] = Field(default_factory=list)
    prompt_hash: str = ""
    token_budget: int = 0
    glossary_version: int = 1


class TranslationUnit(FrozenModel):
    paragraph_id: str
    kind: ParagraphKind
    source_text: str
    translated_text: str
    target_language: str

    terms_used: list[TermUsage] = Field(default_factory=list)
    confidence: float = 0.0
    flags: list[QualityFlag] = Field(default_factory=list)
    notes: str = ""

    prompt_version: str = ""
    model: str = ""
    provider: str = "openai-compatible"
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float = 0.0
    latency_ms: int = 0
    retries: int = 0

    context_window: ContextWindowMeta | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ParagraphTranslationOutput(FrozenModel):
    """The structured output we expect from the ParagraphTranslator LLM call."""

    translated_text: str
    terms_used: list[TermUsage] = Field(default_factory=list)
    confidence: float = 0.8
    notes: str = ""
    untranslated_passthrough: bool = False


class BatchTranslationItem(FrozenModel):
    """One paragraph's translation inside a batch response.

    Identical to ``ParagraphTranslationOutput`` plus a ``paragraph_id`` so the
    caller can match items back to inputs (LLM is allowed to reorder).
    """

    paragraph_id: str
    translated_text: str
    terms_used: list[TermUsage] = Field(default_factory=list)
    confidence: float = 0.8
    notes: str = ""
    untranslated_passthrough: bool = False


class BatchTranslationOutput(FrozenModel):
    items: list[BatchTranslationItem] = Field(default_factory=list)


class SkipRecord(FrozenModel):
    paragraph_id: str
    reason: Literal["passthrough", "filtered", "error"]
    detail: str = ""
