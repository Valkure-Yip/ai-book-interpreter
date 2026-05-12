"""Unit tests for sliding-window context construction."""

from __future__ import annotations

from datetime import datetime

import pytest

from abi.translate.context_builder import build_context, extract_anchors
from abi.types.book import Book, BookMeta, Paragraph, Section
from abi.types.glossary import Glossary, GlossaryEntry
from abi.types.ids import book_id, paragraph_id, section_id
from abi.types.run import WindowConfig
from abi.types.survey import BookOverview, StyleGuide
from abi.types.translation import (
    ContextWindowMeta,
    TranslationUnit,
)


def _para(text: str, position: int, section_id_: str) -> Paragraph:
    return Paragraph(
        paragraph_id=paragraph_id(text, position),
        kind="prose",
        source_text=text,
        position=position,
        section_id=section_id_,
    )


def _make_book() -> Book:
    s1_id = section_id(["Chapter 1"])
    s2_id = section_id(["Chapter 2"])
    paragraphs_s1 = [_para(f"S1 para {i}", i, s1_id) for i in range(5)]
    paragraphs_s2 = [_para(f"S2 para {i}", i + 5, s2_id) for i in range(5)]
    s1 = Section(
        section_id=s1_id,
        level=1,
        heading="Chapter 1",
        heading_trail=["Chapter 1"],
        paragraphs=paragraphs_s1,
    )
    s2 = Section(
        section_id=s2_id,
        level=1,
        heading="Chapter 2",
        heading_trail=["Chapter 2"],
        paragraphs=paragraphs_s2,
    )
    meta = BookMeta(
        book_id=book_id(b"test"),
        title="Test",
        authors=["t"],
        source_language="en",
        source_format="txt",
        source_path="/tmp/test.txt",
        source_sha256="x" * 64,
        detected_at=datetime.utcnow(),
    )
    return Book(meta=meta, toc=[s1, s2])


def _make_glossary() -> Glossary:
    return Glossary(
        book_id="b",
        target_language="zh",
        entries=[
            GlossaryEntry(
                term="embodiment",
                surface_forms=["embodiment"],
                target="具身",
                definition="...",
                is_core=True,
            )
        ],
    )


def _make_overview() -> BookOverview:
    return BookOverview(
        book_id="b",
        title="Test",
        thesis="thesis",
        target_audience="researchers",
        register="academic-formal",
        tone_notes="",
        chapter_summaries=[],
    )


def _make_style() -> StyleGuide:
    return StyleGuide(
        book_id="b",
        target_language="zh",
        register="academic-formal",
        register_directives=["d1"],
        quote_style="「」",
    )


class TestContextBuilder:
    def test_window_basic(self) -> None:
        book = _make_book()
        all_paras = book.iter_paragraphs()
        target = all_paras[2]  # 3rd paragraph of chapter 1
        ctx = build_context(
            book=book, target=target,
            paragraphs_in_order=all_paras,
            translations={},
            glossary=_make_glossary(),
            overview=_make_overview(),
            style_guide=_make_style(),
            chapter_summary=None,
            window_config=WindowConfig(before=2, after=2, glossary_max=10),
        )
        assert len(ctx.prev_window) == 2
        assert len(ctx.next_window) == 2
        assert ctx.target.id == target.paragraph_id
        assert not ctx.crossed_chapter_boundary

    def test_window_crosses_chapter_boundary(self) -> None:
        book = _make_book()
        all_paras = book.iter_paragraphs()
        target = all_paras[5]  # first para of chapter 2
        ctx = build_context(
            book=book, target=target,
            paragraphs_in_order=all_paras,
            translations={},
            glossary=_make_glossary(),
            overview=_make_overview(),
            style_guide=_make_style(),
            chapter_summary=None,
            window_config=WindowConfig(before=3, after=2, glossary_max=10),
        )
        assert ctx.crossed_chapter_boundary
        # All 3 prior come from chapter 1
        assert len(ctx.prev_window) == 3

    def test_prev_window_includes_translations(self) -> None:
        book = _make_book()
        all_paras = book.iter_paragraphs()
        target = all_paras[2]
        # mark para 1 as translated
        prev = all_paras[1]
        unit = TranslationUnit(
            paragraph_id=prev.paragraph_id,
            kind="prose",
            source_text=prev.source_text,
            translated_text="已译前文",
            target_language="zh",
            context_window=ContextWindowMeta(
                k_before=0, j_after=0, glossary_size=0, glossary_version=1
            ),
        )
        translations = {prev.paragraph_id: unit}
        ctx = build_context(
            book=book, target=target,
            paragraphs_in_order=all_paras,
            translations=translations,
            glossary=_make_glossary(),
            overview=_make_overview(),
            style_guide=_make_style(),
            chapter_summary=None,
            window_config=WindowConfig(before=2, after=0, glossary_max=10),
        )
        # The translation should be in prev_window
        match = [p for p in ctx.prev_window if p.id == prev.paragraph_id]
        assert match
        assert match[0].translated == "已译前文"

    def test_glossary_slice_must_include_present_terms(self) -> None:
        book = _make_book()
        all_paras = book.iter_paragraphs()
        target = all_paras[2]
        # Inject "embodiment" into the source text via a new paragraph
        target = target.model_copy(update={"source_text": "Discussion of embodiment."})
        ctx = build_context(
            book=book, target=target,
            paragraphs_in_order=all_paras,
            translations={},
            glossary=_make_glossary(),
            overview=_make_overview(),
            style_guide=_make_style(),
            chapter_summary=None,
            window_config=WindowConfig(before=1, after=1, glossary_max=5),
        )
        assert any(e.term == "embodiment" for e in ctx.glossary_slice)

    def test_anchor_extraction(self) -> None:
        text = "As shown in [12] and Figure 3.1, the claim follows from Eq. (2.4)."
        anchors = extract_anchors(text)
        assert "[12]" in anchors
        assert any("Figure 3.1" in a for a in anchors)
        assert any("Eq" in a for a in anchors)

    def test_empty_anchors(self) -> None:
        assert extract_anchors("simple prose") == []


@pytest.mark.parametrize("position", [0, 1, 2, 3, 4])
def test_window_at_boundaries_does_not_crash(position: int) -> None:
    book = _make_book()
    all_paras = book.iter_paragraphs()
    target = all_paras[position]
    ctx = build_context(
        book=book, target=target,
        paragraphs_in_order=all_paras,
        translations={},
        glossary=_make_glossary(),
        overview=_make_overview(),
        style_guide=_make_style(),
        chapter_summary=None,
        window_config=WindowConfig(before=3, after=3, glossary_max=10),
    )
    assert ctx.target.id == target.paragraph_id
