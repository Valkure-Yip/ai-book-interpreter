"""Unit tests for the wmt24pp dataset adapter and shared dataset utilities.

We test the adapter through its ``stub=true`` path so the suite never hits
the Hugging Face Hub.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from abi.eval.datasets import (
    EvalDataset,
    compute_book_id,
    load_eval_dataset,
    materialize_to_book_file,
    parse_spec,
)


class TestSpecParser:
    def test_minimal(self) -> None:
        spec = parse_spec("wmt24pp:en-zh_CN:literary")
        assert spec.name == "wmt24pp"
        assert spec.language_pair == "en-zh_CN"
        assert spec.register == "literary"
        assert spec.options == {}

    def test_options(self) -> None:
        spec = parse_spec("wmt24pp:en-zh_CN:literary:limit_docs=2:stub=true")
        assert spec.options == {"limit_docs": "2", "stub": "true"}

    def test_canonical(self) -> None:
        spec = parse_spec("wmt24pp:en-zh_CN:literary:limit_docs=1")
        assert spec.as_canonical() == "wmt24pp:en-zh_CN:literary:limit_docs=1"

    def test_invalid_too_few_parts(self) -> None:
        with pytest.raises(ValueError):
            parse_spec("wmt24pp:en-zh_CN")

    def test_invalid_option(self) -> None:
        with pytest.raises(ValueError):
            parse_spec("wmt24pp:en-zh_CN:literary:bad-option")

    def test_book_id_stable_across_options(self) -> None:
        a = compute_book_id(parse_spec("wmt24pp:en-zh_CN:literary"))
        b = compute_book_id(parse_spec("wmt24pp:en-zh_CN:literary:limit_docs=1"))
        # limit_docs should NOT change the book_id (so smoke and full share
        # the same runs/ directory).
        assert a == b

    def test_book_id_changes_with_register(self) -> None:
        a = compute_book_id(parse_spec("wmt24pp:en-zh_CN:literary"))
        b = compute_book_id(parse_spec("wmt24pp:en-zh_CN:news"))
        assert a != b


class TestWmt24ppAdapterStub:
    def test_filters_register_and_bad_source(self) -> None:
        ds = load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true")
        # Stub corpus has 5 rows; 1 wrong-register + 1 is_bad_source filtered.
        assert len(ds.paragraphs) == 3
        assert ds.source_language == "en"
        assert ds.target_language == "zh_CN"
        assert ds.register == "literary"
        for p in ds.paragraphs:
            assert p.source_text
            assert p.reference_text

    def test_grouping_by_document(self) -> None:
        ds = load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true")
        # Two literary documents in the stub: doc-A (2 segs) and doc-C (1 seg).
        assert "doc-A" in ds.doc_to_paragraphs
        assert "doc-C" in ds.doc_to_paragraphs
        assert len(ds.doc_to_paragraphs["doc-A"]) == 2
        assert len(ds.doc_to_paragraphs["doc-C"]) == 1

    def test_segment_sort_order(self) -> None:
        ds = load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true")
        doc_a = ds.doc_to_paragraphs["doc-A"]
        assert [p.segment_id for p in doc_a] == ["1", "2"]

    def test_limit_docs_clamps(self) -> None:
        ds = load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true:limit_docs=1")
        # Only first document (lexically: doc-A) survives.
        assert set(ds.doc_to_paragraphs.keys()) == {"doc-A"}
        assert len(ds.paragraphs) == 2

    def test_stable_paragraph_ids(self) -> None:
        a = load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true")
        b = load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true")
        assert [p.paragraph_id for p in a.paragraphs] == [
            p.paragraph_id for p in b.paragraphs
        ]


class TestMaterialize:
    def test_writes_stable_file(self, tmp_path: Path) -> None:
        ds = load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true")
        p1 = materialize_to_book_file(ds, tmp_path)
        p2 = materialize_to_book_file(ds, tmp_path)
        assert p1 == p2
        assert p1.exists()
        body = p1.read_text(encoding="utf-8")
        # Every source paragraph should appear in the materialized file.
        for para in ds.paragraphs:
            assert para.source_text[:30] in body
        # Documents become Chapter N markers.
        assert "Chapter 1:" in body
        assert "Chapter 2:" in body

    def test_round_trip_through_ingest_preserves_paragraph_count(
        self, tmp_path: Path
    ) -> None:
        """The materialized file must ingest to the SAME number of paragraphs
        as the dataset, otherwise positional alignment will silently drift."""
        from abi.ir import ingest

        ds = load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true")
        path = materialize_to_book_file(ds, tmp_path)
        book, _ = ingest(path)
        paras = [p for p in book.iter_paragraphs() if p.source_text.strip()]
        assert len(paras) == len(ds.paragraphs)

    def test_round_trip_defuses_inline_heading_lookalikes(
        self, tmp_path: Path
    ) -> None:
        """Regression: real wmt24pp paragraphs can be a single ALL-CAPS short
        line (story titles like "GOOD RIDDANCE") or a "Chapter 1" / "1." line.
        ABI's TXT ingester would normally promote these to headings and silently
        delete the paragraph, breaking strict 1:1 alignment with references.
        """
        from abi.eval.datasets._base import DatasetParagraph
        from abi.ir import ingest

        # Hand-built mini dataset where every paragraph is a heading lookalike.
        traps = [
            "GOOD RIDDANCE",                # ALL_CAPS_SHORT
            "Chapter 1",                    # CHAPTER_PATTERNS
            "1. Introduction",              # numbered
            "PART I",                       # PART
            "Ordinary prose paragraph.",    # control
        ]
        ds = EvalDataset(
            book_id="trap-test",
            title="trap-test",
            source_language="en",
            target_language="zh",
            register="literary",
            paragraphs=[
                DatasetParagraph(
                    paragraph_id=f"p{i}",
                    source_text=t,
                    reference_text=f"ref-{i}",
                    document_id="d1",
                    segment_id=str(i),
                    position=i,
                )
                for i, t in enumerate(traps)
            ],
            doc_to_paragraphs={
                "d1": [
                    DatasetParagraph(
                        paragraph_id=f"p{i}",
                        source_text=t,
                        reference_text=f"ref-{i}",
                        document_id="d1",
                        segment_id=str(i),
                        position=i,
                    )
                    for i, t in enumerate(traps)
                ]
            },
        )
        path = materialize_to_book_file(ds, tmp_path)
        book, _ = ingest(path)
        paras = [p for p in book.iter_paragraphs() if p.source_text.strip()]
        assert len(paras) == len(ds.paragraphs), (
            f"expected {len(ds.paragraphs)} paragraphs after ingest, "
            f"got {len(paras)}: {[p.source_text for p in paras]}"
        )


class TestUnknownAdapter:
    def test_raises(self) -> None:
        with pytest.raises(ValueError):
            load_eval_dataset("nonexistent:foo:bar")


class TestEvalDatasetType:
    def test_freeze(self) -> None:
        ds = EvalDataset(
            book_id="x",
            title="t",
            source_language="en",
            target_language="zh",
            register="literary",
            paragraphs=[],
        )
        # frozen dataclass: cannot mutate
        with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
            ds.title = "y"  # type: ignore[misc]
