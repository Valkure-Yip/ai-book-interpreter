"""Run directory layout + manifest persistence."""

from __future__ import annotations

import os
import secrets
from datetime import datetime
from pathlib import Path

from abi.prompts.loader import DEFAULT_VERSIONS
from abi.types.run import RunConfig, RunManifest


def runs_root(override: Path | None = None) -> Path:
    if override is not None:
        return override
    env = os.environ.get("ABI_RUNS_DIR")
    if env:
        return Path(env)
    return Path.cwd() / "runs"


def new_run_id() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def run_directory(book_id: str, run_id: str, root: Path | None = None) -> Path:
    return runs_root(root) / book_id / run_id


def latest_run_for(book_id: str, root: Path | None = None) -> Path | None:
    base = runs_root(root) / book_id
    if not base.exists():
        return None
    candidates = sorted(p for p in base.iterdir() if p.is_dir())
    return candidates[-1] if candidates else None


def find_run_dir(
    *,
    run_id: str,
    book_id: str | None = None,
    root: Path | None = None,
) -> Path | None:
    """Locate a run directory by run_id.

    If ``book_id`` is given, check the natural path first (fast path).
    Otherwise scan all book_id subdirectories — ``runs/<book_id>/<run_id>`` is
    unambiguous because run_id includes a timestamp + random hex suffix.
    """
    base = runs_root(root)
    if not base.exists():
        return None
    if book_id is not None:
        p = base / book_id / run_id
        if p.exists() and p.is_dir():
            return p
    for book_dir in base.iterdir():
        if not book_dir.is_dir():
            continue
        candidate = book_dir / run_id
        if candidate.exists() and candidate.is_dir():
            return candidate
    return None


def write_manifest(out_dir: Path, *, book_id: str, run_id: str, config: RunConfig,
                    pipeline_versions: dict[str, str] | None = None) -> RunManifest:
    manifest = RunManifest(
        run_id=run_id,
        book_id=book_id,
        created_at=datetime.utcnow(),
        config=config,
        pipeline_versions=pipeline_versions or {
            "ingest": "v1",
            "survey": "v1",
            "translate": "v1",
            "assemble": "v1",
        },
        prompt_versions=dict(DEFAULT_VERSIONS),
        capabilities={"structured_output": "json_mode"},
        git_sha=os.environ.get("ABI_GIT_SHA", ""),
    )
    (out_dir / "manifest.json").write_text(
        manifest.model_dump_json(indent=2), encoding="utf-8"
    )
    return manifest
