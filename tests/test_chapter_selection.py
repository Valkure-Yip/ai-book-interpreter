"""Regression tests for ``_select_chapters_for_summary``.

These are pure-logic tests — no LLM, no IO. They lock down the rule that we
prefer leaf chapters over synthetic wrapper sections (a real TXT-ingestion
shape we observed on full-length academic books).
"""

from __future__ import annotations

from datetime import datetime

from abi.survey.pipeline import _select_chapters_for_summary
from abi.types.book import Book, BookMeta, Paragraph, Section


def _para(pid: str, sid: str, pos: int, text: str = "lorem ipsum") -> Paragraph:
    return Paragraph(
        paragraph_id=pid,
        kind="prose",
        source_text=text,
        position=pos,
        section_id=sid,
    )


def _section(
    sid: str,
    *,
    level: int,
    heading: str,
    trail: list[str],
    n_paragraphs: int = 0,
    children: list[Section] | None = None,
) -> Section:
    paragraphs = [_para(f"{sid}-p{i:04d}", sid, i) for i in range(n_paragraphs)]
    return Section(
        section_id=sid,
        level=level,
        heading=heading,
        heading_trail=trail,
        paragraphs=paragraphs,
        children=children or [],
    )


def _book(toc: list[Section]) -> Book:
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
        toc=toc,
    )


class TestSelectChaptersForSummary:
    def test_flat_book_takes_each_top_level_chapter(self) -> None:
        toc = [
            _section("s1", level=1, heading="Ch 1", trail=["Ch 1"], n_paragraphs=10),
            _section("s2", level=1, heading="Ch 2", trail=["Ch 2"], n_paragraphs=10),
            _section("s3", level=1, heading="Ch 3", trail=["Ch 3"], n_paragraphs=10),
        ]
        result = _select_chapters_for_summary(_book(toc))
        assert [s.section_id for s in result] == ["s1", "s2", "s3"]

    def test_wrapper_with_substantial_children_is_skipped(self) -> None:
        """Real TXT-ingestion shape: synthetic 'Front Matter' wraps the actual chapters."""
        children = [
            _section("c1", level=3, heading="1 Hesiod", trail=["Front Matter", "1 Hesiod"], n_paragraphs=26),
            _section("c2", level=3, heading="2 Cap",    trail=["Front Matter", "2 Cap"],    n_paragraphs=33),
            _section("c3", level=3, heading="3 Cloud",  trail=["Front Matter", "3 Cloud"],  n_paragraphs=35),
        ]
        wrapper = _section(
            "wrap", level=1, heading="Front Matter", trail=["Front Matter"],
            n_paragraphs=9, children=children,
        )
        result = _select_chapters_for_summary(_book([wrapper]))
        assert [s.section_id for s in result] == ["c1", "c2", "c3"]
        assert "wrap" not in {s.section_id for s in result}

    def test_leaf_with_trivial_children_is_kept(self) -> None:
        """If children carry only ~1 paragraph each (e.g. stub headings), keep the parent."""
        children = [
            _section("trivial", level=3, heading="Note", trail=["A", "Note"], n_paragraphs=1),
        ]
        parent = _section(
            "parent", level=2, heading="A", trail=["A"], n_paragraphs=20, children=children,
        )
        result = _select_chapters_for_summary(_book([parent]))
        assert {s.section_id for s in result} == {"parent", "trivial"}

    def test_empty_sections_are_dropped(self) -> None:
        toc = [
            _section("empty", level=1, heading="x", trail=["x"], n_paragraphs=0),
            _section("real",  level=1, heading="y", trail=["y"], n_paragraphs=5),
        ]
        result = _select_chapters_for_summary(_book(toc))
        assert [s.section_id for s in result] == ["real"]

    def test_dedup_by_section_id(self) -> None:
        """A section appearing twice in walk order is only emitted once."""
        shared = _section("shared", level=2, heading="x", trail=["x"], n_paragraphs=5)
        toc = [shared, shared]
        result = _select_chapters_for_summary(_book(toc))
        assert [s.section_id for s in result] == ["shared"]
