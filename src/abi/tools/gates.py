"""Tool wrappers around the deterministic gate / build / QA / release modules.

These tools call into ``abi.epub`` / ``abi.qa`` / ``abi.release`` (imported
lazily so the belt stays importable regardless of build order) and return a
PASS/FAIL summary the agent must obey before advancing state.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, StructuredTool

from abi.tools.context import ToolContext


def make_gate_tools(ctx: ToolContext) -> list[BaseTool]:
    project = ctx.project

    def publication_lint() -> str:
        """Run the publication-text lint over frontmatter/chapters/final/metadata.

        Writes output/publication_lint.json. Returns PASS or FAIL + hard errors.
        """
        from abi.epub.lint import publication_lint as _lint

        res = _lint(project)
        return res.summary()

    def asset_manifest_check() -> str:
        """Check that every referenced image/style/font asset exists and is in the OPF.

        Writes output/asset_manifest_check.json. Returns PASS or FAIL.
        """
        from abi.epub.assets import asset_manifest_check as _check

        res = _check(project)
        return res.summary()

    def build_sample_epub(chapter_slugs: str = "") -> str:
        """Build a sample-chapter EPUB into preproduction/stage2_sample/.

        chapter_slugs: comma-separated NNN_slug stems; empty = first chapter.
        """
        from abi.epub.build import build_sample_epub as _build

        slugs = [s.strip() for s in chapter_slugs.split(",") if s.strip()]
        res = _build(project, chapter_slugs=slugs or None)
        return res.summary()

    def build_epub() -> str:
        """Build the full EPUB from chapters/final/ into output/book.epub."""
        from abi.epub.build import build_epub as _build

        res = _build(project)
        return res.summary()

    def epubcheck(epub_path: str = "output/book.epub") -> str:
        """Run EPUBCheck on the given EPUB (needs Java). Writes output/epubcheck.json."""
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
        from abi.qa.validator import validate_random_spotcheck as _validate

        res = _validate(project, require_pass=require_pass)
        return res.summary()

    def create_release(version: str = "") -> str:
        """Create a versioned release (or private artifact) from output/book.epub.

        Refuses unless the latest random spot-check validation PASSed.
        """
        from abi.release.create import create_release as _create

        res = _create(project, version=version or None)
        return res.summary()

    return [
        StructuredTool.from_function(publication_lint),
        StructuredTool.from_function(asset_manifest_check),
        StructuredTool.from_function(build_sample_epub),
        StructuredTool.from_function(build_epub),
        StructuredTool.from_function(epubcheck),
        StructuredTool.from_function(select_random_review_passages),
        StructuredTool.from_function(validate_random_spotcheck),
        StructuredTool.from_function(create_release),
    ]
