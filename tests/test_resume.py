"""Tests for resume helpers: ``find_run_dir`` and ``try_load_survey``."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from abi.runtime.manifest import find_run_dir, latest_run_for, runs_root
from abi.survey.resume import try_load_survey
from abi.types.glossary import Glossary, GlossaryEntry
from abi.types.survey import (
    BookOverview,
    ChapterSummary,
    StyleGuide,
)


def _set_runs_root(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("ABI_RUNS_DIR", str(root))


def test_runs_root_respects_env(tmp_path: Path, monkeypatch) -> None:
    _set_runs_root(monkeypatch, tmp_path)
    assert runs_root() == tmp_path


def test_find_run_dir_with_book_id(tmp_path: Path, monkeypatch) -> None:
    _set_runs_root(monkeypatch, tmp_path)
    target = tmp_path / "bookA" / "20260101T000000Z-abc"
    target.mkdir(parents=True)
    found = find_run_dir(run_id="20260101T000000Z-abc", book_id="bookA")
    assert found == target


def test_find_run_dir_without_book_id_scans(tmp_path: Path, monkeypatch) -> None:
    _set_runs_root(monkeypatch, tmp_path)
    target = tmp_path / "bookX" / "20260101T000000Z-xyz"
    target.mkdir(parents=True)
    # Also create a decoy book dir
    (tmp_path / "bookY").mkdir()
    found = find_run_dir(run_id="20260101T000000Z-xyz")
    assert found == target


def test_find_run_dir_missing_returns_none(tmp_path: Path, monkeypatch) -> None:
    _set_runs_root(monkeypatch, tmp_path)
    assert find_run_dir(run_id="nonexistent") is None


def test_latest_run_for_picks_newest_by_name(tmp_path: Path, monkeypatch) -> None:
    _set_runs_root(monkeypatch, tmp_path)
    book = tmp_path / "bookZ"
    (book / "20260101T000000Z-aaa").mkdir(parents=True)
    (book / "20260301T000000Z-bbb").mkdir(parents=True)
    (book / "20260201T000000Z-ccc").mkdir(parents=True)
    assert latest_run_for("bookZ").name == "20260301T000000Z-bbb"


def test_latest_run_for_missing_book(tmp_path: Path, monkeypatch) -> None:
    _set_runs_root(monkeypatch, tmp_path)
    assert latest_run_for("ghost") is None


def _fake_overview() -> BookOverview:
    return BookOverview(
        book_id="bbbb",
        title="t",
        thesis="Some thesis.",
        chapter_summaries=[
            ChapterSummary(section_id="s1", heading="C1", one_liner="hi", abstract="ab"),
        ],
        target_audience="readers",
        register="academic-formal",
    )


def _fake_glossary() -> Glossary:
    return Glossary(
        book_id="bbbb",
        target_language="zh",
        entries=[
            GlossaryEntry(
                term="cloud",
                surface_forms=["cloud"],
                target="云",
                definition="A cloud.",
                first_seen="x-0",
                locked=True,
                is_core=True,
                source="survey",
            )
        ],
    )


def _fake_style_guide() -> StyleGuide:
    return StyleGuide(book_id="bbbb", target_language="zh")


def _seed_survey_dir(survey_dir: Path) -> None:
    """Write a complete set of survey artifacts to disk."""
    survey_dir.mkdir(parents=True)
    (survey_dir / "chapters").mkdir()
    overview = _fake_overview()
    glossary = _fake_glossary()
    sg = _fake_style_guide()
    (survey_dir / "overview.json").write_text(overview.model_dump_json(), encoding="utf-8")
    (survey_dir / "glossary.json").write_text(glossary.model_dump_json(), encoding="utf-8")
    (survey_dir / "style-guide.json").write_text(sg.model_dump_json(), encoding="utf-8")
    (survey_dir / "chapters" / "s1.json").write_text(
        overview.chapter_summaries[0].model_dump_json(), encoding="utf-8"
    )
    (survey_dir / "headings.json").write_text(
        '{"target_language": "zh", "by_section_id": {"s1": "第一章"}}',
        encoding="utf-8",
    )


def test_try_load_survey_returns_none_when_missing(tmp_path: Path) -> None:
    assert try_load_survey(tmp_path / "survey") is None


def test_try_load_survey_loads_all_artifacts(tmp_path: Path) -> None:
    survey_dir = tmp_path / "survey"
    _seed_survey_dir(survey_dir)
    cached = try_load_survey(survey_dir)
    assert cached is not None
    chapters, overview, glossary, style_guide, headings = cached
    assert len(chapters) == 1
    assert chapters[0].section_id == "s1"
    assert overview.thesis == "Some thesis."
    assert len(glossary.entries) == 1
    assert style_guide.target_language == "zh"
    assert headings.by_section_id == {"s1": "第一章"}


def test_try_load_survey_without_headings_returns_empty_map(tmp_path: Path) -> None:
    """If headings.json is absent (older run), we still resume with an empty map."""
    survey_dir = tmp_path / "survey"
    _seed_survey_dir(survey_dir)
    (survey_dir / "headings.json").unlink()
    cached = try_load_survey(survey_dir)
    assert cached is not None
    assert cached[4].by_section_id == {}


def test_try_load_survey_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    survey_dir = tmp_path / "survey"
    _seed_survey_dir(survey_dir)
    (survey_dir / "overview.json").write_text("not-json", encoding="utf-8")
    assert try_load_survey(survey_dir) is None


def test_datetime_fixture_construction() -> None:
    # Sanity: pydantic models accept naive datetimes (used by BookOverview).
    BookOverview(
        book_id="b",
        title="t",
        thesis="x",
        chapter_summaries=[],
        target_audience="",
        register="academic-formal",
    )
    _ = datetime.utcnow()
