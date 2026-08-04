"""Crash-safe promotion of staged artifacts into canonical paths."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from abi.project.artifacts import (
    ArtifactConflictError,
    ArtifactStore,
    InjectedCrash,
    PromotionIntent,
)
from abi.project.layout import BookProject
from abi.project.run_ledger import LedgerConflictError, RunLedger, RunSeed
from abi.types.orchestration import AuthorizedAction, PlanPatch, ProposedAction


@asynccontextmanager
async def prepared_store(
    tmp_path: Path, *, content: str
) -> AsyncIterator[tuple[ArtifactStore, RunLedger, PromotionIntent]]:
    """Build a real project tree and SQLite attempt ready to promote one file."""
    (tmp_path / "state").mkdir()
    async with RunLedger.open(tmp_path / "state/run.db") as ledger:
        run_id = await ledger.create_run(RunSeed(run_id="run-1"))
        await ledger.append_plan(
            run_id,
            PlanPatch(
                objective="translate chapter",
                proposed_actions=(ProposedAction(proposal_id="proposal-1", capability="translate.chapter"),),
                rationale="promote a validated chapter",
            ),
        )
        await ledger.authorize_actions(
            run_id,
            (
                AuthorizedAction(
                    action_id="translate-001",
                    proposal_id="proposal-1",
                    plan_version=1,
                    capability="translate.chapter",
                    parameters_json="{}",
                    idempotency_key="translate-001",
                ),
            ),
        )
        await ledger.start_attempt("translate-001")
        project = BookProject(tmp_path)
        store = ArtifactStore(project, ledger)
        staged_path = store.staging_dir("translate-001", 1) / "001.md"
        staged_path.write_text(content, encoding="utf-8")
        intent = await store.prepare_promotion(
            action_id="translate-001",
            attempt=1,
            staged_relpath="state/staging/translate-001/1/001.md",
            canonical_relpath="chapters/final/001.md",
            media_type="text/markdown",
        )
        yield store, ledger, intent


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_point", ["after_intent", "after_rename"])
async def test_reconcile_completes_interrupted_promotion(
    tmp_path: Path, crash_point: str
) -> None:
    """Catch a crash boundary that otherwise leaves a valid artifact uncommitted."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        with pytest.raises(InjectedCrash):
            await store.promote(staged, crash_after=crash_point)

        if crash_point == "after_intent":
            assert (tmp_path / "state/staging/translate-001/1/001.md").is_file()
        await store.reconcile_all()

        assert (tmp_path / "chapters/final/001.md").read_text(encoding="utf-8") == "translation"
        assert await ledger.promotion_state(staged.intent_id) == "COMMITTED"


@pytest.mark.asyncio
async def test_different_canonical_checksum_creates_conflict(tmp_path: Path) -> None:
    """Catch a promotion that would overwrite a prior canonical artifact."""
    async with prepared_store(tmp_path, content="new") as (store, ledger, staged):
        canonical = tmp_path / "chapters/final/001.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("old", encoding="utf-8")

        with pytest.raises(ArtifactConflictError, match="choose the canonical artifact"):
            await store.promote(staged)

        assert await ledger.has_open_incident("artifact_checksum_conflict")
        assert canonical.read_text(encoding="utf-8") == "old"
        assert (tmp_path / "state/staging/translate-001/1/001.md").read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_matching_canonical_is_an_idempotent_promotion(tmp_path: Path) -> None:
    """Catch retries that reject an already-promoted identical canonical file."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        canonical = tmp_path / "chapters/final/001.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("translation", encoding="utf-8")

        await store.promote(staged)

        assert await ledger.promotion_state(staged.intent_id) == "COMMITTED"
        assert not (tmp_path / "state/staging/translate-001/1/001.md").exists()


@pytest.mark.asyncio
async def test_reconcile_records_missing_artifacts_without_committing(tmp_path: Path) -> None:
    """Catch recovery that commits an intent even though neither artifact survived a crash."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        with pytest.raises(InjectedCrash):
            await store.promote(staged, crash_after="after_intent")
        (tmp_path / "state/staging/translate-001/1/001.md").unlink()

        await store.reconcile_all()

        assert await ledger.promotion_state(staged.intent_id) == "PENDING"
        assert await ledger.has_open_incident("artifact_promotion_missing")


@pytest.mark.asyncio
async def test_reconcile_detects_tampered_committed_canonical(tmp_path: Path) -> None:
    """Catch a reconciler that ignores a canonical artifact changed after commit."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        await store.promote(staged)
        (tmp_path / "chapters/final/001.md").write_text("tampered", encoding="utf-8")

        with pytest.raises(ArtifactConflictError, match="choose the canonical artifact"):
            await store.reconcile_all()

        assert await ledger.has_open_incident("artifact_checksum_conflict")


@pytest.mark.asyncio
async def test_prepare_rejects_a_second_intent_for_one_canonical_path(tmp_path: Path) -> None:
    """Catch racing attempts that would both replace the same absent canonical path."""
    async with prepared_store(tmp_path, content="first") as (store, ledger, staged):
        await ledger.authorize_actions(
            "run-1",
            (
                AuthorizedAction(
                    action_id="translate-002",
                    proposal_id="proposal-1",
                    plan_version=1,
                    capability="translate.chapter",
                    parameters_json="{}",
                    idempotency_key="translate-002",
                ),
            ),
        )
        await ledger.start_attempt("translate-002")
        second_staged = store.staging_dir("translate-002", 1) / "001.md"
        second_staged.write_text("second", encoding="utf-8")

        with pytest.raises(LedgerConflictError, match="choose the canonical artifact"):
            await store.prepare_promotion(
                action_id="translate-002",
                attempt=1,
                staged_relpath="state/staging/translate-002/1/001.md",
                canonical_relpath=staged.canonical_relpath,
                media_type="text/markdown",
            )

        assert await ledger.has_open_incident("artifact_checksum_conflict")


@pytest.mark.asyncio
async def test_prepare_rejects_a_nonstaged_source_path(tmp_path: Path) -> None:
    """Catch a caller that tries to promote a source file outside its attempt staging root."""
    async with prepared_store(tmp_path, content="translation") as (store, _, _):
        source = tmp_path / "source.md"
        source.write_text("source", encoding="utf-8")

        with pytest.raises(ValueError, match="attempt staging directory"):
            await store.prepare_promotion(
                action_id="translate-001",
                attempt=1,
                staged_relpath="source.md",
                canonical_relpath="chapters/final/source.md",
                media_type="text/markdown",
            )


def test_staging_dir_rejects_path_escape(tmp_path: Path) -> None:
    """Catch action identifiers that would write a staging artifact outside its attempt directory."""
    store = ArtifactStore(BookProject(tmp_path), None)

    with pytest.raises(ValueError, match="safe path component"):
        store.staging_dir("../outside", 1)
