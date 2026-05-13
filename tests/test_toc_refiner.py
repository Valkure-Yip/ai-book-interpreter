"""Unit tests for the TOC refiner.

These tests cover the pure-logic pieces (candidate extraction, book rebuild)
WITHOUT invoking the LLM. The LLM-driven happy path is exercised separately
in ``tests/test_e2e_pipeline.py`` via the mock dispatcher.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from abi.survey.toc_refiner import (
    _flatten_to_nodes,
    _is_strong_heading,
    _looks_like_heading,
    _rebuild_book,
    _select_candidates,
)
from abi.types.book import Book, BookMeta, Paragraph, Section


def _para(pid: str, text: str, sid: str, position: int = 0) -> Paragraph:
    return Paragraph(
        paragraph_id=pid,
        kind="prose",
        source_text=text,
        position=position,
        section_id=sid,
        anchors=[],
        attrs={},
    )


def _section(sid: str, heading: str, paragraphs: list[Paragraph], level: int = 1,
             children: list[Section] | None = None) -> Section:
    return Section(
        section_id=sid,
        level=level,
        heading=heading,
        heading_trail=[heading],
        paragraphs=paragraphs,
        children=children or [],
    )


def _book(toc: list[Section]) -> Book:
    return Book(
        meta=BookMeta(
            book_id="b" * 12,
            title="Test Book",
            source_language="en",
            source_format="txt",
            source_path="/tmp/t",
            source_sha256="0" * 64,
            detected_at=datetime(2026, 1, 1),
        ),
        toc=toc,
    )


# --------------------------------------------------------------------------- #
# Heuristic filters
# --------------------------------------------------------------------------- #

class TestStrongHeadingPatterns:
    @pytest.mark.parametrize(
        "text",
        [
            "I.",
            "III",
            "Chapter 3",
            "Chapter 12: Power",
            "第三章",
            "第三部分",
            "PART II",
            "1 Grow",
            "1.2 Background",
            "I. BOURGEOIS AND PROLETARIANS",
        ],
    )
    def test_matches(self, text: str) -> None:
        assert _is_strong_heading(text)

    @pytest.mark.parametrize(
        "text",
        [
            "This is a normal sentence about Chapter 3.",
            "Hello world",
            "",
        ],
    )
    def test_rejects(self, text: str) -> None:
        assert not _is_strong_heading(text)


class TestLooksLikeHeading:
    def test_short_no_punct_is_heading(self) -> None:
        assert _looks_like_heading("BOURGEOIS AND PROLETARIANS")
        assert _looks_like_heading("The Crisis")

    def test_long_sentence_rejected(self) -> None:
        assert not _looks_like_heading(
            "The history of all hitherto existing societies is the history "
            "of class struggles, said Marx."
        )

    def test_punctuation_only_rejected(self) -> None:
        assert not _looks_like_heading("[1]")
        assert not _looks_like_heading("***")

    def test_strong_pattern_always_passes(self) -> None:
        # Even a sentence-shaped string matches if it has a strong prefix.
        assert _looks_like_heading("I. BOURGEOIS AND PROLETARIANS")

    def test_empty_rejected(self) -> None:
        assert not _looks_like_heading("")
        assert not _looks_like_heading("   ")

    def test_trailing_comma_or_colon_rejected(self) -> None:
        assert not _looks_like_heading("In conclusion,")
        assert not _looks_like_heading("Note:")


# --------------------------------------------------------------------------- #
# Node flattening + candidate selection
# --------------------------------------------------------------------------- #

class TestFlattenToNodes:
    def test_walks_in_document_order(self) -> None:
        book = _book([
            _section("s1", "Front Matter", [
                _para("p1", "I. BOURGEOIS AND PROLETARIANS", "s1", 0),
                _para("p2", "The history of all hitherto existing societies...", "s1", 1),
                _para("p3", "II. PROLETARIANS AND COMMUNISTS", "s1", 2),
                _para("p4", "In what relation do the Communists stand to...", "s1", 3),
            ])
        ])
        nodes = _flatten_to_nodes(book)
        # heading "Front Matter" + 4 paragraphs = 5 nodes
        assert len(nodes) == 5
        assert nodes[0].kind == "heading"
        assert nodes[0].text == "Front Matter"
        assert [n.text for n in nodes[1:]] == [
            "I. BOURGEOIS AND PROLETARIANS",
            "The history of all hitherto existing societies...",
            "II. PROLETARIANS AND COMMUNISTS",
            "In what relation do the Communists stand to...",
        ]

    def test_next_preview_set(self) -> None:
        book = _book([
            _section("s1", "C1", [
                _para("p1", "Title", "s1", 0),
                _para("p2", "Body content here.", "s1", 1),
            ])
        ])
        nodes = _flatten_to_nodes(book)
        # First node is heading "C1"; its next is paragraph "Title"
        assert nodes[0].next_preview == "Title"
        # Last node has empty preview
        assert nodes[-1].next_preview == ""

    def test_anchor_ids_unique_and_ordered(self) -> None:
        book = _book([
            _section("s1", "C1", [_para(f"p{i}", f"text{i}", "s1", i) for i in range(5)])
        ])
        nodes = _flatten_to_nodes(book)
        ids = [n.anchor_id for n in nodes]
        assert len(set(ids)) == len(ids)
        assert ids == sorted(ids)


class TestSelectCandidates:
    def test_keeps_headings_drops_long_prose(self) -> None:
        book = _book([
            _section("s1", "Front Matter", [
                _para("p1", "I. BOURGEOIS AND PROLETARIANS", "s1", 0),  # candidate
                _para("p2", "The history of all hitherto existing societies is "
                            "a long sentence that should not be picked.", "s1", 1),
                _para("p3", "II. PROLETARIANS", "s1", 2),  # candidate
            ])
        ])
        nodes = _flatten_to_nodes(book)
        cands = _select_candidates(nodes)
        cand_texts = [c.text for c in cands]
        assert "Front Matter" in cand_texts  # heading kept
        assert "I. BOURGEOIS AND PROLETARIANS" in cand_texts
        assert "II. PROLETARIANS" in cand_texts
        assert not any("hitherto existing" in t for t in cand_texts)

    def test_max_count_caps(self) -> None:
        # 1000 short paragraphs that all look like headings
        paras = [_para(f"p{i:04d}", f"Title {i}", "s1", i) for i in range(1000)]
        book = _book([_section("s1", "C1", paras)])
        nodes = _flatten_to_nodes(book)
        cands = _select_candidates(nodes, max_count=50)
        assert len(cands) == 50


# --------------------------------------------------------------------------- #
# Book rebuild
# --------------------------------------------------------------------------- #

class TestRebuildBook:
    def _manifesto_like_book(self) -> Book:
        """A book whose heuristic ingest dumped everything into 'Front Matter'."""
        return _book([
            _section("s_fm", "Front Matter", [
                _para("p0", "Preamble paragraph.", "s_fm", 0),
                _para("p1", "I. BOURGEOIS AND PROLETARIANS", "s_fm", 1),
                _para("p2", "The history of all hitherto existing societies...", "s_fm", 2),
                _para("p3", "Freeman and slave, patrician and plebeian...", "s_fm", 3),
                _para("p4", "II. PROLETARIANS AND COMMUNISTS", "s_fm", 4),
                _para("p5", "In what relation do the Communists stand to...", "s_fm", 5),
                _para("p6", "The Communists do not form a separate party...", "s_fm", 6),
            ])
        ])

    def _find_anchor(self, nodes, text: str) -> str:
        for n in nodes:
            if n.text == text:
                return n.anchor_id
        raise KeyError(text)

    def test_rebuild_splits_at_detected_anchors(self) -> None:
        book = self._manifesto_like_book()
        nodes = _flatten_to_nodes(book)
        a1 = self._find_anchor(nodes, "I. BOURGEOIS AND PROLETARIANS")
        a2 = self._find_anchor(nodes, "II. PROLETARIANS AND COMMUNISTS")
        refined = _rebuild_book(book, nodes, [
            (a1, "I. Bourgeois and Proletarians", 1),
            (a2, "II. Proletarians and Communists", 1),
        ])
        # Top-level: Front Matter (synthetic) + 2 detected chapters.
        # Front Matter is placed at the same level as the detected chapters
        # so it sits as a sibling, not a parent.
        headings = [s.heading for s in refined.toc]
        assert headings == [
            "Front Matter",
            "I. Bourgeois and Proletarians",
            "II. Proletarians and Communists",
        ]

    def test_front_matter_uses_shallowest_detected_level(self) -> None:
        """If the LLM picks level=2 for all chapters, Front Matter must also be
        level=2 so it stays a sibling (the previous behavior placed it at level=1
        and ended up swallowing every chapter as a nested child)."""
        book = self._manifesto_like_book()
        nodes = _flatten_to_nodes(book)
        a1 = self._find_anchor(nodes, "I. BOURGEOIS AND PROLETARIANS")
        a2 = self._find_anchor(nodes, "II. PROLETARIANS AND COMMUNISTS")
        refined = _rebuild_book(book, nodes, [
            (a1, "I.", 2),
            (a2, "II.", 2),
        ])
        assert [s.heading for s in refined.toc] == ["Front Matter", "I.", "II."]
        # No chapter is buried as a child of Front Matter.
        for s in refined.toc:
            assert s.children == []

    def test_anchor_paragraph_is_consumed_not_in_body(self) -> None:
        book = self._manifesto_like_book()
        nodes = _flatten_to_nodes(book)
        a1 = self._find_anchor(nodes, "I. BOURGEOIS AND PROLETARIANS")
        a2 = self._find_anchor(nodes, "II. PROLETARIANS AND COMMUNISTS")
        refined = _rebuild_book(book, nodes, [
            (a1, "I. Bourgeois", 1),
            (a2, "II. Proletarians", 1),
        ])
        ch1 = next(s for s in refined.toc if s.heading == "I. Bourgeois")
        # Body should NOT include the heading text itself
        assert not any("BOURGEOIS AND PROLETARIANS" in p.source_text for p in ch1.paragraphs)
        # But should include the actual chapter content
        assert any("hitherto existing" in p.source_text for p in ch1.paragraphs)

    def test_paragraph_ids_preserved(self) -> None:
        book = self._manifesto_like_book()
        nodes = _flatten_to_nodes(book)
        a1 = self._find_anchor(nodes, "I. BOURGEOIS AND PROLETARIANS")
        refined = _rebuild_book(book, nodes, [(a1, "I. Bourgeois", 1)])

        original_pids = {p.paragraph_id for p in book.iter_paragraphs()}
        # After refinement, anchor paragraphs are dropped; everything else kept.
        new_pids = {p.paragraph_id for p in refined.iter_paragraphs()}
        assert new_pids.issubset(original_pids)
        # The anchor "p1" was consumed
        assert "p1" not in new_pids
        # Other paragraphs survived
        assert {"p0", "p2", "p3", "p4", "p5", "p6"}.issubset(new_pids)

    def test_paragraph_section_id_updated(self) -> None:
        book = self._manifesto_like_book()
        nodes = _flatten_to_nodes(book)
        a1 = self._find_anchor(nodes, "I. BOURGEOIS AND PROLETARIANS")
        a2 = self._find_anchor(nodes, "II. PROLETARIANS AND COMMUNISTS")
        refined = _rebuild_book(book, nodes, [
            (a1, "I. Bourgeois", 1),
            (a2, "II. Proletarians", 1),
        ])
        ch1 = next(s for s in refined.toc if s.heading == "I. Bourgeois")
        ch2 = next(s for s in refined.toc if s.heading == "II. Proletarians")
        # All paragraphs in ch1 have ch1's section_id
        assert all(p.section_id == ch1.section_id for p in ch1.paragraphs)
        # And ch2 paragraphs have ch2's
        assert all(p.section_id == ch2.section_id for p in ch2.paragraphs)
        # The two section_ids differ
        assert ch1.section_id != ch2.section_id

    def test_front_matter_omitted_when_no_orphan_paragraphs(self) -> None:
        book = _book([
            _section("s1", "Wrap", [
                _para("p1", "Chapter 1: Beginning", "s1", 0),
                _para("p2", "Body of chapter 1.", "s1", 1),
            ])
        ])
        nodes = _flatten_to_nodes(book)
        # Only paragraph p1 is the first anchor; no orphan paragraphs before it
        # (the heading "Wrap" comes first but that's a heading node, not paragraphs).
        # However, the heading "Wrap" is at index 0 and the anchor at index 1.
        # No paragraph nodes precede the anchor, so no Front Matter section.
        a1 = self._find_anchor(nodes, "Chapter 1: Beginning")
        refined = _rebuild_book(book, nodes, [(a1, "Chapter 1: Beginning", 2)])
        assert [s.heading for s in refined.toc] == ["Chapter 1: Beginning"]

    def test_nested_levels(self) -> None:
        book = _book([
            _section("s_fm", "Wrapper", [
                _para("a", "Part I", "s_fm", 0),
                _para("b", "p before chapter", "s_fm", 1),
                _para("c", "Chapter 1", "s_fm", 2),
                _para("d", "p in ch1", "s_fm", 3),
                _para("e", "Section 1.1", "s_fm", 4),
                _para("f", "p in section 1.1", "s_fm", 5),
            ])
        ])
        nodes = _flatten_to_nodes(book)
        refined = _rebuild_book(book, nodes, [
            (self._find_anchor(nodes, "Part I"), "Part I", 1),
            (self._find_anchor(nodes, "Chapter 1"), "Chapter 1", 2),
            (self._find_anchor(nodes, "Section 1.1"), "Section 1.1", 3),
        ])
        # First anchor is "Part I" at node index 1; only the heading "Wrapper"
        # precedes it, which is a heading node not a paragraph, so no Front
        # Matter is synthesized. Part I contains "p before chapter", Chapter 1
        # contains "p in ch1", Section 1.1 contains "p in section 1.1".
        top = refined.toc
        assert [s.heading for s in top] == ["Part I"]
        part = top[0]
        assert [c.heading for c in part.children] == ["Chapter 1"]
        chapter = part.children[0]
        assert [s.heading for s in chapter.children] == ["Section 1.1"]
        # And paragraph attribution
        assert [p.source_text for p in part.paragraphs] == ["p before chapter"]
        assert [p.source_text for p in chapter.paragraphs] == ["p in ch1"]
        assert [p.source_text for p in chapter.children[0].paragraphs] == [
            "p in section 1.1"
        ]

    def test_empty_detection_returns_original(self) -> None:
        book = self._manifesto_like_book()
        nodes = _flatten_to_nodes(book)
        refined = _rebuild_book(book, nodes, [])
        assert refined is book
