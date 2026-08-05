"""Fail-closed deterministic validators over one attempt's evidence overlay."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any, cast

from abi.actions.builtins.inputs import (
    BuildEpubInput,
    ChapterBatchInput,
    EmptyInput,
    ReleaseInput,
    ResearchInput,
    ReviewBatchInput,
    SourceIngestInput,
    SourceSplitInput,
    SpotcheckInput,
)
from abi.actions.contracts import ActionValidator
from abi.actions.effects import expand_expected_artifacts
from abi.actions.evidence import StagingEvidenceView
from abi.types._base import FrozenModel
from abi.types.orchestration import ArtifactBundle, GateDecision

Validator = Callable[[StagingEvidenceView, FrozenModel, ArtifactBundle], GateDecision]
_VERSION = "attempt-evidence-v1"


def _decision(
    view: StagingEvidenceView,
    *,
    passed: bool,
    reason_code: str,
    message: str,
    validator_id: str,
    evidence_refs: tuple[str, ...] = (),
) -> GateDecision:
    return GateDecision(
        passed=passed,
        reason_code=reason_code,
        message=message,
        validator_id=validator_id,
        validator_version=_VERSION,
        bundle_digest=view.bundle_digest,
        artifact_checksums=view.artifact_checksums,
        evidence_refs=evidence_refs,
    )


def _validate_expected(
    capability: str,
    view: StagingEvidenceView,
    parameters: FrozenModel,
    bundle: ArtifactBundle,
) -> GateDecision | None:
    try:
        expected = expand_expected_artifacts(capability, bundle.action_id, parameters)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return _decision(
            view,
            passed=False,
            reason_code="invalid_validator_parameters",
            message=f"{exc}; parse typed parameters and register exact effects.",
            validator_id=capability,
        )
    actual = tuple(
        (entry.canonical_relpath, entry.media_type, entry.evidence_role, entry.metadata)
        for entry in bundle.entries
    )
    required = tuple(
        (entry.canonical_relpath, entry.media_type, entry.evidence_role, entry.metadata)
        for entry in expected.entries
    )
    if actual != required:
        return _decision(
            view,
            passed=False,
            reason_code="artifact_effect_mismatch",
            message="Bundle entries do not exactly equal the parameter-expanded manifest.",
            validator_id=capability,
        )
    missing = tuple(entry.canonical_relpath for entry in bundle.entries if not view.exists(entry.canonical_relpath))
    if missing:
        return _decision(
            view,
            passed=False,
            reason_code="staged_evidence_missing",
            message=f"Missing current attempt evidence: {', '.join(missing)}",
            validator_id=capability,
        )
    return None


def _generic(
    capability: str,
    view: StagingEvidenceView,
    parameters: FrozenModel,
    bundle: ArtifactBundle,
) -> GateDecision:
    if invalid := _validate_expected(capability, view, parameters, bundle):
        return invalid
    refs = tuple(entry.canonical_relpath for entry in bundle.entries)
    return _decision(
        view,
        passed=True,
        reason_code="evidence_valid",
        message="Current attempt evidence exactly satisfies the registered manifest.",
        validator_id=capability,
        evidence_refs=refs,
    )


def _semantic_failure(
    capability: str,
    view: StagingEvidenceView,
    reason_code: str,
    message: str,
) -> GateDecision:
    return _decision(
        view,
        passed=False,
        reason_code=reason_code,
        message=message,
        validator_id=capability,
    )


def _semantic_success(
    capability: str, view: StagingEvidenceView, bundle: ArtifactBundle
) -> GateDecision:
    return _decision(
        view,
        passed=True,
        reason_code="evidence_valid",
        message="Current attempt evidence satisfies the capability's semantic gate.",
        validator_id=capability,
        evidence_refs=tuple(entry.canonical_relpath for entry in bundle.entries),
    )


def _typed_expected(
    capability: str,
    expected_type: type[FrozenModel],
    view: StagingEvidenceView,
    parameters: FrozenModel,
    bundle: ArtifactBundle,
) -> GateDecision | None:
    if not isinstance(parameters, expected_type):
        return _semantic_failure(
            capability,
            view,
            "invalid_validator_parameters",
            f"{capability} requires {expected_type.__name__}.",
        )
    return _validate_expected(capability, view, parameters, bundle)


def _nonempty_bundle(
    capability: str,
    expected_type: type[FrozenModel],
    reason_code: str,
    view: StagingEvidenceView,
    parameters: FrozenModel,
    bundle: ArtifactBundle,
) -> GateDecision:
    if invalid := _typed_expected(
        capability, expected_type, view, parameters, bundle
    ):
        return invalid
    try:
        if any(
            not view.read_bytes(entry.canonical_relpath).strip()
            for entry in bundle.entries
        ):
            raise ValueError("all required evidence must be non-empty")
    except (OSError, PermissionError, UnicodeError, ValueError) as exc:
        return _semantic_failure(capability, view, reason_code, str(exc))
    return _semantic_success(capability, view, bundle)


def _contains_field_pass(text: str, field: str = "result") -> bool:
    pattern = re.compile(rf"(?im)^\s*(?:[#>*`-]+\s*)?{re.escape(field)}\s*:\s*PASS\s*$")
    return pattern.search(text) is not None


def _source_split(
    view: StagingEvidenceView, parameters: FrozenModel, bundle: ArtifactBundle
) -> GateDecision:
    capability = "source.split"
    if invalid := _typed_expected(
        capability, SourceSplitInput, view, parameters, bundle
    ):
        return invalid
    assert isinstance(parameters, SourceSplitInput)
    try:
        toc = json.loads(view.read_text("source/toc.json"))
        if not isinstance(toc, list):
            raise ValueError("source/toc.json must contain an ordered list")
        slugs = tuple(
            item.get("slug") if isinstance(item, dict) else None for item in toc
        )
        sources = tuple(
            item.get("src") if isinstance(item, dict) else None for item in toc
        )
        expected_sources = tuple(
            f"chapters/src/{chapter}.md" for chapter in parameters.expected_chapters
        )
        if slugs != parameters.expected_chapters or sources != expected_sources:
            raise ValueError("TOC chapter identities must exactly equal expected_chapters")
        if any(not view.read_text(path).strip() for path in expected_sources):
            raise ValueError("every expected source chapter must be non-empty")
    except (
        json.JSONDecodeError,
        OSError,
        PermissionError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        return _semantic_failure(capability, view, "source_split_invalid", str(exc))
    return _semantic_success(capability, view, bundle)


def _translation_trial(
    view: StagingEvidenceView, parameters: FrozenModel, bundle: ArtifactBundle
) -> GateDecision:
    capability = "translation.trial"
    result = _nonempty_bundle(
        capability,
        EmptyInput,
        "pretranslation_report_missing",
        view,
        parameters,
        bundle,
    )
    if not result.passed:
        return result
    if not _contains_field_pass(view.read_text("qa/pretranslation/report.md")):
        return _semantic_failure(
            capability,
            view,
            "pretranslation_not_passed",
            "Pretranslation report must conclude result: PASS.",
        )
    return result


def _glossary_prepare(
    view: StagingEvidenceView, parameters: FrozenModel, bundle: ArtifactBundle
) -> GateDecision:
    capability = "glossary.prepare"
    if invalid := _typed_expected(capability, EmptyInput, view, parameters, bundle):
        return invalid
    try:
        rows = tuple(
            row
            for row in view.read_text("glossary/terms.csv").splitlines()
            if row.strip()
        )
        if len(rows) < 2 or not view.read_text("glossary/style_guide.md").strip():
            raise ValueError("Glossary requires a header, a term row, and a style guide.")
    except (OSError, PermissionError, UnicodeError, ValueError) as exc:
        return _semantic_failure(capability, view, "glossary_terms_empty", str(exc))
    return _semantic_success(capability, view, bundle)


def _chapter_review(
    view: StagingEvidenceView, parameters: FrozenModel, bundle: ArtifactBundle
) -> GateDecision:
    capability = "chapter.review"
    if invalid := _typed_expected(
        capability, ReviewBatchInput, view, parameters, bundle
    ):
        return invalid
    assert isinstance(parameters, ReviewBatchInput)
    if not parameters.chapters:
        return _semantic_failure(
            capability, view, "chapter_review_scope_empty", "Explicit chapters are required."
        )
    try:
        if any(
            not view.read_bytes(entry.canonical_relpath).strip()
            for entry in bundle.entries
        ):
            raise ValueError("Every review and final chapter must be non-empty.")
        for chapter in parameters.chapters:
            if not _contains_field_pass(view.read_text(f"qa/gates/{chapter}.gate.md")):
                return _semantic_failure(
                    capability,
                    view,
                    "chapter_gate_not_passed",
                    f"qa/gates/{chapter}.gate.md must conclude result: PASS.",
                )
    except (OSError, PermissionError, UnicodeError, ValueError) as exc:
        return _semantic_failure(capability, view, "chapter_final_missing", str(exc))
    return _semantic_success(capability, view, bundle)


def _preproduction_sample(
    view: StagingEvidenceView, parameters: FrozenModel, bundle: ArtifactBundle
) -> GateDecision:
    capability = "preproduction.sample"
    if invalid := _typed_expected(
        capability, BuildEpubInput, view, parameters, bundle
    ):
        return invalid
    try:
        if not view.read_bytes("preproduction/stage2_sample/sample_book.epub"):
            raise ValueError("Sample EPUB is empty.")
        if not _contains_field_pass(
            view.read_text("preproduction/stage2_sample/sample_review.md"),
            "sample_review_status",
        ):
            raise ValueError("sample_review_status must be PASS.")
    except (OSError, PermissionError, UnicodeError, ValueError) as exc:
        return _semantic_failure(capability, view, "sample_review_not_passed", str(exc))
    return _semantic_success(capability, view, bundle)


def _review_independent(
    view: StagingEvidenceView, parameters: FrozenModel, bundle: ArtifactBundle
) -> GateDecision:
    capability = "review.independent"
    if invalid := _typed_expected(
        capability, ReviewBatchInput, view, parameters, bundle
    ):
        return invalid
    for reviewer in ("agent_a", "agent_b"):
        path = f"reviews/{reviewer}/review.md"
        try:
            passed = _contains_field_pass(view.read_text(path))
        except (OSError, PermissionError, UnicodeError, ValueError):
            passed = False
        if not passed:
            return _semantic_failure(
                capability,
                view,
                "independent_review_not_passed",
                f"{path} must conclude result: PASS.",
            )
    return _semantic_success(capability, view, bundle)


def _source_ingest(
    view: StagingEvidenceView, parameters: FrozenModel, bundle: ArtifactBundle
) -> GateDecision:
    if not isinstance(parameters, SourceIngestInput):
        return _decision(
            view, passed=False, reason_code="invalid_validator_parameters",
            message="source.ingest requires SourceIngestInput.", validator_id="source.ingest"
        )
    if invalid := _validate_expected("source.ingest", view, parameters, bundle):
        return invalid
    try:
        manifest = json.loads(view.read_text("source/source_manifest.json"))
        clean = view.read_text("source/source_text.txt")
    except (UnicodeError, json.JSONDecodeError, OSError, ValueError) as exc:
        return _decision(
            view, passed=False, reason_code="source_evidence_invalid", message=str(exc),
            validator_id="source.ingest"
        )
    if not clean.strip() or not isinstance(manifest, dict) or not manifest.get("sha256"):
        return _decision(
            view, passed=False, reason_code="source_evidence_invalid",
            message="Clean source and a checksum-bearing manifest are required.",
            validator_id="source.ingest"
        )
    return _decision(
        view, passed=True, reason_code="evidence_valid", message="Source evidence is valid.",
        validator_id="source.ingest",
        evidence_refs=("source/source_manifest.json", "source/source_text.txt"),
    )


def _chapter_translate(
    view: StagingEvidenceView, parameters: FrozenModel, bundle: ArtifactBundle
) -> GateDecision:
    if not isinstance(parameters, ChapterBatchInput):
        return _decision(
            view, passed=False, reason_code="invalid_validator_parameters",
            message="chapter.translate requires ChapterBatchInput.", validator_id="chapter.translate"
        )
    if invalid := _validate_expected("chapter.translate", view, parameters, bundle):
        return invalid
    for chapter in parameters.chapters:
        path = f"chapters/translated/{chapter}.md"
        if not view.exists(path) or not view.read_text(path).strip():
            return _decision(
                view, passed=False, reason_code="chapter_translation_missing",
                message=f"Missing non-empty current attempt translation {path}.",
                validator_id="chapter.translate"
            )
    return _decision(
        view, passed=True, reason_code="evidence_valid", message="Translations are present.",
        validator_id="chapter.translate",
        evidence_refs=tuple(f"chapters/translated/{item}.md" for item in parameters.chapters),
    )


def _chapter_control(
    view: StagingEvidenceView, parameters: FrozenModel, bundle: ArtifactBundle
) -> GateDecision:
    result = _generic("chapter.control", view, parameters, bundle)
    if not result.passed or not isinstance(parameters, ChapterBatchInput):
        return result
    for chapter in parameters.chapters:
        controlled_path = f"chapters/controlled/{chapter}.md"
        controlled = view.read_text(controlled_path)
        if not controlled.strip() or not any(char.isalnum() for char in controlled):
            return _decision(
                view,
                passed=False,
                reason_code="chapter_control_revision_invalid",
                message=f"Controlled revision {controlled_path} must be substantive.",
                validator_id="chapter.control",
            )
        text = view.read_text(f"qa/chapter_controls/{chapter}.control.md")
        required_fields = {
            "scope": "FULL_CHAPTER",
            "issues_found": "0",
            "fixes_applied": "0",
            "unresolved_blocking_issues": "0",
            "latest_round_status": "PASS",
            "allow_next_chapter": "true",
            "expert_translation_skill_used": "true",
            "polysemy_unresolved_count": "0",
        }
        report_lines = text.rstrip().splitlines()
        nonempty_indices = tuple(
            index for index, line in enumerate(report_lines) if line.strip()
        )
        trailing_indices = nonempty_indices[-len(required_fields) :]
        trailing_lines = tuple(report_lines[index].strip() for index in trailing_indices)
        expected_lines = tuple(
            f"{field}: {value}" for field, value in required_fields.items()
        )
        block_start = trailing_indices[0] if trailing_indices else len(report_lines)
        separator = max(
            (
                index
                for index, line in enumerate(report_lines[:block_start])
                if not line.strip()
            ),
            default=-1,
        )
        field_prefix = re.compile(
            rf"(?i)^\s*(?:{'|'.join(map(re.escape, required_fields))})\s*:"
        )
        duplicate_in_latest_round = any(
            field_prefix.search(line)
            for line in report_lines[separator + 1 : block_start]
        )
        if duplicate_in_latest_round or tuple(
            line.lower() for line in trailing_lines
        ) != tuple(
            line.lower() for line in expected_lines
        ):
            return _decision(
                view, passed=False, reason_code="chapter_control_not_passed",
                message="Chapter control must record a zero-issue PASS.",
                validator_id="chapter.control"
            )
    return result


def _review_spotcheck(
    view: StagingEvidenceView, parameters: FrozenModel, bundle: ArtifactBundle
) -> GateDecision:
    if not isinstance(parameters, SpotcheckInput):
        return _decision(
            view, passed=False, reason_code="invalid_validator_parameters",
            message="review.spotcheck requires SpotcheckInput.", validator_id="review.spotcheck"
        )
    if invalid := _validate_expected("review.spotcheck", view, parameters, bundle):
        return invalid
    root = f"reviews/random_spotcheck/{parameters.round_id}"
    try:
        report = json.loads(view.read_text(f"{root}/validation_report.json"))
        manifest = json.loads(view.read_text(f"{root}/round_manifest.json"))
        summaries = {
            reviewer: json.loads(
                view.read_text(f"{root}/reviews/{reviewer}_summary.json")
            )
            for reviewer in parameters.reviewers
        }
        sample_indexes = {
            reviewer: json.loads(
                view.read_text(f"{root}/samples/{reviewer}/samples.json")
            )
            for reviewer in parameters.reviewers
        }
    except (
        KeyError,
        PermissionError,
        UnicodeError,
        json.JSONDecodeError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        return _decision(
            view, passed=False, reason_code="spotcheck_not_passed", message=str(exc),
            validator_id="review.spotcheck"
        )

    prior_rounds: list[dict[str, dict[str, Any]]] = []
    visible = set(view.paths())
    round_no = int(parameters.round_id.removeprefix("round_"))
    try:
        for prior_no in range(round_no - 1, 0, -1):
            prior_root = f"reviews/random_spotcheck/round_{prior_no:03d}/reviews"
            paths = {
                reviewer: f"{prior_root}/{reviewer}_summary.json"
                for reviewer in parameters.reviewers
            }
            if any(path not in visible for path in paths.values()):
                break
            prior_rounds.append(
                {
                    reviewer: json.loads(view.read_text(path))
                    for reviewer, path in paths.items()
                }
            )
    except (
        PermissionError,
        UnicodeError,
        json.JSONDecodeError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        return _decision(
            view, passed=False, reason_code="spotcheck_not_passed", message=str(exc),
            validator_id="review.spotcheck"
        )

    from abi.qa.validator import evaluate_spotcheck_summaries

    _, expected_report_bytes = evaluate_spotcheck_summaries(
        round_id=parameters.round_id,
        summaries=summaries,
        prior_rounds=tuple(prior_rounds),
        require_pass=True,
    )
    expected_report = json.loads(expected_report_bytes)
    manifest_valid = (
        isinstance(manifest, dict)
        and manifest.get("round_id") == parameters.round_id
        and manifest.get("reviewers") == list(parameters.reviewers)
        and manifest.get("chapters") == list(parameters.chapters)
        and manifest.get("samples_per_agent") == parameters.samples_per_agent
        and manifest.get("seed") == parameters.seed
    )
    samples_valid = all(
        isinstance(index, list)
        and all(
            isinstance(item, dict) and item.get("chapter") in parameters.chapters
            for item in index
        )
        for index in sample_indexes.values()
    )
    valid = (
        len(parameters.reviewers) >= 2
        and manifest_valid
        and samples_valid
        and isinstance(report, dict)
        and report == expected_report
        and expected_report.get("status") == "PASS"
    )
    return _decision(
        view,
        passed=valid,
        reason_code="evidence_valid" if valid else "spotcheck_not_passed",
        message="Spot-check report is a complete deterministic PASS." if valid else "Spot-check report is not a complete PASS.",
        validator_id="review.spotcheck",
        evidence_refs=(
            f"{root}/validation_report.json",
        ),
    )


def _release_prepare(
    view: StagingEvidenceView, parameters: FrozenModel, bundle: ArtifactBundle
) -> GateDecision:
    if not isinstance(parameters, ReleaseInput):
        return _decision(
            view, passed=False, reason_code="invalid_validator_parameters",
            message="release.prepare requires ReleaseInput.", validator_id="release.prepare"
        )
    if invalid := _validate_expected("release.prepare", view, parameters, bundle):
        return invalid
    try:
        state = json.loads(view.read_text("output/release/release_state.json"))
    except (UnicodeError, json.JSONDecodeError, OSError, ValueError) as exc:
        return _decision(
            view, passed=False, reason_code="release_not_passed", message=str(exc),
            validator_id="release.prepare"
        )
    releases = state.get("releases") if isinstance(state, dict) else None
    matching = (
        [item for item in releases if isinstance(item, dict) and item.get("version") == parameters.version]
        if isinstance(releases, list)
        else []
    )
    valid = (
        isinstance(state, dict)
        and state.get("latest_status") == "PASS"
        and state.get("latest_version") == parameters.version
        and len(matching) == 1
        and matching[0].get("epub") == f"book_{parameters.version}.epub"
        and view.exists(f"output/release/book_{parameters.version}.epub")
    )
    return _decision(
        view,
        passed=valid,
        reason_code="evidence_valid" if valid else "release_not_passed",
        message="Requested release version is present and passed." if valid else "Requested release version is absent or not passed.",
        validator_id="release.prepare",
        evidence_refs=("output/release/release_state.json",),
    )


def _epub_build(
    view: StagingEvidenceView, parameters: FrozenModel, bundle: ArtifactBundle
) -> GateDecision:
    if not isinstance(parameters, BuildEpubInput):
        return _decision(
            view, passed=False, reason_code="invalid_validator_parameters",
            message="epub.build requires BuildEpubInput.", validator_id="epub.build"
        )
    if invalid := _validate_expected("epub.build", view, parameters, bundle):
        return invalid
    try:
        reports = tuple(
            json.loads(view.read_text(path))
            for path in (
                "output/asset_manifest_check.json",
                "output/epubcheck.json",
                "output/publication_lint.json",
            )
        )
        epub = view.read_bytes("output/book.epub")
    except (UnicodeError, json.JSONDecodeError, OSError, ValueError) as exc:
        return _decision(
            view, passed=False, reason_code="epub_gate_failed", message=str(exc),
            validator_id="epub.build"
        )
    valid = bool(epub) and all(
        isinstance(report, dict) and report.get("ok") is True for report in reports
    )
    return _decision(
        view,
        passed=valid,
        reason_code="evidence_valid" if valid else "epub_gate_failed",
        message="EPUB and all deterministic gates passed." if valid else "EPUB or a deterministic gate failed.",
        validator_id="epub.build",
        evidence_refs=tuple(entry.canonical_relpath for entry in bundle.entries),
    )


_PARAMETER_TYPES: Mapping[str, type[FrozenModel]] = {
    "source.split": SourceSplitInput,
    "research.global": ResearchInput,
    "research.book": ResearchInput,
    "translation.trial": EmptyInput,
    "glossary.prepare": EmptyInput,
    "chapter.review": ReviewBatchInput,
    "preproduction.spec": EmptyInput,
    "preproduction.sample": BuildEpubInput,
    "epub.build": BuildEpubInput,
    "review.spotcheck": SpotcheckInput,
    "review.independent": ReviewBatchInput,
    "release.prepare": ReleaseInput,
    "output.finalize": EmptyInput,
    "retrospective.capture": EmptyInput,
}


def _registered_generic(capability: str) -> Validator:
    expected_type = _PARAMETER_TYPES[capability]

    def validate(
        view: StagingEvidenceView, parameters: FrozenModel, bundle: ArtifactBundle
    ) -> GateDecision:
        if not isinstance(parameters, expected_type):
            return _decision(
                view, passed=False, reason_code="invalid_validator_parameters",
                message=f"{capability} requires {expected_type.__name__}.", validator_id=capability
            )
        return _generic(capability, view, parameters, bundle)

    return validate


_VALIDATORS: dict[str, Validator] = {
    capability: _registered_generic(capability) for capability in _PARAMETER_TYPES
}
_VALIDATORS.update(
    {
        "source.ingest": _source_ingest,
        "source.split": _source_split,
        "research.global": lambda view, parameters, bundle: _nonempty_bundle(
            "research.global", ResearchInput, "global_research_missing", view, parameters, bundle
        ),
        "research.book": lambda view, parameters, bundle: _nonempty_bundle(
            "research.book", ResearchInput, "book_research_missing", view, parameters, bundle
        ),
        "translation.trial": _translation_trial,
        "glossary.prepare": _glossary_prepare,
        "chapter.translate": _chapter_translate,
        "chapter.control": _chapter_control,
        "chapter.review": _chapter_review,
        "preproduction.spec": lambda view, parameters, bundle: _nonempty_bundle(
            "preproduction.spec", EmptyInput, "production_spec_missing", view, parameters, bundle
        ),
        "preproduction.sample": _preproduction_sample,
        "epub.build": _epub_build,
        "review.spotcheck": _review_spotcheck,
        "review.independent": _review_independent,
        "release.prepare": _release_prepare,
        "output.finalize": lambda view, parameters, bundle: _nonempty_bundle(
            "output.finalize", EmptyInput, "final_manifest_missing", view, parameters, bundle
        ),
        "retrospective.capture": lambda view, parameters, bundle: _nonempty_bundle(
            "retrospective.capture", EmptyInput, "retrospective_missing", view, parameters, bundle
        ),
    }
)


def validator_catalog() -> dict[str, ActionValidator]:
    return cast(dict[str, ActionValidator], dict(_VALIDATORS))


def validate_evidence(
    capability: str,
    evidence_view: StagingEvidenceView,
    parameters: FrozenModel,
    bundle: ArtifactBundle,
) -> GateDecision:
    validator = _VALIDATORS.get(capability)
    if validator is None:
        return _decision(
            evidence_view,
            passed=False,
            reason_code="validator_not_registered",
            message=f"No validator for {capability}; register one before authorizing this Action.",
            validator_id="unregistered",
        )
    return validator(evidence_view, parameters, bundle)


__all__ = ["validate_evidence", "validator_catalog"]
