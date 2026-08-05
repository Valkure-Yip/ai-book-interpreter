"""Deterministic parameter expansion into exact artifact manifests."""

from __future__ import annotations

from abi.actions.builtins.inputs import (
    ChapterBatchInput,
    ReleaseInput,
    ReviewBatchInput,
    SourceSplitInput,
    SpotcheckInput,
)
from abi.types._base import FrozenModel
from abi.types.orchestration import ExpectedArtifact, ExpectedArtifactManifest


def _artifact(path: str, media_type: str, role: str) -> ExpectedArtifact:
    return ExpectedArtifact(
        canonical_relpath=path, media_type=media_type, evidence_role=role
    )


_FIXED: dict[str, tuple[ExpectedArtifact, ...]] = {
    "source.ingest": (
        _artifact("source/source_manifest.json", "application/json", "source_manifest"),
        _artifact("source/source_text.txt", "text/plain", "source_text"),
    ),
    "research.global": (
        _artifact("qa/benchmark/global_research_ack.md", "text/markdown", "research"),
    ),
    "research.book": (
        _artifact("metadata/book_research.md", "text/markdown", "research"),
        _artifact("metadata/style_profile.md", "text/markdown", "style_profile"),
    ),
    "translation.trial": (
        _artifact("qa/pretranslation/report.md", "text/markdown", "trial_report"),
    ),
    "glossary.prepare": (
        _artifact("glossary/style_guide.md", "text/markdown", "style_guide"),
        _artifact("glossary/terms.csv", "text/csv", "glossary"),
    ),
    "preproduction.spec": (
        _artifact("preproduction/stage1/production_spec.md", "text/markdown", "production_spec"),
    ),
    "preproduction.sample": (
        _artifact("preproduction/stage2_sample/sample_book.epub", "application/epub+zip", "sample_epub"),
        _artifact("preproduction/stage2_sample/sample_review.md", "text/markdown", "sample_review"),
    ),
    "epub.build": (
        _artifact("output/asset_manifest_check.json", "application/json", "asset_gate"),
        _artifact("output/book.epub", "application/epub+zip", "epub"),
        _artifact("output/epubcheck.json", "application/json", "epubcheck"),
        _artifact("output/publication_lint.json", "application/json", "publication_gate"),
    ),
    "output.finalize": (
        _artifact("output/final_manifest.md", "text/markdown", "final_manifest"),
    ),
    "retrospective.capture": (
        _artifact("retrospective/retrospective.md", "text/markdown", "retrospective"),
        _artifact("retrospective/template_update_suggestions.md", "text/markdown", "suggestions"),
    ),
}


def expand_expected_artifacts(
    capability: str, action_id: str, parameters: FrozenModel
) -> ExpectedArtifactManifest:
    """Expand one capability without globs, directories, or implicit sorting at runtime."""
    entries: tuple[ExpectedArtifact, ...]
    if capability == "source.split":
        if not isinstance(parameters, SourceSplitInput):
            raise TypeError("source.split requires SourceSplitInput")
        entries = (
            *(
                _artifact(
                    f"chapters/src/{chapter}.md",
                    "text/markdown",
                    "source_chapter",
                )
                for chapter in parameters.expected_chapters
            ),
            _artifact("source/toc.json", "application/json", "source_toc"),
        )
    elif capability == "chapter.translate":
        if not isinstance(parameters, ChapterBatchInput):
            raise TypeError("chapter.translate requires ChapterBatchInput")
        entries = tuple(
            _artifact(f"chapters/translated/{chapter}.md", "text/markdown", "translation")
            for chapter in sorted(parameters.chapters)
        )
    elif capability == "chapter.control":
        if not isinstance(parameters, ChapterBatchInput):
            raise TypeError("chapter.control requires ChapterBatchInput")
        entries = tuple(
            item
            for chapter in sorted(parameters.chapters)
            for item in (
                _artifact(f"chapters/controlled/{chapter}.md", "text/markdown", "translation"),
                _artifact(f"qa/chapter_controls/{chapter}.control.md", "text/markdown", "control"),
            )
        )
        entries = tuple(sorted(entries, key=lambda item: item.canonical_relpath))
    elif capability == "chapter.review":
        if not isinstance(parameters, ReviewBatchInput):
            raise TypeError("chapter.review requires ReviewBatchInput")
        paths = tuple(
            item
            for chapter in sorted(parameters.chapters)
            for item in (
                (f"chapters/final/{chapter}.md", "translation"),
                (f"qa/fidelity/{chapter}.md", "fidelity_review"),
                (f"qa/gates/{chapter}.gate.md", "chapter_gate"),
                (f"qa/imagery/{chapter}.imagery.md", "imagery_review"),
                (f"qa/readability/{chapter}.md", "readability_review"),
                (f"qa/terminology/{chapter}.md", "terminology_review"),
            )
        )
        paths = tuple(sorted(paths))
        entries = tuple(_artifact(path, "text/markdown", role) for path, role in paths)
    elif capability == "release.prepare":
        if not isinstance(parameters, ReleaseInput):
            raise TypeError("release.prepare requires ReleaseInput")
        version = parameters.version if parameters.version.startswith("v") else f"v{parameters.version}"
        entries = (
            _artifact(
                f"output/release/book_{version}.epub",
                "application/epub+zip",
                "release_epub",
            ),
            _artifact("output/release/release_state.json", "application/json", "release_state"),
        )
    elif capability == "review.spotcheck":
        if not isinstance(parameters, SpotcheckInput):
            raise TypeError("review.spotcheck requires SpotcheckInput")
        root = f"reviews/random_spotcheck/{parameters.round_id}"
        entries = (
            *(
                item
                for reviewer in parameters.reviewers
                for item in (
                    _artifact(
                        f"{root}/reviews/{reviewer}_review.md",
                        "text/markdown",
                        "review_detail",
                    ),
                    _artifact(
                        f"{root}/reviews/{reviewer}_summary.json",
                        "application/json",
                        "review_summary",
                    ),
                )
            ),
            _artifact(
                f"{root}/round_manifest.json",
                "application/json",
                "round_manifest",
            ),
            *(
                item
                for reviewer in parameters.reviewers
                for item in (
                    _artifact(
                        f"{root}/samples/{reviewer}/samples.json",
                        "application/json",
                        "review_samples",
                    ),
                    _artifact(
                        f"{root}/samples/{reviewer}/samples.md",
                        "text/markdown",
                        "review_samples",
                    ),
                )
            ),
            _artifact(
                f"{root}/validation_report.json",
                "application/json",
                "review_gate",
            ),
        )
    elif capability == "review.independent":
        if not isinstance(parameters, ReviewBatchInput):
            raise TypeError("review.independent requires ReviewBatchInput")
        entries = (
            _artifact("reviews/agent_a/review.md", "text/markdown", "independent_review"),
            _artifact("reviews/agent_b/review.md", "text/markdown", "independent_review"),
            _artifact("reviews/revision_route.md", "text/markdown", "revision_route"),
        )
    else:
        try:
            entries = _FIXED[capability]
        except KeyError as exc:
            raise KeyError(
                f"no effect expander for {capability}; register exact artifact effects before authorization"
            ) from exc
    return ExpectedArtifactManifest(action_id=action_id, entries=entries)


__all__ = ["expand_expected_artifacts"]
