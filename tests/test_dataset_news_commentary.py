"""Unit tests for the ``Helsinki-NLP/news_commentary`` adapter.

All tests use the adapter's ``stub=true`` mode so we never touch the Hub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from abi.eval.datasets import (
    compute_book_id,
    load_eval_dataset,
    materialize_to_book_file,
    parse_spec,
)
from abi.eval.datasets.news_commentary import (
    _build_dataset,
    _detect_doc_boundaries,
    _make_doc_id,
)

SPEC = "news_commentary:en-zh:academic-accessible:stub=true"


def _row(idx: int, en: str, zh: str) -> dict[str, Any]:
    return {"id": idx, "translation": {"en": en, "zh": zh}}


class TestAdapterStub:
    def test_basic_load(self) -> None:
        ds = load_eval_dataset(SPEC)
        assert ds.source_language == "en"
        assert ds.target_language == "zh"
        assert ds.register == "academic-accessible"
        assert len(ds.paragraphs) == 6

    def test_groups_into_two_documents(self) -> None:
        ds = load_eval_dataset(SPEC)
        # Stub has two titles ("1929 or 1989?" and "What Failed in 2008?")
        # plus their body paragraphs.
        assert len(ds.doc_to_paragraphs) == 2
        sizes = sorted(len(v) for v in ds.doc_to_paragraphs.values())
        assert sizes == [3, 3]

    def test_paragraph_positions_are_sequential(self) -> None:
        ds = load_eval_dataset(SPEC)
        assert [p.position for p in ds.paragraphs] == list(range(len(ds.paragraphs)))

    def test_stable_paragraph_ids(self) -> None:
        a = load_eval_dataset(SPEC)
        b = load_eval_dataset(SPEC)
        assert [p.paragraph_id for p in a.paragraphs] == [
            p.paragraph_id for p in b.paragraphs
        ]
        # Globally unique within the dataset.
        assert len({p.paragraph_id for p in a.paragraphs}) == len(a.paragraphs)

    def test_limit_docs(self) -> None:
        ds = load_eval_dataset(SPEC + ":limit_docs=1")
        # Only one of the two documents survives.
        assert len(ds.doc_to_paragraphs) == 1
        # 3 rows = title + 2 body paragraphs.
        assert len(ds.paragraphs) == 3

    def test_book_id_stable_across_options(self) -> None:
        a = compute_book_id(parse_spec("news_commentary:en-zh:academic-accessible"))
        b = compute_book_id(
            parse_spec("news_commentary:en-zh:academic-accessible:limit_docs=1")
        )
        assert a == b


class TestTitleHeuristic:
    """The doc-boundary heuristic is the heart of this adapter — pin it down."""

    def test_title_followed_by_long_body_is_detected(self) -> None:
        rows = [
            _row(0, "Short Title", "短标题"),
            _row(1, "A" * 200, "B" * 200),
            _row(2, "C" * 200, "D" * 200),
        ]
        assert _detect_doc_boundaries(rows, title_max_chars=90, body_min_chars=150) == [0]

    def test_short_row_without_long_body_is_not_a_title(self) -> None:
        # Two short rows in a row → neither is a body-paragraph boundary; we
        # still synthesize a boundary at 0 so every row is covered.
        rows = [
            _row(0, "Short Title", "短标题"),
            _row(1, "Short Sentence.", "短句"),
            _row(2, "Another short one.", "另一句"),
        ]
        boundaries = _detect_doc_boundaries(
            rows, title_max_chars=90, body_min_chars=150
        )
        assert boundaries == [0]

    def test_no_title_at_start_inserts_synthetic_boundary(self) -> None:
        rows = [
            _row(0, "A" * 200, "B" * 200),
            _row(1, "C" * 200, "D" * 200),
            _row(2, "New Title", "新标题"),
            _row(3, "E" * 200, "F" * 200),
        ]
        boundaries = _detect_doc_boundaries(
            rows, title_max_chars=90, body_min_chars=150
        )
        assert boundaries == [0, 2]

    def test_thresholds_are_configurable(self) -> None:
        rows = [
            _row(0, "Borderline title that is a bit longer than ninety chars "
                    "but still not a full paragraph really.", "..."),
            _row(1, "A" * 200, "B" * 200),
        ]
        # Default threshold (90) rejects this row.
        default_boundaries = _detect_doc_boundaries(
            rows, title_max_chars=90, body_min_chars=150
        )
        assert default_boundaries == [0]
        # Raise threshold so the long-ish title qualifies.
        relaxed = _detect_doc_boundaries(
            rows, title_max_chars=200, body_min_chars=150
        )
        # The first row qualifies AND a synthetic 0 boundary is the same row.
        assert relaxed == [0]

    def test_consecutive_titles(self) -> None:
        """Real corpus pattern: title → body → body → title → body → body."""
        rows = [
            _row(0, "Title A", "标题A"),
            _row(1, "A" * 250, "BBB"),
            _row(2, "C" * 250, "DDD"),
            _row(3, "Title B", "标题B"),
            _row(4, "E" * 250, "FFF"),
            _row(5, "G" * 250, "HHH"),
        ]
        assert _detect_doc_boundaries(
            rows, title_max_chars=90, body_min_chars=150
        ) == [0, 3]


class TestDocIdGeneration:
    def test_slug_from_title(self) -> None:
        row = _row(0, "1929 or 1989?", "1929或1989?")
        assert _make_doc_id(row, 0) == "1929_or_1989"

    def test_fallback_when_empty(self) -> None:
        row = _row(0, "   ", "...")
        assert _make_doc_id(row, 42).startswith("news_commentary_doc_")

    def test_collisions_disambiguated_by_adapter(self) -> None:
        # Two identical titles → second doc gets a __2 suffix in _build_dataset.
        rows = [
            _row(0, "Same Title", "同样标题"),
            _row(1, "A" * 200, "B" * 200),
            _row(2, "Same Title", "同样标题"),
            _row(3, "C" * 200, "D" * 200),
        ]
        spec = parse_spec("news_commentary:en-zh:academic-accessible")
        ds = _build_dataset(spec, rows)
        keys = list(ds.doc_to_paragraphs)
        assert keys[0] == "Same_Title"
        assert keys[1] == "Same_Title__2"


class TestLanguagePair:
    def test_filters_missing_targets(self) -> None:
        rows = [
            _row(0, "Title", "标题"),
            _row(1, "A" * 200, "B" * 200),
            # Missing zh translation → dropped.
            {"id": 2, "translation": {"en": "C" * 200, "zh": ""}},
            _row(3, "D" * 200, "E" * 200),
        ]
        spec = parse_spec("news_commentary:en-zh:academic-accessible")
        ds = _build_dataset(spec, rows)
        assert len(ds.paragraphs) == 3
        for p in ds.paragraphs:
            assert p.source_text and p.reference_text


class TestMaterialize:
    def test_round_trip_through_ingest_preserves_paragraph_count(
        self, tmp_path: Path
    ) -> None:
        from abi.ir import ingest

        ds = load_eval_dataset(SPEC)
        path = materialize_to_book_file(ds, tmp_path)
        book, _ = ingest(path)
        paras = [p for p in book.iter_paragraphs() if p.source_text.strip()]
        assert len(paras) == len(ds.paragraphs)

    def test_documents_become_chapters(self, tmp_path: Path) -> None:
        ds = load_eval_dataset(SPEC)
        path = materialize_to_book_file(ds, tmp_path)
        body = path.read_text(encoding="utf-8")
        # Two stub documents → Chapter 1 + Chapter 2.
        assert "Chapter 1:" in body
        assert "Chapter 2:" in body


class TestSpecRoundTrip:
    def test_unknown_option_does_not_crash(self) -> None:
        # Unknown options are tolerated (e.g. user passes a tag we don't use).
        ds = load_eval_dataset(SPEC + ":mystery_flag=1")
        assert len(ds.paragraphs) > 0

    def test_invalid_threshold_rejected(self) -> None:
        with pytest.raises(ValueError):
            load_eval_dataset(SPEC + ":title_max_chars=0")
