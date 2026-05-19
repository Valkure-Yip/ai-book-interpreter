"""LLM output schemas for Pass 1. Kept local to the survey package."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from abi.types.survey import TermCandidate


class ChapterSummaryOutput(BaseModel):
    one_liner: str
    abstract: str
    key_points: list[str] = Field(default_factory=list)
    key_terms: list[TermCandidate] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class BookOverviewOutput(BaseModel):
    thesis: str
    target_audience: str = ""
    register: Literal[
        "academic-formal",
        "academic-accessible",
        "popular-science",
        "textbook",
        "literary",
    ] = "academic-formal"
    tone_notes: str = ""


class StyleGuideOutput(BaseModel):
    register_directives: list[str] = Field(default_factory=list)
    forbidden_patterns: list[str] = Field(default_factory=list)
    preferred_patterns: list[str] = Field(default_factory=list)


class GlossaryArbiterOutput(BaseModel):
    chosen_target: str
    rationale: str = ""
    rejected: list[dict[str, str]] = Field(default_factory=list)


class MindmapOutput(BaseModel):
    mermaid: str


class HeadingTranslationItem(BaseModel):
    """One row in the batch heading-translation response."""

    section_id: str
    translated: str


class HeadingTranslationsOutput(BaseModel):
    items: list[HeadingTranslationItem] = Field(default_factory=list)


class TocDetectorItem(BaseModel):
    """One detected chapter in the TOC-refinement response."""

    anchor_id: str
    title: str
    level: int = 2


class TocDetectorOutput(BaseModel):
    """LLM-reconstructed chapter structure.

    Each item points at one candidate (by ``anchor_id`` echoed from the prompt)
    that the model claims is a real chapter/section heading, with a cleaned
    title and a level (1=part, 2=chapter, 3=sub-section).
    """

    chapters: list[TocDetectorItem] = Field(default_factory=list)
