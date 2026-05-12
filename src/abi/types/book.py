"""Book IR — the unified representation produced by Pass 0."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from abi.types._base import FrozenModel

ParagraphKind = Literal[
    "prose",
    "heading",
    "quote",
    "list_item",
    "code",
    "equation",
    "figure_caption",
    "table_cell",
    "footnote",
    "citation",
]

SourceFormat = Literal["txt", "epub", "pdf"]


class Anchor(FrozenModel):
    """Inline reference inside a paragraph (footnote ref, citation, figure, etc.)."""

    kind: Literal["footnote", "citation", "figure", "section", "url"]
    target: str
    span: tuple[int, int]
    label: str | None = None


class BookMeta(FrozenModel):
    book_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    source_language: str = "en"
    source_format: SourceFormat
    source_path: str
    source_sha256: str
    detected_at: datetime
    notes: list[str] = Field(default_factory=list)


class Paragraph(FrozenModel):
    paragraph_id: str
    kind: ParagraphKind
    source_text: str
    position: int
    section_id: str
    anchors: list[Anchor] = Field(default_factory=list)
    attrs: dict[str, str] = Field(default_factory=dict)


class Section(FrozenModel):
    section_id: str
    level: int
    heading: str
    heading_trail: list[str]
    paragraphs: list[Paragraph] = Field(default_factory=list)
    children: list[Section] = Field(default_factory=list)


class Book(FrozenModel):
    meta: BookMeta
    toc: list[Section]
    footnotes: dict[str, Paragraph] = Field(default_factory=dict)
    references: list[Paragraph] = Field(default_factory=list)

    def iter_paragraphs(self) -> list[Paragraph]:
        """DFS over toc returning paragraphs in book order."""
        out: list[Paragraph] = []

        def walk(s: Section) -> None:
            out.extend(s.paragraphs)
            for c in s.children:
                walk(c)

        for s in self.toc:
            walk(s)
        return out

    def iter_sections(self) -> list[Section]:
        out: list[Section] = []

        def walk(s: Section) -> None:
            out.append(s)
            for c in s.children:
                walk(c)

        for s in self.toc:
            walk(s)
        return out
