"""Project scaffolding for the durable dynamic run lifecycle."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from abi.orchestrator.run import make_book, resume
from abi.project import ScaffoldRequest, scaffold_project
from abi.project.run_ledger import (
    LedgerConflictError,
    LedgerNotFoundError,
    RunLedger,
    RunSeed,
)
from abi.types.orchestration import RunResult
from abi.types.run import RunConfig


def _request(root: Path) -> ScaffoldRequest:
    return ScaffoldRequest(
        target_root=root / "zh-Hans",
        book_slug="测试书_作者",
        source_lang="en",
        target_lang="zh-Hans",
        source_target="en-zh-Hans",
    )


def test_scaffold_creates_new_state_contract(tmp_path: Path) -> None:
    """Catch scaffolds that recreate the removed fixed-pipeline state authority."""
    project = scaffold_project(_request(tmp_path))

    assert project.root.name == "0001_测试书_作者"
    assert project.run_db.is_file()
    assert project.staging_root.is_dir()
    assert project.graph_checkpoints.parent.is_dir()
    assert project.action_checkpoints.parent.is_dir()
    assert project.action_checkpoints != project.graph_checkpoints
    assert project.finalized_book_yaml == project.root / "metadata/finalized_book.yaml"
    assert project.retrospective == project.root / "retrospective/retrospective.md"
    assert not (project.root / "state/pipeline_state.json").exists()


def test_private_use_overlay_is_preserved(tmp_path: Path) -> None:
    request = ScaffoldRequest(
        target_root=tmp_path / "private" / "zh-Hans",
        book_slug="b",
        source_lang="en",
        target_lang="zh-Hans",
        source_target="en-zh-Hans",
        publication_mode="private_use",
    )

    project = scaffold_project(request)

    assert (project.root / "references/private_use_policy.md").exists()
    assert project.private_use_declaration.exists()
    assert (project.root / ".gitignore").exists()


@pytest.mark.asyncio
async def test_make_book_and_resume_reuse_one_run_and_observability_identity(
    tmp_path: Path,
) -> None:
    """Catch resume paths that create a second run or rotate trace identity."""
    source = tmp_path / "source.txt"
    source.write_text("source text", encoding="utf-8")
    observed_run_ids: list[str] = []

    class _Driver:
        def __init__(self, context) -> None:  # type: ignore[no-untyped-def]
            self._context = context

        async def run(self, run_id: str) -> RunResult:
            assert run_id == self._context.run.run_id
            self._context.events.event("offline.lifecycle")
            self._context.metrics.flush()
            durable = await self._context.ledger.get_run(run_id)
            return RunResult(run_id=run_id, status=durable.status)

    @asynccontextmanager
    async def service_factory(context):  # type: ignore[no-untyped-def]
        observed_run_ids.append(context.run.run_id)
        yield _Driver(context)

    project, created = await make_book(
        source=str(source),
        source_target="en-zh-hans",
        config=RunConfig(),
        books_root=tmp_path / "books",
        book_slug="stable",
        project_root=tmp_path / "project",
        run_service_factory=service_factory,
    )
    _, resumed = await resume(
        project_root=project.root,
        config=RunConfig(),
        run_service_factory=service_factory,
    )

    assert created.run_id == resumed.run_id
    assert observed_run_ids == [created.run_id, created.run_id]
    async with RunLedger.open(project.run_db) as ledger:
        runs = await ledger.list_runs()
    assert [run.run_id for run in runs] == [created.run_id]
    assert runs[0].book_slug == "stable"
    assert runs[0].source_target == "en-zh-hans"
    assert runs[0].publication_mode == "public_domain"
    event_run_ids = {
        json.loads(line)["run_id"]
        for line in (project.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    }
    metrics_run_id = json.loads((project.root / "metrics.json").read_text(encoding="utf-8"))[
        "run_id"
    ]
    assert event_run_ids == {created.run_id}
    assert metrics_run_id == created.run_id


@pytest.mark.asyncio
async def test_resume_rejects_project_without_exactly_one_business_run(
    tmp_path: Path,
) -> None:
    """Catch resume paths that invent a run when the ledger is empty."""
    project = scaffold_project(_request(tmp_path), root=tmp_path / "empty")

    with pytest.raises(LedgerNotFoundError, match=r"exactly one.*make-book"):
        await resume(project_root=project.root, config=RunConfig())


@pytest.mark.asyncio
async def test_resume_rejects_ambiguous_multiple_business_runs(tmp_path: Path) -> None:
    """Catch resume paths that guess an identity from an invalid multi-run ledger."""
    project = scaffold_project(_request(tmp_path), root=tmp_path / "ambiguous")
    async with RunLedger.open(project.run_db) as ledger:
        await ledger.create_run(RunSeed(run_id="run-a"))
        await ledger.create_run(RunSeed(run_id="run-b"))

    with pytest.raises(LedgerConflictError, match=r"multiple.*repair"):
        await resume(project_root=project.root, config=RunConfig())
