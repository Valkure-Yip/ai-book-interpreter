"""Unit tests for the heading-rendering helper in assemble.

Pure logic only (no LLM, no IO). Locks down that the translated heading is
used when available, with bilingual mode showing both, and that empty / missing
entries fall back gracefully to the source heading.
"""

from __future__ import annotations

from abi.assemble.pipeline import _heading_md
from abi.types.book import Section


def _sec(sid: str = "s1", level: int = 1, heading: str = "1 Grow") -> Section:
    return Section(
        section_id=sid,
        level=level,
        heading=heading,
        heading_trail=[heading],
        paragraphs=[],
        children=[],
    )


class TestHeadingMd:
    def test_no_map_falls_back_to_source(self) -> None:
        out = _heading_md(_sec(), base_level=2)
        assert out == "## 1 Grow"

    def test_empty_map_falls_back_to_source(self) -> None:
        out = _heading_md(_sec(), base_level=2, headings_map={})
        assert out == "## 1 Grow"

    def test_translated_heading_is_used(self) -> None:
        out = _heading_md(
            _sec(sid="s1"),
            base_level=2,
            headings_map={"s1": "1 增长"},
        )
        assert out == "## 1 增长"

    def test_bilingual_includes_both(self) -> None:
        out = _heading_md(
            _sec(sid="s1"),
            base_level=2,
            headings_map={"s1": "1 增长"},
            bilingual=True,
        )
        assert out == "## 1 增长 (1 Grow)"

    def test_bilingual_falls_back_when_no_translation(self) -> None:
        out = _heading_md(_sec(sid="orphan"), base_level=2, headings_map={"other": "X"},
                          bilingual=True)
        assert out == "## 1 Grow"

    def test_identical_translation_does_not_duplicate(self) -> None:
        """When LLM echoes source (e.g. proper-noun heading), avoid '## X (X)'."""
        out = _heading_md(
            _sec(sid="s1", heading="NATO"),
            base_level=2,
            headings_map={"s1": "NATO"},
            bilingual=True,
        )
        assert out == "## NATO"

    def test_whitespace_only_translation_falls_back(self) -> None:
        out = _heading_md(
            _sec(sid="s1"),
            base_level=2,
            headings_map={"s1": "   "},
        )
        assert out == "## 1 Grow"

    def test_level_calculation(self) -> None:
        # base_level=2 + section.level=3 -> #### (4)
        out = _heading_md(_sec(level=3, heading="Sub"), base_level=2)
        assert out == "#### Sub"

    def test_level_clamped_to_six(self) -> None:
        out = _heading_md(_sec(level=10, heading="X"), base_level=2)
        assert out.startswith("######")
        assert "X" in out
