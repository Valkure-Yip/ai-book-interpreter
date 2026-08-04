"""Crash-safe promotion of staged artifacts into canonical paths."""

from __future__ import annotations

import asyncio
import os
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
        store.write_staged_bytes(
            action_id="translate-001", attempt=1, relative_path="001.md", content=content.encode()
        )
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
        staged_duplicate = store.write_staged_bytes(
            action_id="translate-001", attempt=1, relative_path="001.md", content=b"different"
        )

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
        store.write_staged_bytes(
            action_id="translate-002", attempt=1, relative_path="001.md", content=b"second"
        )

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
        store.write_staged_bytes(
            action_id="translate-001", attempt=1, relative_path="001.md", content=b"translation"
        )

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
        store.write_staged_bytes(
            action_id="translate-002", attempt=1, relative_path="001.md", content=b"second"
        )

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
                stores[action_id].write_staged_bytes(
                    action_id=action_id, attempt=1, relative_path="001.md", content=content.encode()
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
        store.write_staged_bytes(
            action_id="translate-002", attempt=1, relative_path="002.md", content=b"recovered"
        )
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


@pytest.mark.asyncio
async def test_staged_mutation_after_verification_never_commits_wrong_canonical(tmp_path: Path) -> None:
    """Catch a mutable staged inode being installed after its checksum was verified."""
    async with prepared_store(tmp_path, content="expected") as (_, ledger, staged):
        staged_path = tmp_path / "state/staging/translate-001/1/001.md"

        def mutate_staged(point: str, _: PromotionIntent | None) -> None:
            if point == "after_staged_verification":
                staged_path.write_text("wrong", encoding="utf-8")

        store = ArtifactStore(BookProject(tmp_path), ledger, test_hook=mutate_staged)
        with pytest.raises(ArtifactConflictError, match="choose the canonical artifact"):
            await store.promote(staged)

        assert await ledger.promotion_state(staged.intent_id) == "PENDING"
        canonical = tmp_path / "chapters/final/001.md"
        assert not canonical.exists() or canonical.read_text(encoding="utf-8") == "expected"


@pytest.mark.asyncio
async def test_post_prepare_staging_alias_never_deletes_canonical(tmp_path: Path) -> None:
    """Catch cleanup unlinking canonical after an attempt directory becomes a symlink."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, _):
        canonical = tmp_path / "chapters/final/001.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("translation", encoding="utf-8")
        staged_path = tmp_path / "state/staging/translate-001/1/001.md"
        attempt_dir = staged_path.parent
        staged_path.unlink()
        attempt_dir.rmdir()
        attempt_dir.symlink_to(canonical.parent, target_is_directory=True)

        with pytest.raises(ArtifactConflictError, match=r"staging|repair"):
            await store.reconcile_all()

        assert canonical.read_text(encoding="utf-8") == "translation"
        assert await ledger.has_open_incident("artifact_intent_invalid")


def test_staging_dir_race_hook_cannot_create_outside_project(tmp_path: Path) -> None:
    """Catch a symlink inserted between validation and mkdir creating an external attempt directory."""
    project = BookProject(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    def insert_symlink(point: str, _: PromotionIntent | None) -> None:
        if point == "before_staging_mkdir":
            project.staging_root.mkdir(parents=True, exist_ok=True)
            (project.staging_root / "translate-001").symlink_to(outside, target_is_directory=True)

    store = ArtifactStore(project, None, test_hook=insert_symlink)
    with pytest.raises(ValueError, match="symlink"):
        store.write_staged_bytes(
            action_id="translate-001", attempt=1, relative_path="001.md", content=b"translation"
        )

    assert not (outside / "1").exists()


@pytest.mark.asyncio
async def test_direct_ledger_intent_bypass_never_promotes_source_file(tmp_path: Path) -> None:
    """Catch a ledger-created intent bypassing attempt staging validation during reconciliation."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, _):
        source = tmp_path / "source.md"
        source.write_text("source", encoding="utf-8")
        bypass = await ledger.create_promotion_intent(
            action_id="translate-001",
            attempt=1,
            staged_relpath="source.md",
            canonical_relpath="chapters/final/source.md",
            checksum="41cf6794ba4200b839c53531555decbf73202b1f3cefa1a22190f76f08c1ae47",
            media_type="text/markdown",
        )

        with pytest.raises(ArtifactConflictError, match="repair"):
            await store.reconcile_intent(bypass)

        assert source.read_text(encoding="utf-8") == "source"
        assert not (tmp_path / "chapters/final/source.md").exists()
        assert await ledger.has_open_incident("artifact_intent_invalid")


@pytest.mark.asyncio
async def test_each_conflicting_intent_records_its_own_incident(tmp_path: Path) -> None:
    """Catch incident deduplication that collapses two canonical conflicts from one action."""
    async with prepared_store(tmp_path, content="first") as (store, ledger, _):
        first = tmp_path / "chapters/final/001.md"
        first.parent.mkdir(parents=True)
        first.write_text("old-first", encoding="utf-8")
        store.write_staged_bytes(
            action_id="translate-001", attempt=1, relative_path="002.md", content=b"second"
        )
        await store.prepare_promotion(
            action_id="translate-001",
            attempt=1,
            staged_relpath="state/staging/translate-001/1/002.md",
            canonical_relpath="chapters/final/002.md",
            media_type="text/markdown",
        )
        second_canonical = tmp_path / "chapters/final/002.md"
        second_canonical.write_text("old-second", encoding="utf-8")

        with pytest.raises(ArtifactConflictError):
            await store.reconcile_all()
        with pytest.raises(ArtifactConflictError):
            await store.reconcile_all()

        incidents = [
            incident
            for incident in (await ledger.load_snapshot("run-1")).incidents
            if incident.error_code == "artifact_checksum_conflict"
        ]
        assert len(incidents) == 2
        assert second_canonical.read_text(encoding="utf-8") == "old-second"


@pytest.mark.asyncio
async def test_canonical_directory_remap_after_validation_cannot_escape_project(tmp_path: Path) -> None:
    """Catch canonical parent re-resolution after a validated directory is replaced by an external symlink."""
    async with prepared_store(tmp_path, content="translation") as (_, ledger, staged):
        outside = tmp_path / "outside"
        outside.mkdir()
        chapters = tmp_path / "chapters"
        saved_chapters = tmp_path / "saved_chapters"

        def remap_canonical_parent(point: str, _: PromotionIntent | None) -> None:
            if point == "after_staged_verification":
                chapters.rename(saved_chapters)
                chapters.symlink_to(outside, target_is_directory=True)

        store = ArtifactStore(BookProject(tmp_path), ledger, test_hook=remap_canonical_parent)
        await store.promote(staged)

        assert await ledger.promotion_state(staged.intent_id) == "COMMITTED"
        assert (saved_chapters / "final/001.md").read_text(encoding="utf-8") == "translation"
        assert not (outside / "final/001.md").exists()


@pytest.mark.asyncio
async def test_replaced_temp_inode_cannot_install_wrong_canonical(tmp_path: Path) -> None:
    """Catch a temporary filename replacement between its verification and no-overwrite installation."""
    async with prepared_store(tmp_path, content="expected") as (_, ledger, staged):
        def replace_temp(point: str, _: PromotionIntent | None) -> None:
            if point == "after_temp_verified":
                temp = next((tmp_path / "chapters/final").glob(".abi-promotion-*.tmp"))
                temp.unlink()
                temp.write_text("wrong", encoding="utf-8")

        store = ArtifactStore(BookProject(tmp_path), ledger, test_hook=replace_temp)
        with pytest.raises(ArtifactConflictError, match="temporary file was replaced"):
            await store.promote(staged)

        assert await ledger.promotion_state(staged.intent_id) == "PENDING"
        assert not (tmp_path / "chapters/final/001.md").exists()


def test_display_staging_path_remap_cannot_redirect_safe_writer(tmp_path: Path) -> None:
    """Catch writes through the safe API escaping after a previously displayed path is remapped."""
    project = BookProject(tmp_path)
    store = ArtifactStore(project, None)
    displayed = store.staging_dir("translate-001", 1)
    outside = tmp_path / "outside"
    outside.mkdir()
    project.staging_root.mkdir(parents=True)
    (project.staging_root / "translate-001").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        store.write_staged_bytes(
            action_id="translate-001", attempt=1, relative_path="001.md", content=b"translation"
        )

    assert displayed == project.staging_root / "translate-001/1"
    assert not (outside / "1/001.md").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("special", ["symlink", "fifo"])
async def test_special_staged_file_records_repair_and_reconcile_continues(
    tmp_path: Path, special: str
) -> None:
    """Catch a symlink or FIFO staged target being consumed during recovery."""
    async with prepared_store(tmp_path, content="good") as (store, ledger, _):
        store.write_staged_bytes(
            action_id="translate-001", attempt=1, relative_path="special.md", content=b"special"
        )
        special_intent = await store.prepare_promotion(
            action_id="translate-001",
            attempt=1,
            staged_relpath="state/staging/translate-001/1/special.md",
            canonical_relpath=f"chapters/final/{special}.md",
            media_type="text/markdown",
        )
        special_path = tmp_path / "state/staging/translate-001/1/special.md"
        special_path.unlink()
        if special == "symlink":
            special_path.symlink_to(tmp_path / "source.md")
        else:
            os.mkfifo(special_path)

        with pytest.raises(ArtifactConflictError, match="repair"):
            await store.reconcile_all()

        assert (tmp_path / "chapters/final/001.md").read_text(encoding="utf-8") == "good"
        assert await ledger.has_open_incident("artifact_intent_invalid")
        assert await ledger.promotion_state(special_intent.intent_id) == "PENDING"


@pytest.mark.asyncio
async def test_reconcile_safely_cleans_only_its_crash_temporary_files(tmp_path: Path) -> None:
    """Catch crash recovery leaving owned promotion temporaries or sweeping unrelated files."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        with pytest.raises(InjectedCrash):
            await store.promote(staged, crash_after="after_temp")
        final_dir = tmp_path / "chapters/final"
        owned = list(final_dir.glob(f".abi-promotion-{staged.intent_id}-*.tmp"))
        unrelated = final_dir / ".abi-promotion-unrelated.tmp"
        unrelated.write_text("keep", encoding="utf-8")
        assert len(owned) == 1

        await store.reconcile_all()

        assert await ledger.promotion_state(staged.intent_id) == "COMMITTED"
        assert not list(final_dir.glob(f".abi-promotion-{staged.intent_id}-*.tmp"))
        assert unrelated.read_text(encoding="utf-8") == "keep"


def test_staging_dir_rejects_path_escape(tmp_path: Path) -> None:
    """Catch action identifiers that would write a staging artifact outside its attempt directory."""
    store = ArtifactStore(BookProject(tmp_path), None)

    with pytest.raises(ValueError, match="safe path component"):
        store.staging_dir("../outside", 1)
