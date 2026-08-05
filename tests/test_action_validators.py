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
    SpotcheckInput,
)
from abi.actions.effects import expand_expected_artifacts
from abi.actions.evidence import StagingEvidenceView
from abi.actions.validators import validate_evidence
from abi.epub.result import GateResult
from abi.project.artifacts import ArtifactStore, sha256_file
from abi.project.layout import BookProject
from abi.types.orchestration import ArtifactRef


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


def _validator_input(
    project: BookProject,
    capability: str,
    parameters: object,
    *,
    action_id: str,
) -> tuple[StagingEvidenceView, object]:
    store = ArtifactStore(project, None)
    writer = store.writer(action_id, 1)
    try:
        manifest = expand_expected_artifacts(capability, action_id, parameters)  # type: ignore[arg-type]
    except (AttributeError, KeyError, TypeError, ValueError):
        manifest = None
    if manifest is not None:
        for expected in manifest.entries:
            source = project.root / expected.canonical_relpath
            if capability == "release.prepare" and expected.evidence_role == "release_epub":
                released = sorted(project.release_dir.glob("*.epub"))
                if project.book_epub.is_file():
                    source = project.book_epub
                elif released:
                    source = released[0]
            if capability == "review.spotcheck" and not source.is_file():
                source.parent.mkdir(parents=True, exist_ok=True)
                if expected.evidence_role == "round_manifest":
                    source.write_text(
                        json.dumps(
                            {
                                "round_id": parameters.round_id,
                                "reviewers": list(parameters.reviewers),
                                "chapters": list(parameters.chapters),
                                "samples_per_agent": parameters.samples_per_agent,
                                "seed": parameters.seed,
                            }
                        ),
                        encoding="utf-8",
                    )
                elif expected.evidence_role == "review_samples" and source.suffix == ".json":
                    source.write_text(
                        json.dumps([{"unit_id": "001:p1", "chapter": parameters.chapters[0]}]),
                        encoding="utf-8",
                    )
                elif expected.evidence_role in {"review_summary", "review_gate"}:
                    source.write_text("{}", encoding="utf-8")
                else:
                    source.write_text("fixture", encoding="utf-8")
            if source.is_file():
                writer.write_bytes(
                    expected.canonical_relpath,
                    source.read_bytes(),
                    media_type=expected.media_type,
                    evidence_role=expected.evidence_role,
                    metadata=expected.metadata,
                )
    if not writer.entries:
        writer.write_text(
            "tmp/validator-probe.txt",
            "probe",
            evidence_role="probe",
        )
    bundle = writer.artifact_bundle()
    output_paths = {entry.canonical_relpath for entry in bundle.entries}
    committed = tuple(
        ArtifactRef(
            artifact_id=f"fixture:{path.relative_to(project.root).as_posix()}",
            relpath=path.relative_to(project.root).as_posix(),
            sha256=sha256_file(path),
            producer_action_id="fixture",
        )
        for path in sorted(project.root.rglob("*"))
        if path.is_file()
        and "state/staging" not in path.relative_to(project.root).as_posix()
        and path.relative_to(project.root).as_posix() not in output_paths
    )
    view = StagingEvidenceView.for_bundle(project, committed, bundle)
    store.close()
    return view, bundle


def test_unknown_capability_never_passes(tmp_path: Path) -> None:
    project = scaffold_without_ledger(tmp_path)
    view, bundle = _validator_input(
        project, "invented.capability", EmptyInput(), action_id="unknown"
    )
    result = validate_evidence("invented.capability", view, EmptyInput(), bundle)
    assert result.passed is False
    assert result.reason_code == "validator_not_registered"


def test_chapter_validator_checks_only_requested_chapters(tmp_path: Path) -> None:
    project = project_with_chapters(tmp_path, source=("001", "002"), translated=("001",))
    one_parameters = ChapterBatchInput(chapters=("001",))
    one_view, one_bundle = _validator_input(
        project, "chapter.translate", one_parameters, action_id="translate-one"
    )
    one = validate_evidence("chapter.translate", one_view, one_parameters, one_bundle)
    two_parameters = ChapterBatchInput(chapters=("002",))
    two_view, two_bundle = _validator_input(
        project, "chapter.translate", two_parameters, action_id="translate-two"
    )
    two = validate_evidence("chapter.translate", two_view, two_parameters, two_bundle)
    assert one.passed is True
    assert two.passed is False


def test_every_builtin_capability_has_an_exhaustive_validator(tmp_path: Path) -> None:
    project = scaffold_without_ledger(tmp_path)
    registry = build_action_registry()

    for spec in registry.specs():
        parameters = registry.get(spec.capability).input_model.model_construct()
        view, bundle = _validator_input(
            project, spec.capability, parameters, action_id=spec.capability.replace(".", "-")
        )
        decision = validate_evidence(spec.capability, view, parameters, bundle)
        assert decision.reason_code != "validator_not_registered"


def test_validator_rejects_the_wrong_typed_parameters(tmp_path: Path) -> None:
    project = scaffold_without_ledger(tmp_path)

    view, bundle = _validator_input(
        project, "chapter.translate", EmptyInput(), action_id="wrong-parameters"
    )
    result = validate_evidence("chapter.translate", view, EmptyInput(), bundle)

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

    parameters = BuildEpubInput()
    view, bundle = _validator_input(project, "epub.build", parameters, action_id="epub")
    result = validate_evidence("epub.build", view, parameters, bundle)

    assert result.passed is True


def test_spotcheck_validator_rejects_agent_forged_pass_report(tmp_path: Path) -> None:
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

    parameters = SpotcheckInput(
        round_id="round_001", reviewers=("agent_a", "agent_b"), chapters=("001",),
        samples_per_agent=1, seed=1,
    )
    view, bundle = _validator_input(project, "review.spotcheck", parameters, action_id="forged")
    result = validate_evidence("review.spotcheck", view, parameters, bundle)

    assert result.passed is False
    assert result.reason_code == "spotcheck_not_passed"


def _passing_summary() -> dict[str, object]:
    return {
        "average_score": 95,
        "lowest_score": 93,
        "open_p0_p1_p2": 0,
        "confidence": 0.91,
        "samples": [{"unit_id": "001:p1", "score": 93}],
    }


def _write_passing_summary(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_passing_summary()), encoding="utf-8")


def test_spotcheck_validator_recomputes_two_rounds_from_agent_summaries(
    tmp_path: Path,
) -> None:
    project = scaffold_without_ledger(tmp_path)
    for round_name in ("round_001", "round_002"):
        for label in ("agent_a", "agent_b"):
            _write_passing_summary(
                project.random_spotcheck_dir
                / round_name
                / "reviews"
                / f"{label}_summary.json"
            )
    from abi.qa.validator import evaluate_spotcheck_summaries

    summaries = {label: _passing_summary() for label in ("agent_a", "agent_b")}
    _, report = evaluate_spotcheck_summaries(
        round_id="round_002", summaries=summaries, prior_rounds=(summaries,)
    )
    (project.random_spotcheck_dir / "round_002/validation_report.json").write_bytes(report)
    parameters = SpotcheckInput(
        round_id="round_002", reviewers=("agent_a", "agent_b"), chapters=("001",),
        samples_per_agent=1, seed=2,
    )
    view, bundle = _validator_input(project, "review.spotcheck", parameters, action_id="valid")
    result = validate_evidence("review.spotcheck", view, parameters, bundle)

    assert result.passed is True


def test_spotcheck_validator_does_not_count_forged_prior_round(tmp_path: Path) -> None:
    project = scaffold_without_ledger(tmp_path)
    forged = project.random_spotcheck_dir / "round_001/validation_report.json"
    forged.parent.mkdir(parents=True)
    forged.write_text(
        json.dumps({"this_round_pass": True, "status": "PASS"}), encoding="utf-8"
    )
    for label in ("agent_a", "agent_b"):
        _write_passing_summary(
            project.random_spotcheck_dir
            / "round_002"
            / "reviews"
            / f"{label}_summary.json"
        )

    from abi.qa.validator import evaluate_spotcheck_summaries

    summaries = {label: _passing_summary() for label in ("agent_a", "agent_b")}
    _, report = evaluate_spotcheck_summaries(round_id="round_002", summaries=summaries)
    (project.random_spotcheck_dir / "round_002/validation_report.json").write_bytes(report)
    parameters = SpotcheckInput(
        round_id="round_002", reviewers=("agent_a", "agent_b"), chapters=("001",),
        samples_per_agent=1, seed=2,
    )
    view, bundle = _validator_input(project, "review.spotcheck", parameters, action_id="prior-forged")
    result = validate_evidence("review.spotcheck", view, parameters, bundle)

    assert result.passed is False
    assert result.reason_code == "spotcheck_not_passed"


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
                        "epub": "book_v0.0.1.epub",
                        "created_at": "2026-08-05T00:00:00+00:00",
                        "status": "PASS",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    (project.release_dir / "book_v0.0.1.epub").write_bytes(b"released")
    parameters = ReleaseInput(version="v0.0.1")
    view, bundle = _validator_input(project, "release.prepare", parameters, action_id="release-ok")
    result = validate_evidence("release.prepare", view, parameters, bundle)

    assert result.passed is True


def test_release_validator_rejects_old_pass_when_requested_version_is_missing(
    tmp_path: Path,
) -> None:
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
                        "epub": "book_v0.0.1.epub",
                        "created_at": "2026-08-05T00:00:00+00:00",
                        "status": "PASS",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (project.release_dir / "book_v0.0.1.epub").write_bytes(b"old")

    parameters = ReleaseInput(version="v0.0.2")
    view, bundle = _validator_input(project, "release.prepare", parameters, action_id="release-old")
    result = validate_evidence("release.prepare", view, parameters, bundle)

    assert result.passed is False
    assert result.reason_code == "release_not_passed"
