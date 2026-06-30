"""Tests for Pass 0.5 TOC refinement (abi.ir.toc_refiner)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from abi.ir import ingest
from abi.ir.toc_refiner import (
    TOCEntry,
    TOCResponse,
    _rebuild_book,
    extract_candidates,
    needs_refinement,
    refine_toc_with_llm,
)

MANIFESTO = Path(__file__).parent / "fixtures" / "the_communist_manifesto.txt"


# --- extract_candidates ---

def test_extract_candidates_basic() -> None:
    text = "\n".join([
        "CHAPTER ONE",
        "",
        "It was the best of times.",
        "It was the worst of times.",
        "",
        "CHAPTER TWO",
        "",
        "Some more text here.",
    ])
    cands = extract_candidates(text)
    texts = [c[1] for c in cands]
    assert "CHAPTER ONE" in texts
    assert "CHAPTER TWO" in texts
    # Prose lines with sentence-ending punctuation should not appear.
    assert "It was the best of times." not in texts
    assert "Some more text here." not in texts


def test_extract_candidates_filters_long_lines() -> None:
    long_line = "A" * 121
    text = f"\n{long_line}\n\nShort Line\n"
    cands = extract_candidates(text)
    texts = [c[1] for c in cands]
    assert long_line not in texts
    assert "Short Line" in texts


def test_extract_candidates_manifesto() -> None:
    text = MANIFESTO.read_text(encoding="utf-8")
    cands = extract_candidates(text)
    texts = [c[1] for c in cands]
    assert len(cands) > 5
    assert any("BOURGEOIS" in t for t in texts)


# --- needs_refinement ---

def test_needs_refinement_with_warning() -> None:
    book = MagicMock()
    book.toc = []
    assert needs_refinement(book, ["no_explicit_chapter_detected"]) is True


def test_needs_refinement_single_large_section() -> None:
    book = MagicMock()
    book.toc = [MagicMock()]
    book.iter_paragraphs.return_value = list(range(50))
    assert needs_refinement(book, []) is True


def test_no_refinement_multiple_sections() -> None:
    book = MagicMock()
    book.toc = [MagicMock(), MagicMock(), MagicMock()]
    assert needs_refinement(book, []) is False


# --- _rebuild_book ---

def test_rebuild_book_creates_sections() -> None:
    """Verify _rebuild_book assigns paragraphs to the correct sections."""
    raw = "\n".join([
        "Title page stuff",
        "",
        "I.",
        "FIRST CHAPTER",
        "",
        "Para one of chapter one.",
        "",
        "Para two of chapter one.",
        "",
        "II.",
        "SECOND CHAPTER",
        "",
        "Para one of chapter two.",
        "",
    ])
    _book, _warnings = ingest(Path(__file__).parent / "fixtures" / "short_book.txt")
    from abi.types.book import Book, BookMeta, Paragraph, Section

    paras = [
        Paragraph(paragraph_id="p1", section_id="s0", position=0,
                  kind="prose", source_text="Title page stuff"),
        Paragraph(paragraph_id="p2", section_id="s1", position=1,
                  kind="prose", source_text="Para one of chapter one."),
        Paragraph(paragraph_id="p3", section_id="s1", position=2,
                  kind="prose", source_text="Para two of chapter one."),
        Paragraph(paragraph_id="p4", section_id="s2", position=3,
                  kind="prose", source_text="Para one of chapter two."),
    ]
    fm = Section(
        section_id="s0", level=1, heading="Front Matter",
        heading_trail=["Front Matter"], paragraphs=paras, children=[],
    )
    meta = BookMeta(
        book_id="test", title="Test", authors=[], source_language="en",
        source_format="txt", source_path="test.txt", source_sha256="abc",
        detected_at=datetime.utcnow(),
    )
    test_book = Book(meta=meta, toc=[fm])

    entries = [
        TOCEntry(line_number=3, title="FIRST CHAPTER", level=1),
        TOCEntry(line_number=10, title="SECOND CHAPTER", level=1),
    ]
    rebuilt = _rebuild_book(test_book, raw, entries)
    assert len(rebuilt.toc) >= 2  # front matter + 2 chapters or just 2+
    titles = [s.heading for s in rebuilt.toc]
    assert "FIRST CHAPTER" in titles
    assert "SECOND CHAPTER" in titles


# --- refine_toc_with_llm ---

def test_refine_toc_with_llm_success() -> None:
    """Mock the LLM call and verify the refinement flow."""
    raw = "\n".join([
        "Some preamble.",
        "",
        "CHAPTER ONE",
        "",
        "Content of chapter one.",
        "",
        "CHAPTER TWO",
        "",
        "Content of chapter two.",
        "",
    ])
    from abi.types.book import Book, BookMeta, Paragraph, Section

    paras = [
        Paragraph(paragraph_id="p1", section_id="s0", position=0,
                  kind="prose", source_text="Some preamble."),
        Paragraph(paragraph_id="p2", section_id="s0", position=1,
                  kind="prose", source_text="Content of chapter one."),
        Paragraph(paragraph_id="p3", section_id="s0", position=2,
                  kind="prose", source_text="Content of chapter two."),
    ]
    fm = Section(
        section_id="s0", level=1, heading="Front Matter",
        heading_trail=["Front Matter"], paragraphs=paras, children=[],
    )
    meta = BookMeta(
        book_id="test", title="Test", authors=[], source_language="en",
        source_format="txt", source_path="test.txt", source_sha256="abc",
        detected_at=datetime.utcnow(),
    )
    book = Book(meta=meta, toc=[fm])

    # Mock router: return structured TOCResponse.
    cands = extract_candidates(raw)
    # Find the line numbers for CHAPTER ONE and CHAPTER TWO.
    ch1_line = next(ln for ln, t in cands if "CHAPTER ONE" in t)
    ch2_line = next(ln for ln, t in cands if "CHAPTER TWO" in t)

    mock_response = TOCResponse(chapters=[
        TOCEntry(line_number=ch1_line, title="CHAPTER ONE", level=1),
        TOCEntry(line_number=ch2_line, title="CHAPTER TWO", level=1),
    ])
    mock_router = MagicMock()
    mock_router.invoke_structured = AsyncMock(return_value=(mock_response, MagicMock()))

    refined = asyncio.run(refine_toc_with_llm(book, raw, router=mock_router))

    assert len(refined.toc) >= 2
    titles = [s.heading for s in refined.toc]
    assert "CHAPTER ONE" in titles
    assert "CHAPTER TWO" in titles


def test_refine_toc_with_llm_failure_returns_original() -> None:
    """If LLM fails, the original book is returned unchanged."""
    from abi.types.book import Book, BookMeta, Section

    fm = Section(
        section_id="s0", level=1, heading="Front Matter",
        heading_trail=["Front Matter"], paragraphs=[], children=[],
    )
    meta = BookMeta(
        book_id="test", title="Test", authors=[], source_language="en",
        source_format="txt", source_path="test.txt", source_sha256="abc",
        detected_at=datetime.utcnow(),
    )
    book = Book(meta=meta, toc=[fm])

    mock_router = MagicMock()
    mock_router.invoke_structured = AsyncMock(side_effect=RuntimeError("LLM down"))

    result = asyncio.run(refine_toc_with_llm(book, "some text\n", router=mock_router))
    assert result is book  # unchanged
