"""Book-project directory contract (PDBT ``PIPELINE_SPEC.md`` §4).

A ``BookProject`` is a thin, side-effect-light view over a project root. It
knows the canonical sub-paths every stage reads/writes, so no stage hard-codes
directory names. The agent's filesystem tools are sandboxed to ``root``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Full directory contract — created on scaffold.
CONTRACT_DIRS: tuple[str, ...] = (
    "source",
    "source/tables",
    "metadata",
    "references",
    "skills/translation-quality-defect-families",
    "skills/expert-translation-quality",
    "chapters/src",
    "chapters/translated",
    "chapters/controlled",
    "chapters/final",
    "glossary",
    "frontmatter",
    "assets/figures",
    "assets/images",
    "assets/tables",
    "assets/styles",
    "qa/pretranslation",
    "qa/chapter_controls",
    "qa/fidelity",
    "qa/readability",
    "qa/imagery",
    "qa/terminology",
    "qa/gates",
    "qa/refinement",
    "preproduction/stage1",
    "preproduction/stage2_sample",
    "reviews/agent_a",
    "reviews/agent_b",
    "reviews/random_spotcheck",
    "reviews/scorecards",
    "retrospective",
    "output",
    "output/release",
    "state",
    "goal",
)

_SLUG_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")


def slugify(text: str, *, max_len: int = 48) -> str:
    """Project-folder slug. Keeps CJK + word chars, collapses the rest to ``_``."""
    text = text.strip()
    text = _SLUG_RE.sub("_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:max_len] or "book"


@dataclass(frozen=True)
class BookProject:
    """Canonical paths for one book project rooted at ``root``."""

    root: Path

    # --- source ---
    @property
    def source_raw(self) -> Path:
        return self.root / "source/source_text_raw.txt"

    @property
    def source_clean(self) -> Path:
        return self.root / "source/source_text.txt"

    @property
    def source_manifest(self) -> Path:
        return self.root / "source/source_manifest.json"

    @property
    def toc_json(self) -> Path:
        return self.root / "source/toc.json"

    # --- metadata ---
    @property
    def book_yaml(self) -> Path:
        return self.root / "metadata/book.yaml"

    @property
    def finalized_book_yaml(self) -> Path:
        """Immutable metadata decision produced by preproduction."""
        return self.root / "metadata/finalized_book.yaml"

    @property
    def rights_checklist(self) -> Path:
        return self.root / "metadata/rights_checklist.md"

    @property
    def source_evidence(self) -> Path:
        return self.root / "metadata/source_evidence.md"

    @property
    def private_use_declaration(self) -> Path:
        return self.root / "metadata/private_use_declaration.md"

    @property
    def book_research(self) -> Path:
        return self.root / "metadata/book_specific_translation_research.md"

    @property
    def style_profile(self) -> Path:
        return self.root / "metadata/style_profile.md"

    # --- glossary ---
    @property
    def terms_csv(self) -> Path:
        return self.root / "glossary/terms.csv"

    @property
    def style_guide(self) -> Path:
        return self.root / "glossary/style_guide.md"

    # --- chapters ---
    @property
    def chapters_src(self) -> Path:
        return self.root / "chapters/src"

    @property
    def chapters_translated(self) -> Path:
        return self.root / "chapters/translated"

    @property
    def chapters_controlled(self) -> Path:
        return self.root / "chapters/controlled"

    @property
    def chapters_final(self) -> Path:
        return self.root / "chapters/final"

    def chapter_slugs(self) -> list[str]:
        """``NNN_slug`` stems present under ``chapters/src``."""
        if not self.chapters_src.exists():
            return []
        return sorted(p.stem for p in self.chapters_src.glob("*.md"))

    # --- qa ---
    @property
    def qa(self) -> Path:
        return self.root / "qa"

    def chapter_control(self, slug: str) -> Path:
        return self.root / f"qa/chapter_controls/{slug}.control.md"

    def chapter_gate(self, slug: str) -> Path:
        return self.root / f"qa/gates/{slug}.gate.md"

    @property
    def pretranslation_report(self) -> Path:
        return self.root / "qa/pretranslation/pretranslation_report.md"

    @property
    def pretranslation_style_profile(self) -> Path:
        return self.root / "metadata/pretranslation_style_profile.md"

    # --- preproduction ---
    @property
    def production_spec(self) -> Path:
        return self.root / "preproduction/stage1/production_spec.md"

    @property
    def sample_review(self) -> Path:
        return self.root / "preproduction/stage2_sample/sample_review.md"

    @property
    def sample_epub(self) -> Path:
        return self.root / "preproduction/stage2_sample/sample_book.epub"

    # --- output ---
    @property
    def book_epub(self) -> Path:
        return self.root / "output/book.epub"

    @property
    def release_dir(self) -> Path:
        return self.root / "output/release"

    @property
    def private_artifacts_dir(self) -> Path:
        return self.root / "output/private_artifacts"

    @property
    def publication_lint_report(self) -> Path:
        return self.root / "output/publication_lint.json"

    @property
    def asset_manifest_report(self) -> Path:
        return self.root / "output/asset_manifest_check.json"

    @property
    def epubcheck_log(self) -> Path:
        return self.root / "output/epubcheck.json"

    @property
    def final_manifest(self) -> Path:
        return self.root / "output/final_manifest.md"

    # --- reviews ---
    @property
    def random_spotcheck_dir(self) -> Path:
        return self.root / "reviews/random_spotcheck"

    @property
    def revision_route(self) -> Path:
        return self.root / "reviews/revision_route.md"

    # --- retrospective ---
    @property
    def retrospective(self) -> Path:
        return self.root / "retrospective/retrospective.md"

    # --- state ---
    @property
    def run_db(self) -> Path:
        """SQLite business ledger for this book project."""
        return self.root / "state/run.db"

    @property
    def graph_checkpoints(self) -> Path:
        """Durable Controller-loop checkpoints, separate from business facts."""
        return self.root / "state/graph_checkpoints.sqlite"

    @property
    def action_checkpoints(self) -> Path:
        """Durable agent Action checkpoints, isolated from the Controller graph."""
        return self.root / "state/action_checkpoints.sqlite"

    @property
    def staging_root(self) -> Path:
        """Attempt-isolated workspace for artifacts not yet promoted."""
        return self.root / "state/staging"

    @property
    def status_projection(self) -> Path:
        """Rebuildable, non-authoritative projection of the run ledger."""
        return self.root / "state/status_projection.json"

    # --- helpers ---
    def rel(self, path: Path) -> str:
        """Return ``path`` as a project-relative POSIX string (for manifests)."""
        try:
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def within(self, path: Path) -> bool:
        """True if ``path`` resolves inside the project root (sandbox check)."""
        try:
            path.resolve().relative_to(self.root.resolve())
            return True
        except ValueError:
            return False

    def exists(self) -> bool:
        """Return whether this project has an initialized durable business ledger."""
        return self.run_db.is_file()
