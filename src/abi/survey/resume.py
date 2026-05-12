"""Reconstruct ``SurveyResult`` from on-disk artifacts of a previous run.

Returns ``None`` when artifacts are missing or unparseable, signaling that
the caller should re-run Pass 1 from scratch. We deliberately use an
"all-or-nothing" policy: partial survey resume invites subtle bugs (e.g.
glossary built against an older chapter summary set), so we either reuse
everything or rebuild everything.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from abi.survey.heading_translator import HeadingMap
from abi.types.glossary import Glossary
from abi.types.survey import BookOverview, ChapterSummary, StyleGuide

_log = logging.getLogger(__name__)


def try_load_survey(survey_dir: Path) -> tuple[
    list[ChapterSummary], BookOverview, Glossary, StyleGuide, HeadingMap
] | None:
    required = [
        survey_dir / "overview.json",
        survey_dir / "glossary.json",
        survey_dir / "style-guide.json",
        survey_dir / "chapters",
    ]
    if not all(p.exists() for p in required):
        return None

    try:
        overview = BookOverview.model_validate_json(
            (survey_dir / "overview.json").read_text(encoding="utf-8")
        )
        glossary = Glossary.model_validate_json(
            (survey_dir / "glossary.json").read_text(encoding="utf-8")
        )
        style_guide = StyleGuide.model_validate_json(
            (survey_dir / "style-guide.json").read_text(encoding="utf-8")
        )
        chapters: list[ChapterSummary] = []
        for f in sorted((survey_dir / "chapters").glob("*.json")):
            chapters.append(
                ChapterSummary.model_validate_json(f.read_text(encoding="utf-8"))
            )

        target_lang = style_guide.target_language
        headings = HeadingMap(by_section_id={}, target_language=target_lang)
        headings_path = survey_dir / "headings.json"
        if headings_path.exists():
            data = json.loads(headings_path.read_text(encoding="utf-8"))
            headings = HeadingMap(
                by_section_id=dict(data.get("by_section_id", {})),
                target_language=data.get("target_language", target_lang),
            )
    except Exception as exc:
        _log.warning("survey resume failed (%s); will re-run Pass 1", exc)
        return None

    return chapters, overview, glossary, style_guide, headings
