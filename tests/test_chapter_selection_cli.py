"""Tests for ``parse_chapter_selection`` and ``filter_book_by_chapters``."""

from __future__ import annotations

from datetime import datetime

import pytest

from abi.runtime.selection import filter_book_by_chapters, parse_chapter_selection
from abi.types.book import Book, BookMeta, Section


def _section(sid: str, heading: str) -> Section:
    return Section(
        section_id=sid,
        level=1,
        heading=heading,
        heading_trail=[heading],
        paragraphs=[],
        children=[],
    )


def _book(headings: list[str]) -> Book:
    return Book(
        meta=BookMeta(
            book_id="b" * 12,
            title="t",
            source_language="en",
            source_format="txt",
            source_path="/tmp/t",
            source_sha256="0" * 64,
            detected_at=datetime(2026, 1, 1),
        ),
        toc=[_section(f"s{i}", h) for i, h in enumerate(headings)],
    )


class TestParseChapterSelection:
    def test_empty_returns_empty_set(self) -> None:
        assert parse_chapter_selection("") == set()
        assert parse_chapter_selection("   ") == set()

    def test_single_number(self) -> None:
        assert parse_chapter_selection("3") == {3}

    def test_comma_list(self) -> None:
        assert parse_chapter_selection("1,3,5") == {1, 3, 5}

    def test_range(self) -> None:
        assert parse_chapter_selection("2-5") == {2, 3, 4, 5}

    def test_mixed(self) -> None:
        assert parse_chapter_selection("1,3-5,8") == {1, 3, 4, 5, 8}

    def test_whitespace_tolerated(self) -> None:
        assert parse_chapter_selection(" 1 , 3 - 5 ") == {1, 3, 4, 5}

    def test_dedup_overlap(self) -> None:
        assert parse_chapter_selection("1-3,2-4") == {1, 2, 3, 4}

    def test_zero_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_chapter_selection("0")

    def test_inverted_range_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_chapter_selection("5-3")

    def test_non_numeric_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_chapter_selection("a,b")


class TestFilterBookByChapters:
    def test_empty_selection_returns_unchanged(self) -> None:
        book = _book(["A", "B", "C"])
        out, warnings = filter_book_by_chapters(book, set())
        assert [s.heading for s in out.toc] == ["A", "B", "C"]
        assert warnings == []

    def test_keeps_selected_drops_rest(self) -> None:
        book = _book(["A", "B", "C", "D"])
        out, warnings = filter_book_by_chapters(book, {1, 3})
        assert [s.heading for s in out.toc] == ["A", "C"]
        assert any(w.startswith("chapter_selection_applied:kept_2_of_4") for w in warnings)
        assert any(w.startswith("chapter_selection_dropped:") for w in warnings)

    def test_out_of_range_indices_are_warned(self) -> None:
        book = _book(["A", "B"])
        out, warnings = filter_book_by_chapters(book, {1, 99})
        assert [s.heading for s in out.toc] == ["A"]
        assert any("out_of_range" in w for w in warnings)

    def test_all_invalid_keeps_full_toc(self) -> None:
        book = _book(["A", "B"])
        out, warnings = filter_book_by_chapters(book, {99, 100})
        assert [s.heading for s in out.toc] == ["A", "B"]
        assert any("yielded_empty_kept_full_toc" in w for w in warnings)

    def test_preserves_meta(self) -> None:
        book = _book(["A", "B", "C"])
        out, _ = filter_book_by_chapters(book, {2})
        assert out.meta.book_id == book.meta.book_id
        assert out.meta.title == book.meta.title
