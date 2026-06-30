"""High-level entry points used by the CLI: ``make_book`` and ``resume``."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import httpx

from abi.orchestrator.driver import OrchestrationResult, Orchestrator
from abi.project.layout import BookProject
from abi.project.scaffold import ScaffoldRequest, scaffold_project
from abi.project.state import Status
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.providers.services import build_run_services
from abi.tools.context import ToolContext
from abi.types.run import RunConfig


def split_source_target(source_target: str) -> tuple[str, str]:
    """``"en-zh-Hans"`` -> ``("en", "zh-Hans")`` (split on the first hyphen)."""
    if "-" not in source_target:
        raise ValueError(
            f"invalid source_target {source_target!r}; expected '{{source}}-{{target}}' "
            "like 'en-zh-Hans' or 'ja-es'."
        )
    src, tgt = source_target.split("-", 1)
    return src, tgt


def _place_source(project: BookProject, source: str) -> None:
    """Copy/fetch the source into the project's source/ directory."""
    project.source_raw.parent.mkdir(parents=True, exist_ok=True)
    if source.startswith(("http://", "https://")):
        resp = httpx.get(source, follow_redirects=True, timeout=60)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "epub" in ctype or source.lower().endswith(".epub"):
            (project.root / "source" / "source.epub").write_bytes(resp.content)
        else:
            project.source_raw.write_text(resp.text, encoding="utf-8")
        return
    src_path = Path(source).expanduser().resolve()
    if not src_path.exists():
        raise FileNotFoundError(f"source not found: {source}")
    if src_path.suffix.lower() == ".epub":
        shutil.copy2(src_path, project.root / "source" / src_path.name)
    else:
        project.source_raw.write_text(
            src_path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
        )


def _services_for(project: BookProject, config: RunConfig):
    run_id = uuid.uuid4().hex[:12]
    events = EventLogger(project.root / "events.jsonl", run_id=run_id)
    metrics = MetricsAggregator(
        project.root / "metrics.json", run_id=run_id, book_id=project.root.name
    )
    return build_run_services(config=config, events=events, metrics=metrics)


async def make_book(
    *,
    source: str,
    source_target: str,
    config: RunConfig,
    books_root: Path,
    book_slug: str | None = None,
    publication_mode: str = "public_domain",
    profile: str | None = None,
    project_root: Path | None = None,
    until: Status | None = None,
    max_stage_attempts: int = 3,
) -> tuple[BookProject, OrchestrationResult]:
    """Scaffold a new book project, place the source, and run to ``until``/DONE."""
    source_lang, target_lang = split_source_target(source_target)
    slug = book_slug or Path(source).stem or "book"

    req = ScaffoldRequest(
        target_root=books_root / target_lang,
        book_slug=slug,
        source_lang=source_lang,
        target_lang=target_lang,
        source_target=source_target,
        publication_mode=publication_mode,
        profile=profile,
    )
    project = scaffold_project(req, root=project_root)
    _place_source(project, source)

    services = _services_for(project, config)
    ctx = ToolContext(project=project, services=services, config=config)
    orch = Orchestrator(ctx, max_stage_attempts=max_stage_attempts)
    result = await orch.run(until=until)
    return project, result


async def resume(
    *,
    project_root: Path,
    config: RunConfig,
    until: Status | None = None,
    max_stage_attempts: int = 3,
) -> tuple[BookProject, OrchestrationResult]:
    """Resume an existing project from its persisted state."""
    project = BookProject(Path(project_root).expanduser().resolve())
    if not project.exists():
        raise FileNotFoundError(
            f"no project state at {project.state_path}. Run `abi make-book` first."
        )
    services = _services_for(project, config)
    ctx = ToolContext(project=project, services=services, config=config)
    orch = Orchestrator(ctx, max_stage_attempts=max_stage_attempts)
    result = await orch.run(until=until)
    return project, result
