"""Unit tests for stratified sampler + judge aggregation."""

from __future__ import annotations

from abi.eval.aggregate import aggregate
from abi.eval.sampler import stratified_sample
from abi.types.eval import AlignedTriple, JudgeSampleResult, LikertScore


def _t(pid: str, position: int, chapter: str, src_chars: int = 200) -> AlignedTriple:
    return AlignedTriple(
        paragraph_id=pid,
        position=position,
        section_id=f"sec-{chapter}",
        heading_trail=[chapter],
        source_text="x" * src_chars,
        abi_text="abi",
        baseline_text="base",
        aligned=True,
    )


class TestSampler:
    def test_returns_all_when_pool_smaller_than_budget(self) -> None:
        triples = [_t(f"p{i}", i, "Ch1") for i in range(5)]
        out = stratified_sample(triples, n_samples=30)
        assert len(out) == 5

    def test_each_chapter_contributes_first_paragraph(self) -> None:
        triples = (
            [_t(f"a{i}", i, "Ch1") for i in range(10)]
            + [_t(f"b{i}", 10 + i, "Ch2") for i in range(10)]
            + [_t(f"c{i}", 20 + i, "Ch3") for i in range(10)]
        )
        out = stratified_sample(triples, n_samples=6, seed=1)
        pids = {t.paragraph_id for t in out}
        # a0, b0, c0 must all be present
        assert "a0" in pids and "b0" in pids and "c0" in pids

    def test_proportional_distribution(self) -> None:
        # 80 paragraphs in Ch1, 20 in Ch2 — sample of 10 should roughly split 8/2.
        triples = (
            [_t(f"a{i}", i, "Ch1") for i in range(80)]
            + [_t(f"b{i}", 100 + i, "Ch2") for i in range(20)]
        )
        out = stratified_sample(triples, n_samples=10, seed=42)
        ch1_count = sum(1 for t in out if t.heading_trail == ["Ch1"])
        ch2_count = sum(1 for t in out if t.heading_trail == ["Ch2"])
        assert ch1_count + ch2_count == 10
        # 80/20 → expect roughly 8/2, allow ±2 slack
        assert 6 <= ch1_count <= 10
        assert 1 <= ch2_count <= 4

    def test_filters_short_paragraphs(self) -> None:
        triples = (
            [_t("short", 0, "Ch1", src_chars=10)]
            + [_t(f"p{i}", i + 1, "Ch1", src_chars=200) for i in range(5)]
        )
        out = stratified_sample(triples, n_samples=10)
        assert all(t.paragraph_id != "short" for t in out)

    def test_unaligned_paragraphs_excluded(self) -> None:
        a = _t("p1", 0, "Ch1")
        b = AlignedTriple(
            paragraph_id="p2",
            position=1,
            section_id="sec-Ch1",
            heading_trail=["Ch1"],
            source_text="x" * 200,
            abi_text="abi",
            baseline_text="",
            aligned=False,
        )
        out = stratified_sample([a, b], n_samples=5)
        assert [t.paragraph_id for t in out] == ["p1"]

    def test_deterministic_with_same_seed(self) -> None:
        triples = [_t(f"p{i}", i, f"Ch{i % 3}") for i in range(30)]
        a = stratified_sample(triples, n_samples=8, seed=7)
        b = stratified_sample(triples, n_samples=8, seed=7)
        assert [t.paragraph_id for t in a] == [t.paragraph_id for t in b]


def _ls(adq: int, flu: int, coh: int, sty: int) -> LikertScore:
    return LikertScore(
        adequacy=adq, fluency=flu, coherence=coh, style=sty
    )


def _result(pid: str, abi_label: str, abi: LikertScore, base: LikertScore, verdict: str) -> JudgeSampleResult:
    return JudgeSampleResult(
        paragraph_id=pid,
        position=0,
        section_id="s",
        abi_label=abi_label,  # type: ignore[arg-type]
        likert_abi=abi,
        likert_baseline=base,
        pairwise_verdict=verdict,  # type: ignore[arg-type]
    )


class TestAggregate:
    def test_empty(self) -> None:
        agg = aggregate([])
        assert agg.samples == 0
        assert agg.likert_abi == {}

    def test_winrate_ties_count_half(self) -> None:
        # ABI is A on 4 samples: 2 A wins, 1 tie, 1 B win  →  ABI wins 2, ties 1
        # winrate = (2 + 0.5*1) / 4 = 0.625
        results = [
            _result("p1", "A", _ls(5, 5, 5, 5), _ls(3, 3, 3, 3), "A"),
            _result("p2", "A", _ls(5, 5, 5, 5), _ls(3, 3, 3, 3), "A"),
            _result("p3", "A", _ls(5, 5, 5, 5), _ls(3, 3, 3, 3), "tie"),
            _result("p4", "A", _ls(5, 5, 5, 5), _ls(3, 3, 3, 3), "B"),
        ]
        agg = aggregate(results)
        assert agg.pairwise_abi_wins == 2
        assert agg.pairwise_baseline_wins == 1
        assert agg.pairwise_ties == 1
        assert agg.pairwise_abi_winrate == 0.625

    def test_winrate_handles_b_label_correctly(self) -> None:
        # ABI is B on these samples; "verdict=B" means ABI wins.
        results = [
            _result("p1", "B", _ls(5, 5, 5, 5), _ls(2, 2, 2, 2), "B"),
            _result("p2", "B", _ls(5, 5, 5, 5), _ls(2, 2, 2, 2), "A"),
        ]
        agg = aggregate(results)
        assert agg.pairwise_abi_wins == 1
        assert agg.pairwise_baseline_wins == 1

    def test_likert_means_and_delta(self) -> None:
        results = [
            _result("p1", "A", _ls(4, 5, 4, 4), _ls(3, 3, 3, 3), "A"),
            _result("p2", "A", _ls(5, 5, 5, 5), _ls(4, 4, 4, 4), "A"),
        ]
        agg = aggregate(results)
        # adequacy: abi = 4.5, baseline = 3.5, delta = 1.0
        assert agg.likert_abi["adequacy"] == 4.5
        assert agg.likert_baseline["adequacy"] == 3.5
        assert agg.likert_delta["adequacy"] == 1.0
        # mean across dims
        assert agg.likert_abi["mean"] > agg.likert_baseline["mean"]
