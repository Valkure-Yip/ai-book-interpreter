"""Targeted unit tests for ``abi.translate.pipeline`` helpers.

Currently focused on the error-fallback semantics (``build_error_unit``):

A translator failure used to leak the source text through as the "translation"
which silently produced English-in-Chinese output downstream. The fix is that
``build_error_unit`` must:

  - emit an EMPTY ``translated_text`` (never the source)
  - carry ``schema_error`` + ``translation_failed`` flags so consumers know it
    is a hard failure, not a legitimate passthrough (which is reserved for
    code blocks / equations / empty paragraphs)
"""

from __future__ import annotations

import pytest

from abi.translate.pipeline import build_error_unit
from abi.types.book import Paragraph
from abi.types.ids import paragraph_id


@pytest.fixture
def english_para() -> Paragraph:
    return Paragraph(
        paragraph_id=paragraph_id("Hello world.", 0),
        kind="prose",
        source_text="Hello world.",
        position=0,
        section_id="s1",
    )


class TestBuildErrorUnit:
    def test_translated_text_is_empty_never_source(
        self, english_para: Paragraph
    ) -> None:
        """REGRESSION: pre-fix the error fallback set translated_text =
        source_text. That produced English-in-Chinese books and a 1/5 likert
        disaster on the news_commentary eval (sample 13)."""
        unit = build_error_unit(
            paragraph=english_para,
            exc=ValueError("malformed JSON from LLM"),
            target_language="zh",
            model="test-model",
        )
        assert unit.translated_text == ""
        assert unit.translated_text != english_para.source_text
        assert unit.source_text == english_para.source_text  # source preserved

    def test_flag_codes_are_schema_error_and_translation_failed(
        self, english_para: Paragraph
    ) -> None:
        unit = build_error_unit(
            paragraph=english_para,
            exc=ValueError("boom"),
            target_language="zh",
            model="m",
        )
        codes = sorted(f.code for f in unit.flags)
        assert codes == ["schema_error", "translation_failed"]

    def test_no_passthrough_flag_on_hard_failure(
        self, english_para: Paragraph
    ) -> None:
        """``passthrough`` is reserved for INTENTIONAL non-translation (code
        blocks, equations). A schema_error MUST NOT also carry ``passthrough``
        — that conflation is what hid the regression for so long."""
        unit = build_error_unit(
            paragraph=english_para,
            exc=ValueError("x"),
            target_language="zh",
            model="m",
        )
        assert all(f.code != "passthrough" for f in unit.flags)

    def test_zero_confidence(self, english_para: Paragraph) -> None:
        unit = build_error_unit(
            paragraph=english_para,
            exc=ValueError("x"),
            target_language="zh",
            model="m",
        )
        assert unit.confidence == 0.0

    def test_exception_class_in_notes(self, english_para: Paragraph) -> None:
        class CustomFailure(Exception):
            pass

        unit = build_error_unit(
            paragraph=english_para,
            exc=CustomFailure("details"),
            target_language="zh",
            model="m",
        )
        assert "CustomFailure" in unit.notes

    def test_long_exception_message_truncated(
        self, english_para: Paragraph
    ) -> None:
        very_long = "x" * 10_000
        unit = build_error_unit(
            paragraph=english_para,
            exc=ValueError(very_long),
            target_language="zh",
            model="m",
        )
        schema_flag = next(f for f in unit.flags if f.code == "schema_error")
        assert len(schema_flag.detail) <= 200
