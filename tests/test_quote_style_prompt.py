"""Regression: ``Quote style`` directive must instruct the model to apply the
quote marks ONLY when the SOURCE itself quotes a string.

Pre-fix, the prompt said just ``Quote style: 「」`` which the LLM read as
"wrap technical terms in 「...」". On the news_commentary eval, ABI emitted
``「全球双极格局」`` while baseline (no such directive) wrote ``全球两极格局``
unquoted — and the judge dinged ABI for "unnecessary quotation marks".

The fix is a one-line clarification in the prompt body. We pin it down with
assertions on the rendered prompt strings.
"""

from __future__ import annotations

import pytest

from abi.prompts import get_registry


def _render_paragraph_translator() -> str:
    return get_registry().render(
        "paragraph_translator",
        target_language="zh",
        source_language="en",
        register="academic-accessible",
        quote_style="「」",
        directives=["..."],
        glossary=[],
        book_thesis="t",
        book_audience="a",
        heading_trail=["Chapter 1"],
        position_in_chapter=1,
        chapter_length=1,
        chapter_abstract="",
        crossed_chapter_boundary=False,
        prev_window=[],
        next_window=[],
        anchors=[],
        target={"id": "p1", "source": "Hello.", "kind": "prose"},
    )


def _render_batch_translator() -> str:
    return get_registry().render(
        "paragraph_batch_translator",
        target_language="zh",
        source_language="en",
        register="academic-accessible",
        quote_style="「」",
        directives=["..."],
        glossary=[],
        book_thesis="t",
        book_audience="a",
        heading_trail=["Chapter 1"],
        chapter_abstract="",
        crossed_chapter_boundary=False,
        prev_window=[],
        next_window=[],
        anchors=[],
        targets=[{"id": "p1", "kind": "prose", "source": "Hello."}],
    )


@pytest.mark.parametrize(
    "rendered",
    [_render_paragraph_translator(), _render_batch_translator()],
    ids=["single", "batch"],
)
class TestQuoteStyleSemantics:
    def test_quote_style_text_present(self, rendered: str) -> None:
        assert "Quote style: 「」" in rendered

    def test_prompt_clarifies_source_only(self, rendered: str) -> None:
        """The crucial guardrail — the prompt MUST tell the model to only
        apply the quote_style when the source itself contains quotes."""
        assert "SOURCE itself" in rendered
        # Some explicit "ONLY" qualifier.
        assert "ONLY" in rendered

    def test_prompt_forbids_wrapping_technical_terms(
        self, rendered: str
    ) -> None:
        assert "Do NOT" in rendered or "do NOT" in rendered
        # Must explicitly mention the failure mode we observed.
        assert "technical terms" in rendered

    def test_plain_prose_directive(self, rendered: str) -> None:
        assert "Plain prose stays plain" in rendered
