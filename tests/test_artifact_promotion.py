"""Crash-safe promotion of staged artifacts into canonical paths."""

from __future__ import annotations

import asyncio
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
from abi.project.run_ledger import LedgerConflictError, LedgerError, RunLedger, RunSeed
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
async def test_reconcile_records_a_missing_committed_canonical(tmp_path: Path) -> None:
    """Catch reconciler code that skips a committed intent whose canonical file disappears."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        await store.promote(staged)
        (tmp_path / "chapters/final/001.md").unlink()

        await store.reconcile_all()

        assert await ledger.promotion_state(staged.intent_id) == "COMMITTED"
        assert await ledger.has_open_incident("artifact_promotion_missing")


@pytest.mark.asyncio
async def test_reconcile_preserves_a_differing_staged_duplicate_after_commit(tmp_path: Path) -> None:
    """Catch cleanup that deletes a staged artifact differing from a committed canonical file."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        await store.promote(staged)
        staged_duplicate = store.staging_dir("translate-001", 1) / "001.md"
        staged_duplicate.write_text("different", encoding="utf-8")

        with pytest.raises(ArtifactConflictError, match="choose the canonical artifact"):
            await store.reconcile_all()

        assert staged_duplicate.read_text(encoding="utf-8") == "different"
        assert await ledger.has_open_incident("artifact_checksum_conflict")


@pytest.mark.asyncio
async def test_corrupted_promotion_status_requires_ledger_repair(tmp_path: Path) -> None:
    """Catch silently accepting a persisted promotion state outside the durable enum."""
    async with prepared_store(tmp_path, content="translation") as (_, ledger, staged):
        await ledger._db.execute(
            "UPDATE promotion_intents SET status = 'BROKEN' WHERE intent_id = ?", (staged.intent_id,)
        )
        await ledger._db.commit()

        with pytest.raises(LedgerError, match="repair the ledger"):
            await ledger.get_promotion_intent(staged.intent_id)


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


def test_staging_dir_rejects_symlinked_action_ancestor(tmp_path: Path) -> None:
    """Catch an action staging directory that traverses an external directory symlink."""
    project = BookProject(tmp_path)
    project.staging_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project.staging_root / "translate-001").symlink_to(outside, target_is_directory=True)
    store = ArtifactStore(project, None)

    with pytest.raises(ValueError, match="symlink"):
        store.staging_dir("translate-001", 1)

    assert not (outside / "1").exists()


@pytest.mark.asyncio
async def test_canonical_aliases_share_one_reservation_key(tmp_path: Path) -> None:
    """Catch textual canonical aliases that reserve the same file twice."""
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
        alias_staged = store.staging_dir("translate-002", 1) / "001.md"
        alias_staged.write_text("second", encoding="utf-8")

        with pytest.raises(LedgerConflictError, match="choose the canonical artifact"):
            await store.prepare_promotion(
                action_id="translate-002",
                attempt=1,
                staged_relpath="state/staging/translate-002/1/001.md",
                canonical_relpath="chapters/final/./001.md",
                media_type="text/markdown",
            )

        assert staged.canonical_relpath == "chapters/final/001.md"


@pytest.mark.asyncio
async def test_prepare_rejects_canonical_that_aliases_staging_source(tmp_path: Path) -> None:
    """Catch a canonical destination that aliases the staged source and would be cleaned up."""
    async with prepared_store(tmp_path, content="translation") as (store, _, _):
        with pytest.raises(ValueError, match="outside the staging root"):
            await store.prepare_promotion(
                action_id="translate-001",
                attempt=1,
                staged_relpath="state/staging/translate-001/1/001.md",
                canonical_relpath="state/staging/translate-001/1/./001.md",
                media_type="text/markdown",
            )


@pytest.mark.asyncio
async def test_two_ledger_connections_do_not_overwrite_competing_promotions(tmp_path: Path) -> None:
    """Catch two SQLite connections that race to overwrite one canonical file."""
    (tmp_path / "state").mkdir()
    project = BookProject(tmp_path)
    async with RunLedger.open(project.run_db) as first_ledger:
        run_id = await first_ledger.create_run(RunSeed(run_id="run-1"))
        await first_ledger.append_plan(
            run_id,
            PlanPatch(
                objective="translate chapters",
                proposed_actions=(ProposedAction(proposal_id="proposal-1", capability="translate.chapter"),),
                rationale="exercise independent ledger connections",
            ),
        )
        actions = tuple(
            AuthorizedAction(
                action_id=action_id,
                proposal_id="proposal-1",
                plan_version=1,
                capability="translate.chapter",
                parameters_json="{}",
                idempotency_key=action_id,
            )
            for action_id in ("translate-001", "translate-002")
        )
        await first_ledger.authorize_actions(run_id, actions)
        await first_ledger.start_attempt("translate-001")
        await first_ledger.start_attempt("translate-002")
        async with RunLedger.open(project.run_db) as second_ledger:
            stores = {
                "translate-001": ArtifactStore(project, first_ledger),
                "translate-002": ArtifactStore(project, second_ledger),
            }
            for action_id, content in (("translate-001", "first"), ("translate-002", "second")):
                (stores[action_id].staging_dir(action_id, 1) / "001.md").write_text(
                    content, encoding="utf-8"
                )
            results = await asyncio.gather(
                stores["translate-001"].prepare_promotion(
                    action_id="translate-001",
                    attempt=1,
                    staged_relpath="state/staging/translate-001/1/001.md",
                    canonical_relpath="chapters/final/001.md",
                    media_type="text/markdown",
                ),
                stores["translate-002"].prepare_promotion(
                    action_id="translate-002",
                    attempt=1,
                    staged_relpath="state/staging/translate-002/1/001.md",
                    canonical_relpath="chapters/final/./001.md",
                    media_type="text/markdown",
                ),
                return_exceptions=True,
            )
            winner = next(result for result in results if isinstance(result, PromotionIntent))
            assert any(isinstance(result, LedgerConflictError) for result in results)

            await stores[winner.action_id].promote(winner)

            assert (tmp_path / "chapters/final/001.md").read_text(encoding="utf-8") == (
                "first" if winner.action_id == "translate-001" else "second"
            )


@pytest.mark.asyncio
async def test_reconcile_all_continues_after_a_conflict(tmp_path: Path) -> None:
    """Catch reconciliation that abandons a later recoverable intent after one conflict."""
    async with prepared_store(tmp_path, content="conflicting") as (store, ledger, _):
        conflict = tmp_path / "chapters/final/001.md"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("canonical", encoding="utf-8")
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
        recovered = store.staging_dir("translate-002", 1) / "002.md"
        recovered.write_text("recovered", encoding="utf-8")
        second = await store.prepare_promotion(
            action_id="translate-002",
            attempt=1,
            staged_relpath="state/staging/translate-002/1/002.md",
            canonical_relpath="chapters/final/002.md",
            media_type="text/markdown",
        )

        with pytest.raises(ArtifactConflictError, match="promotion conflict"):
            await store.reconcile_all()

        assert await ledger.promotion_state(second.intent_id) == "COMMITTED"
        assert (tmp_path / "chapters/final/002.md").read_text(encoding="utf-8") == "recovered"


def test_staging_dir_rejects_path_escape(tmp_path: Path) -> None:
    """Catch action identifiers that would write a staging artifact outside its attempt directory."""
    store = ArtifactStore(BookProject(tmp_path), None)

    with pytest.raises(ValueError, match="safe path component"):
        store.staging_dir("../outside", 1)
