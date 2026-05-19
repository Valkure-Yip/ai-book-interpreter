"""Unit tests for the 3-way eval path (ABI / Baseline / Reference)."""

from __future__ import annotations

from datetime import datetime

import pytest

from abi.eval.aggregate import aggregate
from abi.eval.metrics import compute_mechanical
from abi.types.eval import (
    AlignedTriple,
    JudgeSampleResult,
    LikertScore,
)
from abi.types.glossary import Glossary


def _triple(
    pid: str,
    pos: int,
    source: str,
    abi: str,
    base: str,
    ref: str,
    section: str = "Ch1",
    document_id: str = "doc-A",
) -> AlignedTriple:
    return AlignedTriple(
        paragraph_id=pid,
        position=pos,
        section_id=f"sec-{section}",
        heading_trail=[section],
        source_text=source,
        abi_text=abi,
        baseline_text=base,
        aligned=True,
        reference_text=ref,
        has_reference=True,
        document_id=document_id,
    )


def _empty_glossary() -> Glossary:
    return Glossary(book_id="b", target_language="zh", entries=[])


class TestMechanicalReferenceSystem:
    def test_compute_mechanical_for_reference(self) -> None:
        triples = [
            _triple("p1", 0, "Source paragraph one with year 2024.", "ABI 译文 2024。", "基线 2024。", "参考 2024。"),
            _triple("p2", 1, "Another source line.", "另一段译文。", "另一基线。", "另一参考。"),
        ]
        score = compute_mechanical(
            triples=triples,
            glossary=_empty_glossary(),
            source_language="en",
            target_language="zh",
            system="reference",
        )
        assert score.system == "reference"
        assert score.n_paragraphs == 2
        # Reference is non-empty for both paragraphs.
        assert score.completeness == 1.0
        # Year anchor 2024 must survive in reference.
        assert score.anchor_preservation == 1.0

    def test_compute_mechanical_rejects_unknown_system(self) -> None:
        with pytest.raises(ValueError):
            compute_mechanical(
                triples=[],
                glossary=_empty_glossary(),
                source_language="en",
                target_language="zh",
                system="bogus",
            )


def _ls(adq: int, flu: int, coh: int, sty: int) -> LikertScore:
    return LikertScore(
        adequacy=adq, fluency=flu, coherence=coh, style=sty
    )


def _judge_3way(
    pid: str,
    abi_label: str,
    abi: LikertScore,
    base: LikertScore,
    ref: LikertScore,
    *,
    verdict_ab: str = "A",
    verdict_ar: str = "B",
    verdict_br: str = "B",
) -> JudgeSampleResult:
    return JudgeSampleResult(
        paragraph_id=pid,
        position=0,
        section_id="s",
        abi_label=abi_label,  # type: ignore[arg-type]
        likert_abi=abi,
        likert_baseline=base,
        likert_reference=ref,
        pairwise_verdict=verdict_ab,  # type: ignore[arg-type]
        pairwise_abi_vs_ref=verdict_ar,  # type: ignore[arg-type]
        pairwise_baseline_vs_ref=verdict_br,  # type: ignore[arg-type]
    )


class TestAggregateThreeWay:
    def test_likert_reference_block_populated(self) -> None:
        results = [
            _judge_3way(
                "p1", "A",
                _ls(4, 4, 4, 4), _ls(3, 3, 3, 3), _ls(5, 5, 5, 5),
            ),
            _judge_3way(
                "p2", "A",
                _ls(5, 5, 5, 5), _ls(4, 4, 4, 4), _ls(5, 5, 5, 5),
            ),
        ]
        agg = aggregate(results)
        assert agg.likert_reference["adequacy"] == 5.0
        assert agg.likert_reference["mean"] == 5.0
        assert agg.likert_abi["mean"] == 4.5

    def test_three_winrates(self) -> None:
        # Build a scenario where:
        #   ABI vs Baseline: ABI wins both (2/2) → winrate 1.0
        #   ABI vs Reference: Reference wins one, tie one → ABI winrate 0.25
        #   Baseline vs Reference: Reference wins both → Baseline winrate 0.0
        results = [
            _judge_3way(
                "p1", "A",
                _ls(5, 5, 5, 5), _ls(3, 3, 3, 3), _ls(5, 5, 5, 5),
                verdict_ab="A", verdict_ar="tie", verdict_br="B",
            ),
            _judge_3way(
                "p2", "A",
                _ls(4, 4, 4, 4), _ls(3, 3, 3, 3), _ls(5, 5, 5, 5),
                verdict_ab="A", verdict_ar="B", verdict_br="B",
            ),
        ]
        agg = aggregate(results)
        assert agg.pairwise_abi_winrate == 1.0
        assert agg.pairwise_abi_vs_ref_winrate == 0.25
        assert agg.pairwise_baseline_vs_ref_winrate == 0.0
        assert agg.pairwise_abi_vs_ref_wins == 0
        assert agg.pairwise_abi_vs_ref_ties == 1
        assert agg.pairwise_abi_vs_ref_losses == 1

    def test_no_reference_keeps_three_way_fields_zero(self) -> None:
        results = [
            JudgeSampleResult(
                paragraph_id="p1",
                position=0,
                section_id="s",
                abi_label="A",
                likert_abi=_ls(5, 5, 5, 5),
                likert_baseline=_ls(3, 3, 3, 3),
                pairwise_verdict="A",
            )
        ]
        agg = aggregate(results)
        assert agg.likert_reference == {}
        assert agg.pairwise_abi_vs_ref_winrate == 0.0
        assert agg.pairwise_baseline_vs_ref_winrate == 0.0


class TestJudgeDecoder3Way:
    """Cross-check the slot↔system de-anonymization logic."""

    def test_decoder_returns_correct_system(self) -> None:
        from abi.eval._schemas import JudgePairwise3Output
        from abi.eval.judge import _decode_verdict_pair

        parsed = JudgePairwise3Output(
            a_vs_b="A", a_vs_c="C", b_vs_c="tie", rationale=""
        )
        # Slot assignment: A=baseline, B=reference, C=abi
        slot_to_system = {"A": "baseline", "B": "reference", "C": "abi"}
        # a_vs_b: A wins → baseline wins
        assert _decode_verdict_pair(parsed, "A", "B", slot_to_system) == "baseline"
        # a_vs_c: C wins → abi wins
        assert _decode_verdict_pair(parsed, "A", "C", slot_to_system) == "abi"
        # b_vs_c: tie
        assert _decode_verdict_pair(parsed, "B", "C", slot_to_system) == "tie"
        # Reversed lookup also works:
        assert _decode_verdict_pair(parsed, "C", "A", slot_to_system) == "abi"


class TestAlignmentWithReferences:
    def test_references_attach_positionally(self) -> None:
        from abi.eval.alignment import align
        from abi.types.book import Book, BookMeta, Paragraph, Section
        from abi.types.translation import TranslationUnit

        paras = [
            Paragraph(
                paragraph_id=f"p{i}",
                kind="prose",
                source_text=f"source {i}",
                position=i,
                section_id="sec1",
            )
            for i in range(3)
        ]
        section = Section(
            section_id="sec1", level=1, heading="Ch1", heading_trail=["Ch1"],
            paragraphs=paras,
        )
        book = Book(
            meta=BookMeta(
                book_id="b", title="t", source_format="txt", source_path="/x",
                source_sha256="0" * 64, detected_at=datetime.utcnow(),
            ),
            toc=[section],
        )
        units = {
            p.paragraph_id: TranslationUnit(
                paragraph_id=p.paragraph_id, kind="prose",
                source_text="", translated_text=f"abi {i}", target_language="zh",
            )
            for i, p in enumerate(paras)
        }
        triples, report = align(
            book=book,
            units=units,
            baseline_paragraphs=["base 0", "base 1", "base 2"],
            references=["ref 0", "ref 1", "ref 2"],
            document_ids=["doc-A", "doc-A", "doc-B"],
        )
        assert report.reference_paragraphs == 3
        assert all(t.has_reference for t in triples)
        assert triples[0].reference_text == "ref 0"
        assert triples[2].document_id == "doc-B"

    def test_references_length_mismatch_raises(self) -> None:
        from abi.eval.alignment import align
        from abi.types.book import Book, BookMeta, Paragraph, Section

        paras = [
            Paragraph(
                paragraph_id=f"p{i}", kind="prose",
                source_text=f"source {i}", position=i, section_id="sec1",
            )
            for i in range(3)
        ]
        section = Section(
            section_id="sec1", level=1, heading="Ch1", heading_trail=["Ch1"],
            paragraphs=paras,
        )
        book = Book(
            meta=BookMeta(
                book_id="b", title="t", source_format="txt", source_path="/x",
                source_sha256="0" * 64, detected_at=datetime.utcnow(),
            ),
            toc=[section],
        )
        with pytest.raises(ValueError):
            align(
                book=book, units={}, baseline_paragraphs=[],
                references=["only one"],  # 1 != 3
            )
