"""Scaffold a new book-project directory from in-package templates.

This replaces PDBT's ``books/scripts/create_book_project.py`` + template copy:
ABI ships the reference docs, skills, and metadata stubs *inside the package*
(``abi/assets``), so a single ``abi make-book`` produces a self-contained
project with the full directory contract and an ``INIT`` state file.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from abi.project.layout import CONTRACT_DIRS, BookProject, slugify
from abi.project.state import PipelineState, Status

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


@dataclass(frozen=True)
class ScaffoldRequest:
    target_root: Path  # books/{target} root, or any base dir
    book_slug: str
    source_lang: str
    target_lang: str
    source_target: str
    publication_mode: str = "public_domain"
    profile: str | None = None


def _next_number(base: Path) -> int:
    """Next integer prefix within ``base`` (mirrors PDBT auto-increment)."""
    if not base.exists():
        return 1
    used = []
    for child in base.iterdir():
        if child.is_dir() and "_" in child.name:
            prefix = child.name.split("_", 1)[0]
            if prefix.isdigit():
                used.append(int(prefix))
    return (max(used) + 1) if used else 1


def project_dir_for(req: ScaffoldRequest) -> Path:
    """``{target_root}/{NNNN}_{slug}`` like PDBT ``books/{target}/{n}_{title}_{author}``."""
    base = req.target_root
    number = _next_number(base)
    return base / f"{number:04d}_{slugify(req.book_slug)}"


def _copytree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _copytree(item, target)
        else:
            if not target.exists():
                shutil.copy2(item, target)


def _copy_assets(project: BookProject, req: ScaffoldRequest) -> None:
    """Copy language-neutral references + skills + overlays into the project."""
    _copytree(ASSETS_DIR / "references", project.root / "references")
    _copytree(ASSETS_DIR / "skills", project.root / "skills")

    target_overlay = ASSETS_DIR / "targets" / req.target_lang / "references"
    _copytree(target_overlay, project.root / "references")

    pair_overlay = ASSETS_DIR / "source_target" / req.source_target / "references"
    _copytree(pair_overlay, project.root / "references")

    if req.profile:
        profile_overlay = ASSETS_DIR / "profiles" / req.profile / "references"
        _copytree(profile_overlay, project.root / "references")

    if req.publication_mode == "private_use":
        _copytree(ASSETS_DIR / "modes" / "private_use" / "references",
                  project.root / "references")


def _write_metadata_stubs(project: BookProject, req: ScaffoldRequest) -> None:
    if not project.book_yaml.exists():
        project.book_yaml.write_text(
            "\n".join(
                [
                    "# EPUB metadata. Fill these in during ingest (stage 01).",
                    f"title: {req.book_slug}",
                    "authors: []",
                    f"language: {req.target_lang}",
                    f"source_language: {req.source_lang}",
                    "publisher: LifeBook 书坊 译制",
                    "identifier: ''",
                    "rights: ''",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    stub = ASSETS_DIR / "metadata" / "rights_checklist.template.md"
    if stub.exists() and not project.rights_checklist.exists():
        project.rights_checklist.write_text(
            stub.read_text(encoding="utf-8"), encoding="utf-8"
        )

    if req.publication_mode == "private_use":
        decl = ASSETS_DIR / "modes" / "private_use" / "private_use_declaration.template.md"
        if decl.exists() and not project.private_use_declaration.exists():
            project.private_use_declaration.write_text(
                decl.read_text(encoding="utf-8"), encoding="utf-8"
            )
        # Private projects must never publish their source or artifacts.
        gitignore = project.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "# private-use project: never commit source or artifacts\n"
                "source/\noutput/private_artifacts/\nevents.jsonl\nmetrics.json\n",
                encoding="utf-8",
            )


def scaffold_project(req: ScaffoldRequest, *, root: Path | None = None) -> BookProject:
    """Create the project directory tree + INIT state. Idempotent-ish.

    ``root`` overrides the auto-numbered location (used by tests / explicit
    project paths). Otherwise the path is ``{target_root}/{NNNN}_{slug}``.
    """
    project_root = root or project_dir_for(req)
    project = BookProject(project_root)

    for d in CONTRACT_DIRS:
        (project_root / d).mkdir(parents=True, exist_ok=True)

    _copy_assets(project, req)
    _write_metadata_stubs(project, req)

    if not project.exists():
        state = PipelineState(
            book_slug=req.book_slug,
            source_lang=req.source_lang,
            target_lang=req.target_lang,
            source_target=req.source_target,
            publication_mode=req.publication_mode,
            profile=req.profile,
            status=Status.INIT,
            current_step="00_orchestrator",
        )
        project.save_state(state)
        project.append_log(f"scaffold: created project {project_root}")

    return project
