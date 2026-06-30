"""Offline tests for the eval pipeline (no network, no LLM)."""

from __future__ import annotations

import random

import pytest

from abi.eval.align import align_paragraphs, split_paragraphs
from abi.eval.book import (
    _load_glossary,
    check_book_l3,
    eval_book,
    score_book_l2,
)
from abi.eval.calibration import bands_from_calibration, calibrate
from abi.eval.datasets import load_triples, parse_dataset_spec
from abi.eval.judge import LikertOutput, PairwiseOutput, SlotScore, judge_triple
from abi.eval.mechanical import length_ratio_ok, resolve_band, score_paragraph
from abi.eval.report import aggregate_mechanical
from abi.eval.types import EvalTriple
from abi.project.layout import BookProject
from abi.project.state import PipelineState, Status


# --- datasets / spec ---
def test_parse_dataset_spec_basic():
    spec = parse_dataset_spec("wmt24pp:en-zh_CN:literary")
    assert spec.adapter == "wmt24pp"
    assert spec.config == "en-zh_CN"
    assert spec.domain == "literary"
    assert spec.source_lang == "en"
    assert spec.target_lang == "zh-CN"


def test_parse_dataset_spec_options():
    spec = parse_dataset_spec("wmt24pp:en-ja_JP:literary:stub=true:limit=2")
    assert spec.stub is True
    assert spec.limit == 2


def test_load_stub_triples():
    spec = parse_dataset_spec("wmt24pp:en-zh_CN:literary:stub=true")
    triples = load_triples(spec)
    assert len(triples) == 5
    assert all(t.reference for t in triples)
    assert triples[0].source_lang == "en"


# --- mechanical ---
def test_length_ratio_ok_in_band():
    # en-zh resolves to the committed calibrated band (WMT24++ literary), which
    # takes precedence over the QUALITY_SCORE default.
    band = resolve_band("en", "zh-Hans")
    assert band.method == "calibrated"
    assert 0.26 <= band.lo <= 0.28 and 0.42 <= band.hi <= 0.44
    assert length_ratio_ok(0.35, band) == 1.0
    assert length_ratio_ok(0.10, band) == 0.2  # well below shoulder
    assert 0.5 <= length_ratio_ok(0.23, band) <= 1.0  # shoulder


def test_packaged_calibrated_bands_loaded():
    from abi.eval.mechanical import packaged_calibrated_bands

    bands = packaged_calibrated_bands()
    assert {"en-zh", "en-ja", "en-es", "en-fr", "en-de"} <= set(bands)
    assert all(b.method == "calibrated" and b.n > 0 for b in bands.values())


def test_explicit_bands_override_packaged():
    from abi.eval.types import LengthBand

    override = {"en-zh": LengthBand(source_target="en-zh", lo=0.1, hi=0.9, method="x")}
    band = resolve_band("en", "zh-Hans", override)
    assert band.lo == 0.1 and band.hi == 0.9


def test_score_paragraph_good_translation():
    s = score_paragraph(
        "All happy families are alike; each unhappy family is unhappy in its own way.",
        "幸福的家庭都是相似的，不幸的家庭各有各的不幸。",
        source_lang="en",
        target_lang="zh-Hans",
    )
    assert s.completeness == 1.0
    assert s.length_ratio_ok == 1.0  # ~0.30 ratio, inside the calibrated en-zh band
    assert s.para_score > 0.9
    assert "low_score" not in s.flags


def test_score_paragraph_preserves_anchors():
    s = score_paragraph(
        "In 1925 he sold 4,000 copies, far more than the 200 he expected to move.",
        "1925 年，他售出了 4,000 册，远超他原本预期的 200 册。",
        source_lang="en",
        target_lang="zh-Hans",
    )
    assert s.anchor_preservation == 1.0  # 1925 / 4,000 / 200 preserved


def test_score_paragraph_empty_is_completeness_fail():
    s = score_paragraph("hello world", None, source_lang="en", target_lang="zh-Hans")
    assert s.completeness == 0.0
    assert s.para_score == 0.0
    assert "completeness_fail" in s.flags


def test_score_paragraph_residue_flagged():
    s = score_paragraph(
        "The cat sat on the mat in the warm afternoon sun.",
        "The cat sat on the mat in the warm afternoon sun.",  # untranslated
        source_lang="en",
        target_lang="zh-Hans",
    )
    assert s.no_refusal_no_residue == 0.0
    assert "untranslated_residue" in s.flags


def test_term_compliance_violation():
    s = score_paragraph(
        "The Party controls the state.",
        "该组织控制国家。",  # "Party" should be 党 per glossary
        source_lang="en",
        target_lang="zh-Hans",
        glossary={"Party": "党"},
    )
    assert s.term_compliance is not None and s.term_compliance < 1.0
    assert "term_drift" in s.flags


# --- alignment ---
def test_align_equal_length():
    src = "Para one.\n\nPara two is here."
    tgt = "段落一。\n\n第二段在这里。"
    al = align_paragraphs(src, tgt)
    assert not al.chapter_align_failed
    assert len(al.pairs) == 2
    assert al.pairs[1].target is not None


def test_align_skips_headings():
    paras = split_paragraphs("# Title\n\nReal paragraph content here.")
    assert paras == ["Real paragraph content here."]


def test_align_large_gap_degrades():
    # 10 source paras vs 1 target: match rate far below threshold -> chapter-level
    # fallback (caller scores the whole chapter), even though NW matches one pair.
    src = "\n\n".join(f"Source paragraph number {i} with text." for i in range(10))
    tgt = "Only one paragraph."
    al = align_paragraphs(src, tgt)
    assert al.chapter_align_failed
    assert sum(p.target is not None for p in al.pairs) <= 1


def test_align_merged_paragraphs_stays_per_paragraph():
    # A realistic small merge (5 -> 4 paras) keeps a high match rate, so per-
    # paragraph alignment is retained (chapter_align_failed stays False).
    src = "\n\n".join(f"Source sentence number {i} with some words." for i in range(5))
    tgt = "\n\n".join(f"目标句子第 {i} 句包含若干词语内容。" for i in range(4))
    al = align_paragraphs(src, tgt)
    assert sum(p.target is not None for p in al.pairs) >= 4


# --- calibration ---
def test_calibrate_stub():
    triples = load_triples(parse_dataset_spec("wmt24pp:en-zh_CN:literary:stub=true"))
    results = calibrate(triples)
    assert len(results) == 1
    r = results[0]
    assert r.band_key == "en-zh"
    assert r.n == 5
    assert r.suggested_lo <= r.ratio_p50 <= r.suggested_hi


def test_bands_min_samples_gate():
    triples = load_triples(parse_dataset_spec("wmt24pp:en-zh_CN:literary:stub=true"))
    results = calibrate(triples)
    assert bands_from_calibration(results, min_samples=50) == {}  # only 5 samples
    bands = bands_from_calibration(results, min_samples=3)
    assert "en-zh" in bands and bands["en-zh"].method == "calibrated"


# --- aggregation ---
def test_aggregate_mechanical():
    triples = load_triples(parse_dataset_spec("wmt24pp:en-zh_CN:literary:stub=true"))
    scores = [
        score_paragraph(t.source, t.reference, source_lang=t.source_lang,
                        target_lang=t.target_lang)
        for t in triples
    ]
    agg = aggregate_mechanical(scores)
    assert agg["n"] == 5
    assert 0.0 <= agg["score_avg"] <= 1.0
    assert agg["completeness"] == 1.0


# --- judge (fake router) ---
class _FakeRouter:
    """Returns canned structured outputs; records call count."""

    def __init__(self) -> None:
        self.calls = 0

    async def invoke_structured(self, schema, messages, **kwargs):
        self.calls += 1
        if schema is LikertOutput:
            return LikertOutput(
                a=SlotScore(adequacy=5, fluency=5, coherence=5, style=5),
                b=SlotScore(adequacy=3, fluency=3, coherence=3, style=3),
            ), None
        return PairwiseOutput(prefer="A", rationale="A is better"), None


@pytest.mark.asyncio
async def test_judge_triple_maps_slots_back():
    router = _FakeRouter()
    triple = EvalTriple(
        paragraph_id="p1", source="hello", source_lang="en", target_lang="zh-Hans",
        abi="你好（abi）", baseline="你好（baseline）",
    )
    # Force ABI into slot A by seeding so slot A (the '5' scores, preferred) maps to abi.
    rng = random.Random(0)
    res = await judge_triple(router, triple, rng=rng)
    assert res is not None
    assert router.calls == 2
    # Whichever slot ABI landed in, the higher score + 'A' preference must map
    # consistently: the preferred system gets the slot-A (5,5,5,5) score.
    if res.prefer_system == "abi":
        assert res.abi.adequacy == 5
    else:
        assert res.baseline.adequacy == 5


@pytest.mark.asyncio
async def test_judge_skips_when_missing_side():
    router = _FakeRouter()
    triple = EvalTriple(
        paragraph_id="p1", source="hi", source_lang="en", target_lang="zh-Hans",
        abi="你好",  # no baseline
    )
    assert await judge_triple(router, triple, rng=random.Random(0)) is None
    assert router.calls == 0


# --- whole-book three-plane eval ---
_SRC_CH1 = (
    "All happy families are alike; each unhappy family is unhappy in its own way.\n\n"
    "Everything was in confusion in the Oblonskys' house. The wife had found out "
    "that the husband was carrying on an intrigue with a French girl.\n"
)
_TGT_CH1_GOOD = (
    "幸福的家庭都是相似的，不幸的家庭各有各的不幸。\n\n"
    "奥布隆斯基家里一片混乱。妻子发现丈夫和家里的一个法国女人有暧昧关系。\n"
)


def _make_project(tmp_path, *, translated: bool = True, glossary: bool = True):
    root = tmp_path / "0001_book"
    proj = BookProject(root)
    for d in ("state", "chapters/src", "chapters/translated", "glossary"):
        (root / d).mkdir(parents=True, exist_ok=True)
    state = PipelineState(
        book_slug="book", source_lang="en", target_lang="zh-Hans",
        source_target="en-zh-Hans", status=Status.TRANSLATED,
    )
    proj.save_state(state)
    (proj.chapters_src / "001_intro.md").write_text(_SRC_CH1, encoding="utf-8")
    if translated:
        (proj.chapters_translated / "001_intro.md").write_text(_TGT_CH1_GOOD, encoding="utf-8")
    if glossary:
        proj.terms_csv.write_text(
            "term,target,status,display_policy,forbidden_body_renderings,note\n"
            "family,家庭,locked,inline,,\n"
            "intrigue,暧昧,preferred,inline,,\n"
            "ignore_me,X,avoid,inline,,\n",
            encoding="utf-8",
        )
    return proj


def test_load_glossary_only_enforced(tmp_path):
    proj = _make_project(tmp_path)
    g = _load_glossary(proj)
    assert g == {"family": "家庭", "intrigue": "暧昧"}  # 'avoid' row excluded


def test_score_book_l2_good_translation(tmp_path):
    proj = _make_project(tmp_path)
    rep = score_book_l2(proj, source_lang="en", target_lang="zh-Hans")
    assert rep.n_chapters == 1
    assert rep.n_chapters_translated == 1
    assert rep.n_paragraphs >= 1
    assert rep.completeness == 1.0
    assert rep.glossary_terms == 2
    assert rep.verdict in {"PASS", "WARN"}
    assert "completeness_fail" not in rep.flag_counts


def test_score_book_l2_missing_translation_fails(tmp_path):
    proj = _make_project(tmp_path, translated=False)
    rep = score_book_l2(proj, source_lang="en", target_lang="zh-Hans")
    assert rep.n_chapters_translated == 0
    assert rep.completeness == 0.0
    assert rep.verdict == "FAIL"
    assert rep.chapters[0].translated_missing is True


def test_check_book_l3_no_epub_fails(tmp_path):
    proj = _make_project(tmp_path)
    l3 = check_book_l3(proj)
    assert l3.epub.epub_built is False
    assert l3.verdict == "FAIL"
    assert l3.spotcheck.ran is False


def test_eval_book_rolls_up_worst_plane(tmp_path):
    proj = _make_project(tmp_path)
    report = eval_book(proj)
    # L3 has no EPUB -> FAIL, so the combined verdict must be FAIL.
    assert report.l3.verdict == "FAIL"
    assert report.verdict == "FAIL"
    assert report.l2.source_lang == "en"
    assert report.book == "0001_book"
