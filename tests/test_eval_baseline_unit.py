"""Unit tests for baseline chunking & save/load round-trip."""

from __future__ import annotations

from pathlib import Path

from abi.eval.baseline import BaselineResult, _split_into_chunks, load_baseline, save_baseline
from abi.types.eval import BaselineMeta


class TestSplit:
    def test_single_short_text_one_chunk(self) -> None:
        text = "para one\n\npara two\n\npara three"
        chunks = _split_into_chunks(text, char_budget=10_000)
        assert chunks == [text]

    def test_splits_on_paragraph_boundary(self) -> None:
        # 3 paragraphs, each ~50 chars, budget 100 → expect 2-3 chunks
        text = "\n\n".join(["x" * 50, "y" * 50, "z" * 50])
        chunks = _split_into_chunks(text, char_budget=120)
        assert len(chunks) >= 2
        # No chunk should be split mid-paragraph: each chunk's paragraphs
        # joined back together should be equal to the original paragraph list.
        original_paras = [p for p in text.split("\n\n") if p]
        chunk_paras = [p for c in chunks for p in c.split("\n\n") if p]
        assert chunk_paras == original_paras

    def test_oversize_single_paragraph_gets_own_chunk(self) -> None:
        text = "x" * 10_000
        chunks = _split_into_chunks(text, char_budget=100)
        assert len(chunks) == 1
        assert chunks[0] == text


class TestSaveLoad:
    def test_round_trip(self, tmp_path: Path) -> None:
        result = BaselineResult(
            full_text="译文段一\n\n译文段二",
            paragraphs=["译文段一", "译文段二"],
            meta=BaselineMeta(
                model="test-model",
                base_url="https://x/v1",
                chunks=1,
                tokens_in=100,
                tokens_out=50,
                cost_usd=0.001,
                latency_ms=1234,
                chunk_token_budget=50_000,
                output_chars=8,
            ),
        )
        save_baseline(result, tmp_path)
        loaded = load_baseline(tmp_path)
        assert loaded is not None
        assert loaded.paragraphs == result.paragraphs
        assert loaded.meta.model == "test-model"
        assert loaded.meta.chunks == 1

    def test_load_returns_none_when_missing(self, tmp_path: Path) -> None:
        assert load_baseline(tmp_path) is None
