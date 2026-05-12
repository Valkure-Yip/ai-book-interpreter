"""Pass 1 (Survey) artifacts: chapter summaries, book overview, style guide."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from abi.types._base import FrozenModel


class TermCandidate(FrozenModel):
    """A term proposed by Pass 1 (or Pass 2) before glossary merge."""

    surface_form: str
    proposed_target: str
    definition: str = ""
    importance: Literal["core", "secondary", "peripheral"] = "secondary"
    first_surface_paragraph_id: str = ""


class ChapterSummary(FrozenModel):
    section_id: str
    heading: str
    one_liner: str
    abstract: str
    key_points: list[str] = Field(default_factory=list)
    key_terms: list[TermCandidate] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


Register = Literal[
    "academic-formal", "academic-accessible", "popular-science", "textbook", "unknown"
]


class BookOverview(FrozenModel):
    book_id: str
    title: str
    thesis: str
    target_audience: str = ""
    register: Register = "academic-formal"
    tone_notes: str = ""
    chapter_summaries: list[ChapterSummary] = Field(default_factory=list)
    mindmap_mermaid: str = ""


class StyleGuide(FrozenModel):
    book_id: str
    target_language: str
    register: Register = "academic-formal"
    register_directives: list[str] = Field(default_factory=list)
    forbidden_patterns: list[str] = Field(default_factory=list)
    preferred_patterns: list[str] = Field(default_factory=list)
    quote_style: str = "「」"
    number_style: Literal["arabic", "cjk-when-low", "preserve"] = "preserve"
