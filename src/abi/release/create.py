"""Versioned release / private-artifact creation.

Public/licensed projects release into ``output/release/``; private-use projects
into the git-ignored ``output/private_artifacts/``. Refuses to release unless the
latest random spot-check round validated PASS.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from abi.epub.result import GateResult
from abi.project.layout import BookProject
from abi.types._base import FrozenModel

_FS_ILLEGAL = re.compile(r'[\\/:*?"<>|]+')
_VERSION_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


@dataclass
class _Mode:
    private: bool
    dir_path: Path
    state_name: str
    notes_name: str
    index_name: str
    epub_suffix: str
    producer: str


class _ReleaseRecord(FrozenModel):
    version: str
    epub: str
    created_at: str
    status: str


class _ReleaseState(FrozenModel):
    book: str
    producer: str
    latest_status: str
    latest_version: str
    releases: tuple[_ReleaseRecord, ...]


def _mode_for(project: BookProject) -> _Mode:
    st = project.load_state()
    if st.publication_mode == "private_use":
        return _Mode(
            private=True,
            dir_path=project.private_artifacts_dir,
            state_name="private_artifact_state.json",
            notes_name="private_artifact_notes.md",
            index_name="private_artifact_index.md",
            epub_suffix="_private",
            producer="参考public-domain-books-translation 开源项目 个人自制",
        )
    return _Mode(
        private=False,
        dir_path=project.release_dir,
        state_name="release_state.json",
        notes_name="release_notes.md",
        index_name="release_index.md",
        epub_suffix="",
        producer="LifeBook 书坊 译制",
    )


def _title(project: BookProject) -> str:
    if project.book_yaml.exists():
        try:
            data = yaml.safe_load(project.book_yaml.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict) and data.get("title"):
                return str(data["title"])
        except Exception:
            pass
    return project.root.name


def _safe(name: str) -> str:
    return _FS_ILLEGAL.sub("_", name).strip() or "book"


def _spotcheck_passed(project: BookProject) -> bool:
    rounds = sorted(project.random_spotcheck_dir.glob("round_*"))
    if not rounds:
        return False
    report = rounds[-1] / "validation_report.json"
    if not report.exists():
        return False
    try:
        return str(json.loads(report.read_text(encoding="utf-8")).get("status", "")).upper() == "PASS"
    except Exception:
        return False


def _next_version(state_path: Path, given: str | None) -> str:
    if given:
        return given if given.startswith("v") else f"v{given}"
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            versions = [r["version"] for r in data.get("releases", []) if "version" in r]
            best = (0, 0, 0)
            for v in versions:
                m = _VERSION_RE.search(v)
                if m:
                    best = max(best, tuple(int(x) for x in m.groups()))
            return f"v{best[0]}.{best[1]}.{best[2] + 1}"
        except Exception:
            pass
    return "v0.0.1"


def create_release(project: BookProject, *, version: str | None = None) -> GateResult:
    if not project.book_epub.exists():
        return GateResult(False, "output/book.epub missing — build the EPUB first")
    if not _spotcheck_passed(project):
        return GateResult(
            False,
            "random spot-check has not validated PASS; cannot release. "
            "Run stage 16a until validate_random_spotcheck PASSes.",
        )

    mode = _mode_for(project)
    mode.dir_path.mkdir(parents=True, exist_ok=True)
    state_path = mode.dir_path / mode.state_name
    ver = _next_version(state_path, version)
    title = _safe(_title(project))

    epub_name = f"{title}{mode.epub_suffix}_{ver}.epub"
    epub_dest = mode.dir_path / epub_name
    shutil.copy2(project.book_epub, epub_dest)

    ts = datetime.now(UTC).isoformat(timespec="seconds")

    # release_state.json (cumulative).
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        state = {"book": project.root.name, "producer": mode.producer, "releases": []}
    state["latest_status"] = "PASS"
    state["latest_version"] = ver
    state["producer"] = mode.producer
    state.setdefault("releases", []).append(
        {"version": ver, "epub": epub_name, "created_at": ts, "status": "PASS"}
    )
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    # notes (prepend newest at top).
    notes_path = mode.dir_path / mode.notes_name
    entry = (
        f"## {ver} — {ts}\n\n"
        f"- artifact: `{epub_name}`\n"
        f"- producer: {mode.producer}\n"
        f"- QA evidence: random spot-check PASS; see reviews/random_spotcheck/.\n"
        "- reason / fixes / risks: (fill from this iteration)\n\n"
    )
    existing = notes_path.read_text(encoding="utf-8") if notes_path.exists() else \
        f"# Release notes — {_title(project)}\n\n"
    header, _, body = existing.partition("\n\n")
    notes_path.write_text(f"{header}\n\n{entry}{body}", encoding="utf-8")

    # index.
    index_path = mode.dir_path / mode.index_name
    index_lines = [f"# Release index — {_title(project)}", ""]
    for r in state["releases"]:
        index_lines.append(f"- {r['version']} — `{r['epub']}` ({r['status']}, {r['created_at']})")
    index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    return GateResult(
        True,
        f"released {epub_name} ({'private' if mode.private else 'public'}); "
        f"{mode.state_name}.latest_status=PASS",
        details={"version": ver, "artifact": project.rel(epub_dest),
                 "private": mode.private},
    )


def validate_created_release(project: BookProject, *, version: str) -> GateResult:
    """Bind release evidence to the exact controller-requested version and artifact."""
    normalized = version if version.startswith("v") else f"v{version}"
    for directory, state_name in (
        (project.release_dir, "release_state.json"),
        (project.private_artifacts_dir, "private_artifact_state.json"),
    ):
        state_path = directory / state_name
        if not state_path.exists():
            continue
        try:
            state = _ReleaseState.model_validate_json(state_path.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            continue
        matches = tuple(
            record
            for record in state.releases
            if record.version == normalized
        )
        if (
            state.latest_status != "PASS"
            or state.latest_version != normalized
            or not matches
            or state.releases[-1].version != normalized
            or state.releases[-1].status != "PASS"
        ):
            continue
        artifact_name = state.releases[-1].epub
        if Path(artifact_name).name != artifact_name:
            continue
        artifact = directory / artifact_name
        if artifact.is_file():
            return GateResult(
                True,
                f"release {normalized} is bound to {project.rel(artifact)}",
                details={"version": normalized, "artifact": project.rel(artifact)},
            )
    return GateResult(False, f"requested release {normalized} has no matching PASS artifact")
