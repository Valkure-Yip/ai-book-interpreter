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
        assert any(
            w.startswith("chapter_selection_applied:kept_2_of_4_chapters")
            for w in warnings
        )
        assert any(w.startswith("chapter_selection_dropped:") for w in warnings)

    def test_synthetic_front_matter_is_skipped_in_indexing(self) -> None:
        """``--chapters 1`` must pick the FIRST real chapter, not "Front Matter".

        Regression test for the Manifesto bug where the LLM-refined toc looked
        like ``[Front Matter, I. Bourgeois, II. ..., III. ..., IV. ...]`` and
        a user typing ``--chapters 1`` got the unnamed preamble.
        """
        book = _book(["Front Matter", "I. Bourgeois", "II. Proletarians"])
        out, warnings = filter_book_by_chapters(book, {1})
        # toc[1] (I. Bourgeois) is selected, Front Matter is dropped entirely.
        assert [s.heading for s in out.toc] == ["I. Bourgeois"]
        assert any("kept_1_of_2_chapters" in w for w in warnings)
        assert any("Front Matter" in w for w in warnings if "dropped" in w)

    def test_front_matter_is_not_indexable_at_high_indices_either(self) -> None:
        book = _book(["Front Matter", "I", "II", "III"])
        out, _ = filter_book_by_chapters(book, {3})
        # Real-chapter index 3 = "III" (Front Matter doesn't count).
        assert [s.heading for s in out.toc] == ["III"]

    def test_out_of_range_indices_are_warned(self) -> None:
        book = _book(["A", "B"])
        out, warnings = filter_book_by_chapters(book, {1, 99})
        assert [s.heading for s in out.toc] == ["A"]
        assert any("out_of_range" in w for w in warnings)
        # The OOR diagnostic should reference the count of REAL chapters,
        # not the raw toc length (so users see "book_has_2_chapters" even if
        # toc happens to include a Front Matter wrapper).
        assert any("book_has_2_chapters" in w for w in warnings)

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
