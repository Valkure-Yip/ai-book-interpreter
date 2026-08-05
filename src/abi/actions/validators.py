"""Exhaustive, fail-closed evidence validators for registered capabilities."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, ValidationError

from abi.actions.builtins.inputs import (
    BuildEpubInput,
    ChapterBatchInput,
    EmptyInput,
    ReleaseInput,
    ResearchInput,
    ReviewBatchInput,
    SourceIngestInput,
    SourceSplitInput,
)
from abi.actions.contracts import ActionValidator
from abi.project.layout import BookProject
from abi.types._base import FrozenModel
from abi.types.orchestration import GateDecision


class _GateReport(FrozenModel):
    ok: bool
    message: str
    hard_errors: int = 0
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    details: dict[str, object] = Field(default_factory=dict)


class _SpotcheckAgent(FrozenModel):
    label: str
    ok: bool
    confidence: float


class _SpotcheckReport(FrozenModel):
    round: str
    status: str
    release_confidence: float
    this_round_pass: bool
    current_run_pass_rounds_count: int
    current_run_pass_rounds_required: int
    agents: tuple[_SpotcheckAgent, ...]
    reasons: tuple[str, ...]


_ZERO_ISSUE_FIELDS = {
    "scope": "FULL_CHAPTER",
    "issues_found": "0",
    "fixes_applied": "0",
    "unresolved_blocking_issues": "0",
    "latest_round_status": "PASS",
    "allow_next_chapter": "true",
}


def _ok(*evidence_refs: str) -> GateDecision:
    return GateDecision(
        passed=True,
        reason_code="evidence_valid",
        message="Deterministic evidence satisfies the capability contract.",
        evidence_refs=evidence_refs,
    )


def _fail(reason_code: str, message: str) -> GateDecision:
    return GateDecision(passed=False, reason_code=reason_code, message=message)


def _wrong_parameters(capability: str, expected: type[FrozenModel]) -> GateDecision:
    return _fail(
        "invalid_validator_parameters",
        f"{capability} evidence requires {expected.__name__}; parse the Action parameters first.",
    )


def _require_type(
    capability: str,
    parameters: FrozenModel,
    expected: type[FrozenModel],
) -> GateDecision | None:
    if not isinstance(parameters, expected):
        return _wrong_parameters(capability, expected)
    try:
        expected.model_validate(parameters.model_dump())
    except ValidationError:
        return _wrong_parameters(capability, expected)
    return None


def _normalize_line(line: str) -> str:
    line = re.sub(r"[*`]", "", line)
    return re.sub(r"^\s*[#>\-]+\s*", "", line)


def _field_values(text: str, field: str) -> tuple[str, ...]:
    pattern = re.compile(rf"^\s*{field}\s*:\s*(.+?)\s*$", flags=re.IGNORECASE)
    return tuple(
        match.group(1).strip().strip('"').strip()
        for line in text.splitlines()
        if (match := pattern.match(_normalize_line(line))) is not None
    )


def _contains_pass(text: str, *, key: str = "result") -> bool:
    values = _field_values(text, key)
    return bool(values) and values[-1].upper() == "PASS"


def _has_zero_issue_pass(text: str) -> bool:
    return all(
        (values := _field_values(text, field))
        and values[-1].lower() == expected.lower()
        for field, expected in _ZERO_ISSUE_FIELDS.items()
    )


def _read_model(path: Path, model: type[FrozenModel]) -> FrozenModel | None:
    if not path.exists():
        return None
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None


def _validate_source_ingest(
    project: BookProject, parameters: FrozenModel
) -> GateDecision:
    if invalid := _require_type("source.ingest", parameters, SourceIngestInput):
        return invalid
    missing = tuple(
        project.rel(path)
        for path in (project.source_clean, project.source_manifest)
        if not path.exists()
    )
    if missing:
        return _fail("source_evidence_missing", f"Missing source evidence: {', '.join(missing)}")
    return _ok(project.rel(project.source_clean), project.rel(project.source_manifest))


def _validate_source_split(project: BookProject, parameters: FrozenModel) -> GateDecision:
    if invalid := _require_type("source.split", parameters, SourceSplitInput):
        return invalid
    chapters = tuple(sorted(project.chapters_src.glob("*.md")))
    if not project.toc_json.exists() or not chapters:
        return _fail(
            "source_split_evidence_missing",
            "source/toc.json and at least one chapters/src Markdown file are required.",
        )
    return _ok(project.rel(project.toc_json), *(project.rel(path) for path in chapters))


def _validate_research_global(project: BookProject, parameters: FrozenModel) -> GateDecision:
    if invalid := _require_type("research.global", parameters, ResearchInput):
        return invalid
    evidence = project.root / "qa/benchmark/global_research_ack.md"
    return _ok(project.rel(evidence)) if evidence.exists() else _fail(
        "global_research_missing", "qa/benchmark/global_research_ack.md is required."
    )


def _validate_research_book(project: BookProject, parameters: FrozenModel) -> GateDecision:
    if invalid := _require_type("research.book", parameters, ResearchInput):
        return invalid
    missing = tuple(
        project.rel(path)
        for path in (project.book_research, project.style_profile)
        if not path.exists()
    )
    return _fail("book_research_missing", f"Missing book research: {', '.join(missing)}") if missing else _ok(
        project.rel(project.book_research), project.rel(project.style_profile)
    )


def _validate_translation_trial(
    project: BookProject, parameters: FrozenModel
) -> GateDecision:
    if invalid := _require_type("translation.trial", parameters, EmptyInput):
        return invalid
    if not project.pretranslation_report.exists():
        return _fail("pretranslation_report_missing", "Pretranslation report is required.")
    if not _contains_pass(project.pretranslation_report.read_text(encoding="utf-8")):
        return _fail("pretranslation_not_passed", "Pretranslation report must conclude result: PASS.")
    return _ok(project.rel(project.pretranslation_report))


def _validate_glossary_prepare(
    project: BookProject, parameters: FrozenModel
) -> GateDecision:
    if invalid := _require_type("glossary.prepare", parameters, EmptyInput):
        return invalid
    if not project.terms_csv.exists() or not project.style_guide.exists():
        return _fail("glossary_evidence_missing", "Glossary terms and style guide are required.")
    rows = tuple(row for row in project.terms_csv.read_text(encoding="utf-8").splitlines() if row)
    if len(rows) < 2:
        return _fail("glossary_terms_empty", "glossary/terms.csv requires a header and term row.")
    return _ok(project.rel(project.terms_csv), project.rel(project.style_guide))


def _chapter_files(
    project: BookProject,
    parameters: ChapterBatchInput,
    directory: Path,
    suffix: str = ".md",
) -> tuple[Path, ...]:
    return tuple(directory / f"{chapter}{suffix}" for chapter in parameters.chapters)


def _validate_chapter_translate(
    project: BookProject, parameters: FrozenModel
) -> GateDecision:
    if invalid := _require_type("chapter.translate", parameters, ChapterBatchInput):
        return invalid
    assert isinstance(parameters, ChapterBatchInput)
    paths = _chapter_files(project, parameters, project.chapters_translated)
    missing = tuple(project.rel(path) for path in paths if not path.exists())
    if missing:
        return _fail("chapter_translation_missing", f"Missing requested translation: {', '.join(missing)}")
    return _ok(*(project.rel(path) for path in paths))


def _validate_chapter_control(
    project: BookProject, parameters: FrozenModel
) -> GateDecision:
    if invalid := _require_type("chapter.control", parameters, ChapterBatchInput):
        return invalid
    assert isinstance(parameters, ChapterBatchInput)
    for chapter in parameters.chapters:
        path = project.chapter_control(chapter)
        if not path.exists() or not _has_zero_issue_pass(path.read_text(encoding="utf-8")):
            return _fail(
                "chapter_control_not_passed",
                f"qa/chapter_controls/{chapter}.control.md must be a zero-issue PASS.",
            )
    return _ok(*(project.rel(project.chapter_control(item)) for item in parameters.chapters))


def _validate_chapter_review(project: BookProject, parameters: FrozenModel) -> GateDecision:
    if invalid := _require_type("chapter.review", parameters, ReviewBatchInput):
        return invalid
    assert isinstance(parameters, ReviewBatchInput)
    if not parameters.chapters:
        return _fail("chapter_review_scope_empty", "Chapter review requires explicit chapters.")
    refs: list[str] = []
    for chapter in parameters.chapters:
        gate = project.chapter_gate(chapter)
        final = project.chapters_final / f"{chapter}.md"
        if not gate.exists() or not _contains_pass(gate.read_text(encoding="utf-8")):
            return _fail("chapter_gate_not_passed", f"qa/gates/{chapter}.gate.md must PASS.")
        if not final.exists():
            return _fail("chapter_final_missing", f"chapters/final/{chapter}.md is required.")
        refs.extend((project.rel(gate), project.rel(final)))
    return _ok(*refs)


def _validate_preproduction_spec(
    project: BookProject, parameters: FrozenModel
) -> GateDecision:
    if invalid := _require_type("preproduction.spec", parameters, EmptyInput):
        return invalid
    return _ok(project.rel(project.production_spec)) if project.production_spec.exists() else _fail(
        "production_spec_missing", "preproduction/stage1/production_spec.md is required."
    )


def _validate_preproduction_sample(
    project: BookProject, parameters: FrozenModel
) -> GateDecision:
    if invalid := _require_type("preproduction.sample", parameters, BuildEpubInput):
        return invalid
    if not project.sample_review.exists() or not project.sample_epub.exists():
        return _fail("sample_evidence_missing", "Sample EPUB and sample review are required.")
    if not _contains_pass(
        project.sample_review.read_text(encoding="utf-8"), key="sample_review_status"
    ):
        return _fail("sample_review_not_passed", "sample_review_status must be PASS.")
    return _ok(project.rel(project.sample_epub), project.rel(project.sample_review))


def _valid_gate_report(path: Path) -> bool:
    report = _read_model(path, _GateReport)
    return isinstance(report, _GateReport) and report.ok and report.hard_errors == 0


def _validate_epub_build(project: BookProject, parameters: FrozenModel) -> GateDecision:
    if invalid := _require_type("epub.build", parameters, BuildEpubInput):
        return invalid
    if not project.book_epub.exists():
        return _fail("epub_missing", "output/book.epub is required.")
    for path, code in (
        (project.publication_lint_report, "publication_lint_not_passed"),
        (project.asset_manifest_report, "asset_manifest_not_passed"),
        (project.epubcheck_log, "epubcheck_not_passed"),
    ):
        if not _valid_gate_report(path):
            return _fail(code, f"{project.rel(path)} must contain a zero-error PASS report.")
    return _ok(
        project.rel(project.book_epub),
        project.rel(project.publication_lint_report),
        project.rel(project.asset_manifest_report),
        project.rel(project.epubcheck_log),
    )


def _validate_review_spotcheck(
    project: BookProject, parameters: FrozenModel
) -> GateDecision:
    if invalid := _require_type("review.spotcheck", parameters, ReviewBatchInput):
        return invalid
    from abi.qa.validator import validate_random_spotcheck

    deterministic = validate_random_spotcheck(project, require_pass=True)
    if not deterministic.ok:
        return _fail("spotcheck_not_passed", deterministic.summary())
    rounds = tuple(sorted(project.random_spotcheck_dir.glob("round_*")))
    if not rounds:
        return _fail("spotcheck_round_missing", "At least one random spot-check round is required.")
    report_path = rounds[-1] / "validation_report.json"
    report = _read_model(report_path, _SpotcheckReport)
    if not isinstance(report, _SpotcheckReport):
        return _fail("spotcheck_report_invalid", "Latest validation_report.json is missing or invalid.")
    if (
        report.status.upper() != "PASS"
        or report.release_confidence < 0.80
        or not report.this_round_pass
        or report.current_run_pass_rounds_count < report.current_run_pass_rounds_required
        or len(report.agents) < 2
        or not all(agent.ok for agent in report.agents)
    ):
        return _fail("spotcheck_not_passed", "Latest spot-check must PASS at confidence >= 0.80.")
    return _ok(project.rel(report_path))


def _validate_review_independent(
    project: BookProject, parameters: FrozenModel
) -> GateDecision:
    if invalid := _require_type("review.independent", parameters, ReviewBatchInput):
        return invalid
    refs: list[str] = []
    for reviewer in ("agent_a", "agent_b"):
        path = project.root / f"reviews/{reviewer}/review.md"
        if not path.exists() or not _contains_pass(path.read_text(encoding="utf-8")):
            return _fail(
                "independent_review_not_passed",
                f"reviews/{reviewer}/review.md must conclude result: PASS.",
            )
        refs.append(project.rel(path))
    return _ok(*refs)


def _validate_release_prepare(project: BookProject, parameters: FrozenModel) -> GateDecision:
    if invalid := _require_type("release.prepare", parameters, ReleaseInput):
        return invalid
    assert isinstance(parameters, ReleaseInput)
    from abi.release.create import validate_created_release

    result = validate_created_release(project, version=parameters.version)
    if not result.ok:
        return _fail("release_not_passed", result.summary())
    artifact = result.details.get("artifact")
    return _ok(str(artifact)) if isinstance(artifact, str) else _fail(
        "release_not_passed", "Release validation did not identify an artifact."
    )


def _validate_output_finalize(project: BookProject, parameters: FrozenModel) -> GateDecision:
    if invalid := _require_type("output.finalize", parameters, EmptyInput):
        return invalid
    return _ok(project.rel(project.final_manifest)) if project.final_manifest.exists() else _fail(
        "final_manifest_missing", "output/final_manifest.md is required."
    )


def _validate_retrospective_capture(
    project: BookProject, parameters: FrozenModel
) -> GateDecision:
    if invalid := _require_type("retrospective.capture", parameters, EmptyInput):
        return invalid
    suggestions = project.root / "retrospective/template_update_suggestions.md"
    if not project.retrospective.exists() or not suggestions.exists():
        return _fail("retrospective_missing", "Both retrospective reports are required.")
    return _ok(project.rel(project.retrospective), project.rel(suggestions))


_VALIDATORS: Mapping[str, ActionValidator] = {
    "source.ingest": _validate_source_ingest,
    "source.split": _validate_source_split,
    "research.global": _validate_research_global,
    "research.book": _validate_research_book,
    "translation.trial": _validate_translation_trial,
    "glossary.prepare": _validate_glossary_prepare,
    "chapter.translate": _validate_chapter_translate,
    "chapter.control": _validate_chapter_control,
    "chapter.review": _validate_chapter_review,
    "preproduction.spec": _validate_preproduction_spec,
    "preproduction.sample": _validate_preproduction_sample,
    "epub.build": _validate_epub_build,
    "review.spotcheck": _validate_review_spotcheck,
    "review.independent": _validate_review_independent,
    "release.prepare": _validate_release_prepare,
    "output.finalize": _validate_output_finalize,
    "retrospective.capture": _validate_retrospective_capture,
}


def validator_catalog() -> dict[str, ActionValidator]:
    """Return the closed validator bindings used by ActionRegistry startup checks."""
    return dict(_VALIDATORS)


def validate_evidence(
    capability: str, project: BookProject, parameters: FrozenModel
) -> GateDecision:
    validator = _VALIDATORS.get(capability)
    if validator is None:
        return GateDecision(
            passed=False,
            reason_code="validator_not_registered",
            message=f"No validator for {capability}; register one before authorizing this Action.",
        )
    return validator(project, parameters)
