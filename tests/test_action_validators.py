"""Fail-closed tests for capability evidence routing."""

from __future__ import annotations

import json
from pathlib import Path

from abi.actions.builtins.catalog import build_action_registry
from abi.actions.builtins.inputs import (
    BuildEpubInput,
    ChapterBatchInput,
    EmptyInput,
    ReleaseInput,
    ReviewBatchInput,
)
from abi.actions.validators import validate_evidence
from abi.epub.result import GateResult
from abi.project.layout import BookProject


def scaffold_without_ledger(tmp_path: Path) -> BookProject:
    project = BookProject(tmp_path)
    for directory in (
        project.chapters_src,
        project.chapters_translated,
        project.chapters_final,
        project.root / "qa/chapter_controls",
        project.root / "qa/gates",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return project


def project_with_chapters(
    tmp_path: Path,
    *,
    source: tuple[str, ...],
    translated: tuple[str, ...],
) -> BookProject:
    project = scaffold_without_ledger(tmp_path)
    for chapter in source:
        (project.chapters_src / f"{chapter}.md").write_text("source", encoding="utf-8")
    for chapter in translated:
        (project.chapters_translated / f"{chapter}.md").write_text(
            "translated", encoding="utf-8"
        )
    return project


def test_unknown_capability_never_passes(tmp_path: Path) -> None:
    project = scaffold_without_ledger(tmp_path)
    result = validate_evidence("invented.capability", project, EmptyInput())
    assert result.passed is False
    assert result.reason_code == "validator_not_registered"


def test_chapter_validator_checks_only_requested_chapters(tmp_path: Path) -> None:
    project = project_with_chapters(tmp_path, source=("001", "002"), translated=("001",))
    one = validate_evidence("chapter.translate", project, ChapterBatchInput(chapters=("001",)))
    two = validate_evidence("chapter.translate", project, ChapterBatchInput(chapters=("002",)))
    assert one.passed is True
    assert two.passed is False


def test_every_builtin_capability_has_an_exhaustive_validator(tmp_path: Path) -> None:
    project = scaffold_without_ledger(tmp_path)
    registry = build_action_registry()

    for spec in registry.specs():
        parameters = registry.get(spec.capability).input_model.model_construct()
        decision = validate_evidence(spec.capability, project, parameters)
        assert decision.reason_code != "validator_not_registered"


def test_validator_rejects_the_wrong_typed_parameters(tmp_path: Path) -> None:
    project = scaffold_without_ledger(tmp_path)

    result = validate_evidence("chapter.translate", project, EmptyInput())

    assert result.passed is False
    assert result.reason_code == "invalid_validator_parameters"


def test_epub_validator_accepts_existing_gate_report_shape(tmp_path: Path) -> None:
    project = scaffold_without_ledger(tmp_path)
    project.book_epub.parent.mkdir(parents=True, exist_ok=True)
    project.book_epub.write_bytes(b"epub")
    for report in (
        project.publication_lint_report,
        project.asset_manifest_report,
        project.epubcheck_log,
    ):
        GateResult(True, "clean", details={"checked": True}).write_json(report)

    result = validate_evidence("epub.build", project, BuildEpubInput())

    assert result.passed is True


def test_spotcheck_validator_accepts_existing_report_shape(tmp_path: Path) -> None:
    project = scaffold_without_ledger(tmp_path)
    report = project.random_spotcheck_dir / "round_001/validation_report.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        json.dumps(
            {
                "round": "round_001",
                "status": "PASS",
                "release_confidence": 0.91,
                "this_round_pass": True,
                "current_run_pass_rounds_count": 2,
                "current_run_pass_rounds_required": 2,
                "agents": [{"label": "agent_a", "ok": True, "confidence": 0.91}],
                "reasons": [],
            }
        ),
        encoding="utf-8",
    )

    result = validate_evidence("review.spotcheck", project, ReviewBatchInput())

    assert result.passed is True


def test_release_validator_accepts_existing_state_shape(tmp_path: Path) -> None:
    project = scaffold_without_ledger(tmp_path)
    state = project.release_dir / "release_state.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "book": "fixture",
                "producer": "ABI",
                "latest_status": "PASS",
                "latest_version": "v0.0.1",
                "releases": [
                    {
                        "version": "v0.0.1",
                        "epub": "fixture_v0.0.1.epub",
                        "created_at": "2026-08-05T00:00:00+00:00",
                        "status": "PASS",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = validate_evidence("release.prepare", project, ReleaseInput())

    assert result.passed is True
