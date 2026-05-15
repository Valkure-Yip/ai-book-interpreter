"""Unit tests for mechanical metrics (glossary, length, anchors, completeness)."""

from __future__ import annotations

from abi.eval.metrics import compute_mechanical
from abi.types.eval import AlignedTriple
from abi.types.glossary import Glossary, GlossaryEntry


def _triple(
    pid: str,
    src: str,
    abi: str,
    baseline: str,
    pos: int = 0,
    aligned: bool = True,
) -> AlignedTriple:
    return AlignedTriple(
        paragraph_id=pid,
        position=pos,
        section_id="sec1",
        heading_trail=["Ch1"],
        source_text=src,
        abi_text=abi,
        baseline_text=baseline,
        aligned=aligned,
    )


def _glossary(*pairs: tuple[str, str]) -> Glossary:
    entries = [
        GlossaryEntry(term=t, surface_forms=[t], target=tgt, locked=True, is_core=True)
        for t, tgt in pairs
    ]
    return Glossary(book_id="b1", target_language="zh", entries=entries)


class TestGlossaryCompliance:
    def test_full_compliance(self) -> None:
        triples = [
            _triple("p1", "Bourgeoisie rules.", "资产阶级统治。", "资产阶级统治。"),
        ]
        score = compute_mechanical(
            triples=triples,
            glossary=_glossary(("bourgeoisie", "资产阶级")),
            source_language="en",
            target_language="zh",
            system="abi",
        )
        assert score.glossary_compliance == 1.0
        assert score.glossary_violations == 0

    def test_baseline_violates_term(self) -> None:
        triples = [
            _triple("p1", "Bourgeoisie rules.", "资产阶级统治。", "中产阶级统治。"),
        ]
        gl = _glossary(("bourgeoisie", "资产阶级"))
        abi = compute_mechanical(
            triples=triples, glossary=gl, source_language="en",
            target_language="zh", system="abi"
        )
        base = compute_mechanical(
            triples=triples, glossary=gl, source_language="en",
            target_language="zh", system="baseline"
        )
        assert abi.glossary_compliance == 1.0
        assert base.glossary_compliance == 0.0
        assert base.glossary_violations == 1

    def test_no_glossary_terms_appearing_means_score_1(self) -> None:
        triples = [_triple("p1", "An ordinary sentence.", "一个普通句子。", "一句话。")]
        score = compute_mechanical(
            triples=triples,
            glossary=_glossary(("bourgeoisie", "资产阶级")),
            source_language="en",
            target_language="zh",
            system="abi",
        )
        assert score.glossary_checked == 0
        assert score.glossary_compliance == 1.0


class TestLengthRatio:
    def test_in_band_en_zh(self) -> None:
        src = "x" * 100
        tgt = "中" * 30  # ratio = 0.3, within [0.22, 0.55]
        triples = [_triple("p1", src, tgt, tgt)]
        score = compute_mechanical(
            triples=triples,
            glossary=_glossary(),
            source_language="en",
            target_language="zh",
            system="abi",
        )
        assert score.length_ratio_ok == 1.0
        assert 0.29 < score.length_ratio_mean < 0.31

    def test_out_of_band(self) -> None:
        src = "x" * 100
        too_long = "中" * 80  # 0.8, well above 0.55
        triples = [_triple("p1", src, too_long, too_long)]
        score = compute_mechanical(
            triples=triples,
            glossary=_glossary(),
            source_language="en",
            target_language="zh",
            system="abi",
        )
        assert score.length_ratio_ok == 0.0


class TestAnchorPreservation:
    def test_number_anchor_preserved(self) -> None:
        triples = [
            _triple(
                "p1",
                "There were 42 protesters in 1989.",
                "1989 年有 42 名抗议者。",
                "好多抗议者。",  # baseline drops both numbers
            )
        ]
        abi = compute_mechanical(
            triples=triples, glossary=_glossary(),
            source_language="en", target_language="zh", system="abi"
        )
        base = compute_mechanical(
            triples=triples, glossary=_glossary(),
            source_language="en", target_language="zh", system="baseline"
        )
        assert abi.anchor_preservation == 1.0
        assert base.anchor_preservation == 0.0
        assert abi.anchor_checked == 1


class TestCompleteness:
    def test_missing_baseline_paragraph_counted(self) -> None:
        triples = [
            _triple("p1", "Hello world.", "你好世界。", "你好世界。"),
            _triple("p2", "Lost in baseline.", "迷失在 baseline。", "", aligned=False),
        ]
        abi = compute_mechanical(
            triples=triples, glossary=_glossary(),
            source_language="en", target_language="zh", system="abi"
        )
        base = compute_mechanical(
            triples=triples, glossary=_glossary(),
            source_language="en", target_language="zh", system="baseline"
        )
        assert abi.completeness == 1.0
        assert abi.completeness_missing == 0
        assert base.completeness == 0.5
        assert base.completeness_missing == 1

    def test_truncated_translation_counted_missing(self) -> None:
        # 100-char source, 1-char translation = essentially nothing.
        triples = [_triple("p1", "x" * 100, "你", "你")]
        score = compute_mechanical(
            triples=triples, glossary=_glossary(),
            source_language="en", target_language="zh", system="abi"
        )
        assert score.completeness == 0.0
        assert score.completeness_missing == 1
