"""Unit tests for baseline ↔ ABI paragraph alignment."""

from __future__ import annotations

from datetime import datetime

from abi.eval.alignment import _soft_align, align
from abi.types.book import Book, BookMeta, Paragraph, Section
from abi.types.translation import TranslationUnit


def _para(pid: str, text: str, pos: int, sid: str) -> Paragraph:
    return Paragraph(
        paragraph_id=pid,
        kind="prose",
        source_text=text,
        position=pos,
        section_id=sid,
    )


def _book(paras: list[Paragraph]) -> Book:
    section = Section(
        section_id="sec1",
        level=1,
        heading="Ch1",
        heading_trail=["Ch1"],
        paragraphs=paras,
    )
    return Book(
        meta=BookMeta(
            book_id="b1",
            title="t",
            source_format="txt",
            source_path="/x",
            source_sha256="0" * 64,
            detected_at=datetime.utcnow(),
        ),
        toc=[section],
    )


def _unit(pid: str, text: str) -> TranslationUnit:
    return TranslationUnit(
        paragraph_id=pid,
        kind="prose",
        source_text="",
        translated_text=text,
        target_language="zh",
    )


class TestPositionalAlignment:
    def test_equal_lengths_zip_directly(self) -> None:
        book = _book(
            [
                _para("p1", "Hello world.", 0, "sec1"),
                _para("p2", "Another paragraph.", 1, "sec1"),
                _para("p3", "Third one.", 2, "sec1"),
            ]
        )
        units = {
            "p1": _unit("p1", "abi-1"),
            "p2": _unit("p2", "abi-2"),
            "p3": _unit("p3", "abi-3"),
        }
        baseline_paras = ["base-1", "base-2", "base-3"]
        triples, report = align(book=book, units=units, baseline_paragraphs=baseline_paras)
        assert report.strategy == "positional"
        assert report.aligned_pairs == 3
        assert triples[0].abi_text == "abi-1" and triples[0].baseline_text == "base-1"
        assert triples[2].baseline_text == "base-3"
        assert all(t.aligned for t in triples)


class TestSoftAlignment:
    def test_soft_when_off_by_one(self) -> None:
        # 10 source paragraphs, baseline has 9 (LLM merged two adjacent ones).
        paras = [
            _para(f"p{i}", "x" * 100, i, "sec1") for i in range(10)
        ]
        book = _book(paras)
        units = {p.paragraph_id: _unit(p.paragraph_id, "abi") for p in paras}
        baseline = ["base"] * 9  # 9 != 10 but within 10% threshold
        _, report = align(book=book, units=units, baseline_paragraphs=baseline)
        # 10 % 10 → soft alignment kicks in
        assert report.strategy == "soft"

    def test_soft_align_position_based(self) -> None:
        """Cross-language pairs should align by relative position, not raw length.

        Source is English (long), baseline is its Chinese rendering (~0.3×).
        With 4 source and 4 baseline paragraphs of corresponding sizes the
        identity mapping must be recovered.
        """
        # English lengths
        src = [100, 200, 150, 50]
        # Chinese rendering, ~0.3× each
        base = [30, 60, 45, 15]
        mapping = _soft_align(src, base)
        assert mapping == [0, 1, 2, 3]

    def test_soft_align_handles_extra_baseline_paragraph(self) -> None:
        """Baseline adding a 5th paragraph: first 4 still align positionally."""
        src = [100, 200, 150, 50]
        base = [30, 60, 45, 15, 5]
        mapping = _soft_align(src, base)
        # All four source paragraphs should find a match.
        assert all(m >= 0 for m in mapping)


class TestFailedAlignment:
    def test_huge_mismatch_returns_failed(self) -> None:
        paras = [_para(f"p{i}", "x" * 100, i, "sec1") for i in range(10)]
        book = _book(paras)
        units = {p.paragraph_id: _unit(p.paragraph_id, "abi") for p in paras}
        baseline = ["one big blob of text"]  # 1 vs 10 → way off
        triples, report = align(book=book, units=units, baseline_paragraphs=baseline)
        assert report.strategy == "failed"
        assert all(not t.aligned for t in triples)
        assert all(t.baseline_text == "" for t in triples)


class TestEmptyBook:
    def test_zero_source_paragraphs(self) -> None:
        book = _book([])
        triples, report = align(book=book, units={}, baseline_paragraphs=[])
        assert triples == []
        assert report.strategy == "failed"
        assert report.source_paragraphs == 0
