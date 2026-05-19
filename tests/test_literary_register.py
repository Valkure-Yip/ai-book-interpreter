"""Tests for the ``literary`` register added after the wmt24pp eval revealed
that the ``academic-formal`` defaults were making fiction sound like a public
notice (e.g. translating the narrator's "I" as ``本人``).

Covers:
- the Pydantic enum on ``BookOverviewOutput`` accepts ``"literary"``
- the type Literal on ``Register`` accepts ``"literary"``
- ``style_guide._DEFAULT_DIRECTIVES_ZH`` has a non-trivial entry for
  ``literary`` that explicitly bans the antipatterns we saw in the eval
- ``derive_style_guide`` round-trips literary register through the LLM
  fallback path (when the model fails to produce a styleguide JSON, we
  fall back to the canned defaults)
"""

from __future__ import annotations

import pytest

from abi.survey._schemas import BookOverviewOutput
from abi.survey.style_guide import _DEFAULT_DIRECTIVES_ZH, derive_style_guide
from abi.types.run import StyleConfig
from abi.types.survey import BookOverview


class TestRegisterEnum:
    def test_book_overview_output_accepts_literary(self) -> None:
        out = BookOverviewOutput.model_validate(
            {
                "thesis": "A short story about loss.",
                "target_audience": "general readers",
                "register": "literary",
                "tone_notes": "intimate first person",
            }
        )
        assert out.register == "literary"

    def test_book_overview_type_accepts_literary(self) -> None:
        ov = BookOverview(
            book_id="b",
            title="t",
            thesis="x",
            register="literary",
        )
        assert ov.register == "literary"

    def test_book_overview_rejects_unknown_register(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            BookOverviewOutput.model_validate(
                {"thesis": "x", "register": "vaporwave"}
            )


class TestLiteraryDefaults:
    """The canned ``_DEFAULT_DIRECTIVES_ZH["literary"]`` block is what the
    pipeline falls back to when the LLM styleguide deriver fails or omits
    register_directives. It must encode the lessons from the wmt24pp eval.
    """

    def test_block_exists_and_non_empty(self) -> None:
        assert "literary" in _DEFAULT_DIRECTIVES_ZH
        block = _DEFAULT_DIRECTIVES_ZH["literary"]
        assert isinstance(block, list)
        assert len(block) >= 5, "literary register needs at least a handful of directives"

    def test_block_forbids_overformal_first_person(self) -> None:
        """Eval regression: deepseek-v4-flash translated narrator "I" as 「本人」
        which the judge flagged as 'overly formal and stiff'. The default
        directives must explicitly call this out so the LLM styleguide deriver
        is biased away from it and so the fallback path is safe."""
        block_joined = " ".join(_DEFAULT_DIRECTIVES_ZH["literary"])
        assert "我" in block_joined, "must explicitly anchor 1st person on 我"
        # Any of these antipattern markers is sufficient — we don't want to
        # over-couple the test to one exact phrasing.
        antipatterns = ["本人", "鄙人", "在下"]
        assert any(
            ap in block_joined for ap in antipatterns
        ), f"expected one of {antipatterns} to be called out as forbidden"

    def test_block_calls_out_academic_avoidance(self) -> None:
        block_joined = " ".join(_DEFAULT_DIRECTIVES_ZH["literary"])
        assert "学术" in block_joined or "公文" in block_joined, (
            "literary directives must tell the translator NOT to use the "
            "academic-formal register"
        )

    def test_block_disjoint_from_academic_block(self) -> None:
        """Sanity: literary and academic-formal must produce different defaults
        — otherwise the register switch is a no-op."""
        literary = _DEFAULT_DIRECTIVES_ZH["literary"]
        academic = _DEFAULT_DIRECTIVES_ZH["academic-formal"]
        assert set(literary) != set(academic)


class TestStyleGuideFallback:
    """``derive_style_guide`` must keep working for register='literary' even
    when the LLM styleguide deriver throws (network error, schema mismatch,
    etc.). The fallback path picks ``_DEFAULT_DIRECTIVES_ZH[register]``."""

    @pytest.mark.asyncio
    async def test_fallback_picks_literary_directives(self) -> None:
        class _BoomRouter:
            async def invoke_structured(self, *_args, **_kwargs):
                raise RuntimeError("simulated LLM failure")

        overview = BookOverview(
            book_id="b",
            title="A Quiet City",
            thesis="A night in a quarantined town.",
            target_audience="general readers",
            register="literary",
            tone_notes="introspective, first person",
        )
        guide = await derive_style_guide(
            router=_BoomRouter(),  # type: ignore[arg-type]
            overview=overview,
            style=StyleConfig(),
            target_language="zh",
        )
        assert guide.register == "literary"
        # Fallback directives must be the literary defaults, not academic.
        assert guide.register_directives == _DEFAULT_DIRECTIVES_ZH["literary"]
        assert any("学术" in d or "公文" in d for d in guide.register_directives)
