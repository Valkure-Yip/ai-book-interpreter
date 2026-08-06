"""Tool wrappers around the deterministic gate / build / QA / release modules.

These tools call into ``abi.epub`` / ``abi.qa`` / ``abi.release`` (imported
lazily so the belt stays importable regardless of build order) and return a
PASS/FAIL summary the agent must obey before advancing state.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from abi.actions.builtins.inputs import SpotcheckInput
from abi.epub.result import GateResult
from abi.project.artifacts import AttemptStagingWriter, BufferedAttemptWriter
from abi.tools.context import ToolContext
from abi.tools.permissions import ActionPathPermissions
from abi.types._base import FrozenModel
from abi.types.tools import GateRuntimeMetadata, ToolBinding


class EmptyInput(FrozenModel):
    """No arguments are accepted by this tool."""


class BuildSampleEpubInput(FrozenModel):
    chapter_slugs: str = Field(
        default="", description="Comma-separated chapter stems; empty selects the first chapter."
    )


class EpubcheckInput(FrozenModel):
    epub_path: str = Field(default="output/book.epub", description="Project-relative EPUB path.")


class CreateReleaseInput(FrozenModel):
    version: str = Field(default="", description="Optional explicit release version.")


def make_gate_tools(
    ctx: ToolContext,
    *,
    permissions: ActionPathPermissions | None = None,
    runtime_metadata: GateRuntimeMetadata | None = None,
    writer: AttemptStagingWriter | BufferedAttemptWriter | None = None,
    spotcheck_input: SpotcheckInput | None = None,
) -> list[ToolBinding]:
    project = ctx.project

    def require_writer() -> AttemptStagingWriter | BufferedAttemptWriter:
        if writer is None:
            raise PermissionError(
                "gate output requires an AttemptStagingWriter; dispatch inside an authorized attempt"
            )
        return writer

    def require_read(path: str) -> None:
        if permissions is not None and not permissions.can_read(path):
            raise PermissionError(
                f"this Action is not allowed to read {path!r}; declare it in read_set"
            )

    def require_write(path: str) -> None:
        if permissions is not None and not permissions.can_write(path):
            raise PermissionError(
                f"this Action is not allowed to write {path!r}; declare it in write_set"
            )

    def require_reads(*paths: str) -> None:
        for path in paths:
            require_read(path)

    def publication_lint() -> str:
        """Run the publication-text lint over frontmatter/chapters/final/metadata.

        Writes output/publication_lint.json. Returns PASS or FAIL + hard errors.
        """
        require_reads("frontmatter", "chapters/final", "metadata")
        require_write("output/publication_lint.json")
        from abi.epub.lint import publication_lint as _lint

        res = _lint(project, runtime_metadata=runtime_metadata, write_report=False)
        require_writer().write_text(
            "output/publication_lint.json",
            _gate_result_json(res),
            media_type="application/json",
            evidence_role="publication_gate",
        )
        return res.summary()

    def asset_manifest_check() -> str:
        """Check that every referenced image/style/font asset exists and is in the OPF.

        Writes output/asset_manifest_check.json. Returns PASS or FAIL.
        """
        require_reads("frontmatter", "chapters/final", "assets")
        require_write("output/asset_manifest_check.json")
        from abi.epub.assets import asset_manifest_check as _check

        res = _check(project, write_report=False)
        require_writer().write_text(
            "output/asset_manifest_check.json",
            _gate_result_json(res),
            media_type="application/json",
            evidence_role="asset_gate",
        )
        return res.summary()

    def build_sample_epub(chapter_slugs: str = "") -> str:
        """Build a sample-chapter EPUB into preproduction/stage2_sample/.

        chapter_slugs: comma-separated NNN_slug stems; empty = first chapter.
        """
        require_reads("chapters/final", "frontmatter", "metadata", "assets")
        require_write("preproduction/stage2_sample/sample_book.epub")
        from abi.epub.build import build_sample_epub_bytes as _build

        slugs = [s.strip() for s in chapter_slugs.split(",") if s.strip()]
        res, payload = _build(project, chapter_slugs=slugs or None)
        if res.ok:
            require_writer().write_bytes(
                "preproduction/stage2_sample/sample_book.epub",
                payload,
                media_type="application/epub+zip",
                evidence_role="sample_epub",
            )
        return res.summary()

    def build_epub() -> str:
        """Build the full EPUB from chapters/final/ into output/book.epub."""
        require_reads("chapters/final", "frontmatter", "metadata", "assets")
        require_write("output/book.epub")
        from abi.epub.build import build_epub_bytes as _build

        res, payload = _build(project)
        if res.ok:
            require_writer().write_bytes(
                "output/book.epub",
                payload,
                media_type="application/epub+zip",
                evidence_role="epub",
            )
        return res.summary()

    def epubcheck(epub_path: str = "output/book.epub") -> str:
        """Run EPUBCheck on the given EPUB (needs Java). Writes output/epubcheck.json."""
        require_read(epub_path)
        require_write("output/epubcheck.json")
        from abi.epub.epubcheck import run_epubcheck_readonly

        staged_paths = {entry.canonical_relpath for entry in require_writer().entries}
        source = (
            require_writer().staged_path(epub_path)
            if epub_path in staged_paths
            else ctx.resolve(epub_path)
        )
        res = run_epubcheck_readonly(source)
        return res.summary()

    def select_random_review_passages() -> str:
        """Deterministically select a new stratified random spot-check round.

        Creates reviews/random_spotcheck/round_XXX/ with seed, manifest, strata,
        and per-agent sample lists. Returns the new round directory.
        """
        require_reads("chapters/final")
        require_write("reviews/random_spotcheck")
        if spotcheck_input is None:
            raise PermissionError("spot-check selection requires frozen controller input")
        from abi.qa.sampler import plan_random_review_passages

        result, outputs = plan_random_review_passages(
            project,
            round_id=spotcheck_input.round_id,
            reviewers=spotcheck_input.reviewers,
            chapters=spotcheck_input.chapters,
            samples_per_agent=spotcheck_input.samples_per_agent,
            seed=spotcheck_input.seed,
        )
        if result.ok:
            current_writer = require_writer()
            for path in sorted(outputs):
                media_type = "application/json" if path.endswith(".json") else "text/markdown"
                role = "round_manifest" if path.endswith("round_manifest.json") else "review_samples"
                current_writer.write_bytes(
                    path,
                    outputs[path],
                    media_type=media_type,
                    evidence_role=role,
                )
        return result.summary()

    def validate_random_spotcheck() -> str:
        """Validate the latest spot-check round against the excellence gate.

        Enforces release_confidence>=0.80, avg>=92, min>=88, no open P0/P1/P2.
        Writes validation_report.json. Returns PASS or FAIL.
        """
        require_reads("reviews/random_spotcheck")
        require_write("reviews/random_spotcheck")
        if spotcheck_input is None:
            raise PermissionError("spot-check validation requires frozen controller input")
        current_writer = require_writer()
        root = f"reviews/random_spotcheck/{spotcheck_input.round_id}"
        summaries: dict[str, dict[str, Any]] = {}
        for reviewer in spotcheck_input.reviewers:
            summary_path = f"{root}/reviews/{reviewer}_summary.json"
            try:
                payload = json.loads(current_writer.read_bytes(summary_path))
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError(
                    f"missing or invalid reviewer summary {summary_path}: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise ValueError(f"reviewer summary {summary_path} must be a JSON object")
            summaries[reviewer] = payload

        prior_rounds: list[dict[str, dict[str, Any]]] = []
        round_no = int(spotcheck_input.round_id.removeprefix("round_"))
        for prior_no in range(round_no - 1, 0, -1):
            prior: dict[str, dict[str, Any]] = {}
            for reviewer in spotcheck_input.reviewers:
                prior_path = (
                    project.random_spotcheck_dir
                    / f"round_{prior_no:03d}"
                    / "reviews"
                    / f"{reviewer}_summary.json"
                )
                if not prior_path.is_file() or prior_path.is_symlink():
                    prior = {}
                    break
                try:
                    payload = json.loads(prior_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError):
                    prior = {}
                    break
                if not isinstance(payload, dict):
                    prior = {}
                    break
                prior[reviewer] = payload
            if not prior:
                break
            prior_rounds.append(prior)

        from abi.qa.validator import evaluate_spotcheck_summaries

        result, report = evaluate_spotcheck_summaries(
            round_id=spotcheck_input.round_id,
            summaries=summaries,
            prior_rounds=tuple(prior_rounds),
            require_pass=True,
        )
        current_writer.write_bytes(
            f"{root}/validation_report.json",
            report,
            media_type="application/json",
            evidence_role="review_gate",
        )
        return result.summary()

    def create_release(version: str = "") -> str:
        """Create a versioned release (or private artifact) from output/book.epub.

        Refuses unless the latest random spot-check validation PASSed.
        """
        require_reads("output/book.epub", "reviews/random_spotcheck", "metadata")
        private_mode = (
            runtime_metadata.publication_mode == "private_use"
            if runtime_metadata is not None
            else project.private_use_declaration.exists()
        )
        release_root = "output/private_artifacts" if private_mode else "output/release"
        require_read(release_root)
        require_write(release_root)
        raise PermissionError(
            "release creation is a deterministic built-in and cannot run as an agent tool"
        )

    return [
        ToolBinding(
            "publication_lint",
            publication_lint.__doc__ or "Run publication lint.",
            EmptyInput,
            publication_lint,
        ),
        ToolBinding(
            "asset_manifest_check",
            asset_manifest_check.__doc__ or "Check assets.",
            EmptyInput,
            asset_manifest_check,
        ),
        ToolBinding(
            "build_sample_epub",
            build_sample_epub.__doc__ or "Build a sample EPUB.",
            BuildSampleEpubInput,
            build_sample_epub,
        ),
        ToolBinding("build_epub", build_epub.__doc__ or "Build EPUB.", EmptyInput, build_epub),
        ToolBinding("epubcheck", epubcheck.__doc__ or "Run EPUBCheck.", EpubcheckInput, epubcheck),
        ToolBinding(
            "select_random_review_passages",
            select_random_review_passages.__doc__ or "Select review passages.",
            EmptyInput,
            select_random_review_passages,
        ),
        ToolBinding(
            "validate_random_spotcheck",
            validate_random_spotcheck.__doc__ or "Validate the latest spot check.",
            EmptyInput,
            validate_random_spotcheck,
        ),
        ToolBinding(
            "create_release",
            create_release.__doc__ or "Create a release.",
            CreateReleaseInput,
            create_release,
        ),
    ]


def _gate_result_json(result: GateResult) -> str:
    import json

    return json.dumps(
        {
            "ok": bool(result.ok),
            "message": str(result.message),
            "errors": list(result.hard_errors),
            "warnings": list(result.warnings),
            "details": dict(result.details),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
