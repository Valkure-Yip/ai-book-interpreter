"""Tool wrappers around the deterministic gate / build / QA / release modules.

These tools call into ``abi.epub`` / ``abi.qa`` / ``abi.release`` (imported
lazily so the belt stays importable regardless of build order) and return a
PASS/FAIL summary the agent must obey before advancing state.
"""

from __future__ import annotations

from pydantic import Field

from abi.tools.context import ToolContext
from abi.tools.permissions import ActionPathPermissions
from abi.types._base import FrozenModel
from abi.types.tools import ToolBinding


class EmptyInput(FrozenModel):
    """No arguments are accepted by this tool."""


class BuildSampleEpubInput(FrozenModel):
    chapter_slugs: str = Field(
        default="", description="Comma-separated chapter stems; empty selects the first chapter."
    )


class EpubcheckInput(FrozenModel):
    epub_path: str = Field(default="output/book.epub", description="Project-relative EPUB path.")


class SelectRandomReviewInput(FrozenModel):
    agents: int = Field(default=2, ge=1)
    samples_per_agent: int = Field(default=120, ge=1)
    target_confidence: float = Field(default=0.80, ge=0, le=1)


class ValidateRandomSpotcheckInput(FrozenModel):
    require_pass: bool = True


class CreateReleaseInput(FrozenModel):
    version: str = Field(default="", description="Optional explicit release version.")


def make_gate_tools(
    ctx: ToolContext,
    *,
    permissions: ActionPathPermissions | None = None,
) -> list[ToolBinding]:
    project = ctx.project

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

        res = _lint(project)
        return res.summary()

    def asset_manifest_check() -> str:
        """Check that every referenced image/style/font asset exists and is in the OPF.

        Writes output/asset_manifest_check.json. Returns PASS or FAIL.
        """
        require_reads("frontmatter", "chapters/final", "assets")
        require_write("output/asset_manifest_check.json")
        from abi.epub.assets import asset_manifest_check as _check

        res = _check(project)
        return res.summary()

    def build_sample_epub(chapter_slugs: str = "") -> str:
        """Build a sample-chapter EPUB into preproduction/stage2_sample/.

        chapter_slugs: comma-separated NNN_slug stems; empty = first chapter.
        """
        require_reads("chapters/final", "frontmatter", "metadata", "assets")
        require_write("preproduction/stage2_sample/sample_book.epub")
        from abi.epub.build import build_sample_epub as _build

        slugs = [s.strip() for s in chapter_slugs.split(",") if s.strip()]
        res = _build(project, chapter_slugs=slugs or None)
        return res.summary()

    def build_epub() -> str:
        """Build the full EPUB from chapters/final/ into output/book.epub."""
        require_reads("chapters/final", "frontmatter", "metadata", "assets")
        require_write("output/book.epub")
        from abi.epub.build import build_epub as _build

        res = _build(project)
        return res.summary()

    def epubcheck(epub_path: str = "output/book.epub") -> str:
        """Run EPUBCheck on the given EPUB (needs Java). Writes output/epubcheck.json."""
        require_read(epub_path)
        require_write("output/epubcheck.json")
        from abi.epub.epubcheck import run_epubcheck

        res = run_epubcheck(project, ctx.resolve(epub_path))
        return res.summary()

    def select_random_review_passages(
        agents: int = 2, samples_per_agent: int = 120, target_confidence: float = 0.80
    ) -> str:
        """Deterministically select a new stratified random spot-check round.

        Creates reviews/random_spotcheck/round_XXX/ with seed, manifest, strata,
        and per-agent sample lists. Returns the new round directory.
        """
        require_reads("chapters/final")
        require_write("reviews/random_spotcheck")
        from abi.qa.sampler import select_random_review_passages as _select

        res = _select(
            project,
            agents=agents,
            samples_per_agent=samples_per_agent,
            target_confidence=target_confidence,
        )
        return res.summary()

    def validate_random_spotcheck(require_pass: bool = True) -> str:
        """Validate the latest spot-check round against the excellence gate.

        Enforces release_confidence>=0.80, avg>=92, min>=88, no open P0/P1/P2.
        Writes validation_report.json. Returns PASS or FAIL.
        """
        require_reads("reviews/random_spotcheck")
        require_write("reviews/random_spotcheck")
        from abi.qa.validator import validate_random_spotcheck as _validate

        res = _validate(project, require_pass=require_pass)
        return res.summary()

    def create_release(version: str = "") -> str:
        """Create a versioned release (or private artifact) from output/book.epub.

        Refuses unless the latest random spot-check validation PASSed.
        """
        require_reads("output/book.epub", "reviews/random_spotcheck", "metadata")
        if permissions is not None and not (
            permissions.can_write("output/release")
            or permissions.can_write("output/private_artifacts")
        ):
            raise PermissionError(
                "this Action is not allowed to create a release; declare its output directory"
            )
        from abi.release.create import create_release as _create

        res = _create(project, version=version or None)
        return res.summary()

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
            SelectRandomReviewInput,
            select_random_review_passages,
        ),
        ToolBinding(
            "validate_random_spotcheck",
            validate_random_spotcheck.__doc__ or "Validate the latest spot check.",
            ValidateRandomSpotcheckInput,
            validate_random_spotcheck,
        ),
        ToolBinding(
            "create_release",
            create_release.__doc__ or "Create a release.",
            CreateReleaseInput,
            create_release,
        ),
    ]
