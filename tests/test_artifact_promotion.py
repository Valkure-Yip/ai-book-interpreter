"""Crash-safe promotion of staged artifacts into canonical paths."""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import abi.project.artifacts as artifact_module
from abi.actions.evidence import StagingEvidenceView
from abi.project.artifacts import (
    ArtifactConflictError,
    ArtifactStore,
    InjectedCrash,
    PromotionIntent,
    sha256_file,
)
from abi.project.layout import BookProject
from abi.project.run_ledger import LedgerConflictError, LedgerError, RunLedger, RunSeed
from abi.types.orchestration import (
    ArtifactBundle,
    ArtifactBundleEntry,
    AttemptOutcomeReceiptPayload,
    AuthorizedAction,
    ExpectedArtifact,
    ExpectedArtifactManifest,
    GateArtifactIdentity,
    GateDecision,
    GateReceiptPayload,
    PlanPatch,
    ProposedAction,
    RetryPolicySpec,
    Succeeded,
    canonical_bundle_json,
    canonical_manifest_json,
    canonical_model_json,
    sha256_canonical_json,
)


def _authorized_action(
    action_id: str, *, canonical_relpath: str = "chapters/final/001.md"
) -> AuthorizedAction:
    manifest = ExpectedArtifactManifest(
        action_id=action_id,
        entries=(
            ExpectedArtifact(
                canonical_relpath=canonical_relpath,
                media_type="text/markdown",
                evidence_role="translation",
            ),
        ),
    )
    retry = RetryPolicySpec(max_attempts=1)
    return AuthorizedAction(
        action_id=action_id,
        proposal_id="proposal-1",
        plan_version=1,
        capability="translate.chapter",
        parameters_json="{}",
        write_set=("chapters/final",),
        idempotency_key=action_id,
        expected_artifact_manifest=manifest,
        expected_artifact_manifest_digest=sha256_canonical_json(
            canonical_manifest_json(manifest)
        ),
        expected_evidence_refs=("translation",),
        retry_policy=retry,
        retry_policy_fingerprint=sha256_canonical_json(canonical_model_json(retry)),
    )


def _fixture_staged_path(root: Path) -> Path:
    return root / "state/staging/translate-001/1/chapters/final/001.md"


async def _stage_one_artifact(
    project: BookProject,
    store: ArtifactStore,
    ledger: RunLedger,
    *,
    action_id: str,
    canonical_relpath: str,
    content: bytes,
    media_type: str = "text/markdown",
    evidence_role: str = "translation",
) -> PromotionIntent:
    await ledger.authorize_actions(
        "run-1",
        (_authorized_action(action_id, canonical_relpath=canonical_relpath),),
    )
    await ledger.start_attempt(action_id)
    writer = store.writer(action_id, 1)
    writer.write_bytes(
        canonical_relpath,
        content,
        media_type=media_type,
        evidence_role=evidence_role,
    )
    return await _persist_bundle_protocol(project, store, ledger, writer.artifact_bundle())


async def _persist_bundle_protocol(
    project: BookProject,
    store: ArtifactStore,
    ledger: RunLedger,
    bundle: ArtifactBundle,
) -> PromotionIntent:
    outcome = Succeeded(artifact_bundle=bundle, evidence_refs=("translation",))
    outcome_json = canonical_model_json(outcome)
    bundle_json = canonical_bundle_json(bundle)
    await ledger.record_attempt_outcome(
        AttemptOutcomeReceiptPayload(
            action_id=bundle.action_id,
            attempt=bundle.attempt,
            canonical_outcome_json=outcome_json,
            outcome_digest=sha256_canonical_json(outcome_json),
            canonical_bundle_json=bundle_json,
            bundle_digest=sha256_canonical_json(bundle_json),
            evidence_refs=("translation",),
        )
    )
    view = StagingEvidenceView.for_bundle(project, (), bundle)
    decision = GateDecision(
        passed=True,
        reason_code="evidence_valid",
        message="valid",
        validator_id="translate.chapter",
        validator_version="1",
        bundle_digest=view.bundle_digest,
        artifact_checksums=view.artifact_checksums,
        evidence_refs=("translation",),
    )
    gate_json = canonical_model_json(decision)
    _, intents = await ledger.create_gate_receipt_and_bundle_intents(
        GateReceiptPayload(
            action_id=bundle.action_id,
            attempt=bundle.attempt,
            validator_id="translate.chapter",
            validator_version="1",
            canonical_gate_decision_json=gate_json,
            gate_decision_digest=sha256_canonical_json(gate_json),
            bundle_digest=view.bundle_digest,
            artifacts=tuple(
                GateArtifactIdentity(
                    staged_relpath=entry.staged_relpath,
                    canonical_relpath=entry.canonical_relpath,
                    checksum=checksum,
                )
                for entry, checksum in zip(
                    bundle.entries, view.artifact_checksums, strict=True
                )
            ),
            evidence_refs=("translation",),
        )
    )
    return intents[0]


@asynccontextmanager
async def prepared_store(
    tmp_path: Path, *, content: str
) -> AsyncIterator[tuple[ArtifactStore, RunLedger, PromotionIntent]]:
    """Build a real project tree and SQLite attempt ready to promote one file."""
    tmp_path.mkdir(parents=True, exist_ok=True)
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
                _authorized_action("translate-001"),
            ),
        )
        await ledger.start_attempt("translate-001")
        project = BookProject(tmp_path)
        store = ArtifactStore(project, ledger)
        writer = store.writer("translate-001", 1)
        writer.write_bytes(
            "chapters/final/001.md",
            content.encode(),
            media_type="text/markdown",
            evidence_role="translation",
        )
        intent = await _persist_bundle_protocol(project, store, ledger, writer.artifact_bundle())
        yield store, ledger, intent


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_point", ["after_intent", "after_canonical_write"])
async def test_reconcile_completes_interrupted_promotion(
    tmp_path: Path, crash_point: str
) -> None:
    """Catch a crash boundary that otherwise leaves a valid artifact uncommitted."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        with pytest.raises(InjectedCrash):
            await store.promote(staged, crash_after=crash_point)

        if crash_point == "after_intent":
            assert _fixture_staged_path(tmp_path).is_file()
        await store.reconcile_all()

        assert (tmp_path / "chapters/final/001.md").read_text(encoding="utf-8") == "translation"
        assert await ledger.promotion_state(staged.intent_id) == "COMMITTED"
        assert (tmp_path / "state/staging/translate-001/1/chapters/final/001.md").read_text(
            encoding="utf-8"
        ) == "translation"


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
        assert _fixture_staged_path(tmp_path).read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_matching_canonical_is_an_idempotent_promotion(tmp_path: Path) -> None:
    """Catch retries that reject an already-promoted identical canonical file."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        canonical = tmp_path / "chapters/final/001.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("translation", encoding="utf-8")

        await store.promote(staged)

        assert await ledger.promotion_state(staged.intent_id) == "COMMITTED"
        assert (tmp_path / "state/staging/translate-001/1/chapters/final/001.md").read_text(
            encoding="utf-8"
        ) == "translation"


@pytest.mark.asyncio
async def test_reconcile_records_missing_artifacts_without_committing(tmp_path: Path) -> None:
    """Catch recovery that commits an intent even though neither artifact survived a crash."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        with pytest.raises(InjectedCrash):
            await store.promote(staged, crash_after="after_intent")
        _fixture_staged_path(tmp_path).unlink()

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
    """Catch recovery recreating a committed artifact after its canonical name drifts missing."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        await store.promote(staged)
        (tmp_path / "chapters/final/001.md").unlink()

        for _ in range(2):
            with pytest.raises(ArtifactConflictError, match="promotion conflict"):
                await store.reconcile_all()

        assert await ledger.promotion_state(staged.intent_id) == "CONFLICT"
        assert await ledger.has_open_incident("artifact_promotion_missing")
        assert (tmp_path / "state/staging/translate-001/1/chapters/final/001.md").read_text(
            encoding="utf-8"
        ) == "translation"


@pytest.mark.asyncio
@pytest.mark.parametrize("staged_change", ["modified", "deleted"])
async def test_committed_reconcile_ignores_non_authoritative_staged_residue_after_restart(
    tmp_path: Path, staged_change: str
) -> None:
    """Catch COMMITTED recovery treating disposable staging residue as authoritative evidence."""
    project = BookProject(tmp_path)
    staged_path = _fixture_staged_path(tmp_path)
    canonical = tmp_path / "chapters/final/001.md"

    async with prepared_store(tmp_path, content="translation") as (store, ledger, intent):
        await store.promote(intent)
        if staged_change == "modified":
            staged_path.write_text("non-authoritative residue", encoding="utf-8")
        else:
            staged_path.unlink()

        reconciled = await store.reconcile_all()

        assert tuple(item.status for item in reconciled) == ("COMMITTED",)
        assert await ledger.promotion_state(intent.intent_id) == "COMMITTED"
        assert (await ledger.load_snapshot("run-1")).incidents == ()
        assert canonical.read_text(encoding="utf-8") == "translation"
        intent_id = intent.intent_id
        store.close()

    async with RunLedger.open(project.run_db) as restarted_ledger:
        restarted_store = ArtifactStore(project, restarted_ledger)
        try:
            restarted = await restarted_store.reconcile_all()
            assert tuple(item.status for item in restarted) == ("COMMITTED",)
            assert await restarted_ledger.promotion_state(intent_id) == "COMMITTED"
            assert (await restarted_ledger.load_snapshot("run-1")).incidents == ()
            assert canonical.read_text(encoding="utf-8") == "translation"
        finally:
            restarted_store.close()


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
        with pytest.raises(LedgerConflictError, match="choose the canonical artifact"):
            await _stage_one_artifact(
                BookProject(tmp_path),
                store,
                ledger,
                action_id="translate-002",
                canonical_relpath=staged.canonical_relpath,
                content=b"second",
            )

        assert await ledger.has_open_incident("artifact_checksum_conflict")


@pytest.mark.asyncio
async def test_prepare_rejects_a_nonstaged_source_path(tmp_path: Path) -> None:
    """Catch a caller that tries to promote a source file outside its attempt staging root."""
    async with prepared_store(tmp_path, content="translation"):
        source = tmp_path / "source.md"
        source.write_text("source", encoding="utf-8")

        with pytest.raises(ValueError, match="exact action/attempt namespace"):
            ArtifactBundle(
                action_id="translate-001",
                attempt=1,
                entries=(
                    ArtifactBundleEntry(
                        staged_relpath="source.md",
                        canonical_relpath="chapters/final/source.md",
                        media_type="text/markdown",
                        evidence_role="translation",
                    ),
                ),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "canonical_relpath",
    ["STATE/STAGING/escape.md", "CHAPTERS/FINAL/001.md"],
)
async def test_prepare_rejects_uppercase_canonical_before_intent_or_filesystem_mutation(
    tmp_path: Path, canonical_relpath: str
) -> None:
    """Catch platform-dependent uppercase aliases reaching the ledger or canonical tree."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, _):
        before = await ledger.promotion_intents()
        candidate = tmp_path / canonical_relpath

        with pytest.raises(ValueError):
            store.writer("translate-invalid", 1).write_bytes(
                canonical_relpath,
                b"invalid",
                media_type="text/markdown",
                evidence_role="translation",
            )

        assert await ledger.promotion_intents() == before
        assert not candidate.exists()


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


def test_safe_staged_writer_never_modifies_an_existing_hardlinked_inode(tmp_path: Path) -> None:
    """Catch a no-follow writer truncating an existing inode through a staged hardlink."""
    project = BookProject(tmp_path)
    store = ArtifactStore(project, None)
    store.write_staged_bytes(
        action_id="translate-001", attempt=1, relative_path="seed.md", content=b"seed"
    )
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"must survive")
    staged = tmp_path / "state/staging/translate-001/1/001.md"
    os.link(outside, staged)

    with pytest.raises(FileExistsError):
        store.write_staged_bytes(
            action_id="translate-001", attempt=1, relative_path="001.md", content=b"replacement"
        )

    assert outside.read_bytes() == b"must survive"
    assert staged.read_bytes() == b"must survive"


def test_sha256_file_rejects_a_symlink(tmp_path: Path) -> None:
    """Catch the public checksum helper following a caller-controlled symlink."""
    target = tmp_path / "target.bin"
    target.write_bytes(b"artifact")
    alias = tmp_path / "alias.bin"
    alias.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        sha256_file(alias)


def test_sha256_file_rejects_a_fifo_without_blocking_or_leaking_fd(tmp_path: Path) -> None:
    """Catch the public checksum helper blocking on or leaking a special-file descriptor."""
    fifo = tmp_path / "artifact.fifo"
    os.mkfifo(fifo)
    outcome: list[object] = []
    baseline = len(os.listdir("/dev/fd"))

    def checksum_fifo() -> None:
        try:
            outcome.append(sha256_file(fifo))
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=checksum_fifo, daemon=True)
    worker.start()
    worker.join(timeout=0.5)
    blocked = worker.is_alive()
    if blocked:
        unblock_fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        os.close(unblock_fd)
        worker.join(timeout=1)

    assert not blocked, "sha256_file blocked while opening a FIFO"
    assert len(outcome) == 1
    assert isinstance(outcome[0], ValueError)
    assert "regular file" in str(outcome[0])
    assert len(os.listdir("/dev/fd")) <= baseline


def test_store_fails_closed_without_nonblocking_open_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch internal staged/canonical opens silently losing their nonblocking guarantee."""
    monkeypatch.setattr(artifact_module, "_O_NONBLOCK", 0)

    with pytest.raises(RuntimeError, match="O_NONBLOCK"):
        ArtifactStore(BookProject(tmp_path), None)


def test_store_rejects_a_symlink_as_the_durable_project_root(tmp_path: Path) -> None:
    """Catch crash recovery accepting a project root that now redirects through a symlink."""
    real_root = tmp_path / "real-project"
    real_root.mkdir()
    alias = tmp_path / "project"
    alias.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ValueError, match=r"trusted project root|symlink"):
        ArtifactStore(BookProject(alias), None)


def test_store_lifecycle_root_remap_fails_closed_before_staged_write(tmp_path: Path) -> None:
    """Catch later root resolution redirecting a staged write into an external directory."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    saved_root = tmp_path / "saved-project"
    store = ArtifactStore(BookProject(project_root), None)
    project_root.rename(saved_root)
    project_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match=r"project root|repair"):
        store.write_staged_bytes(
            action_id="translate-001", attempt=1, relative_path="001.md", content=b"translation"
        )

    assert not (outside / "state/staging/translate-001/1/001.md").exists()
    assert not (saved_root / "state/staging/translate-001/1/001.md").exists()


@pytest.mark.asyncio
async def test_canonical_dot_alias_is_rejected_before_reservation(tmp_path: Path) -> None:
    """Catch dot components being normalized into a reserved canonical key."""
    async with prepared_store(tmp_path, content="first") as (store, ledger, staged):
        before = await ledger.promotion_intents()
        with pytest.raises(ValueError):
            store.writer("translate-002", 1).write_bytes(
                "chapters/final/./001.md",
                b"second",
                media_type="text/markdown",
                evidence_role="translation",
            )

        assert await ledger.promotion_intents() == before
        assert staged.canonical_relpath == "chapters/final/001.md"


@pytest.mark.asyncio
async def test_prepare_rejects_canonical_that_aliases_staging_source(tmp_path: Path) -> None:
    """Catch a canonical destination that aliases the staged source and would be cleaned up."""
    async with prepared_store(tmp_path, content="translation") as (store, _, _):
        with pytest.raises(ValueError, match="state/staging namespace"):
            store.writer("translate-invalid", 1).write_bytes(
                "state/staging/translate-001/1/001.md",
                b"invalid",
                media_type="text/markdown",
                evidence_role="translation",
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
            _authorized_action(action_id)
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
            bundles: dict[str, ArtifactBundle] = {}
            for action_id, content in (("translate-001", "first"), ("translate-002", "second")):
                writer = stores[action_id].writer(action_id, 1)
                writer.write_bytes(
                    "chapters/final/001.md",
                    content.encode(),
                    media_type="text/markdown",
                    evidence_role="translation",
                )
                bundles[action_id] = writer.artifact_bundle()
            results = await asyncio.gather(
                _persist_bundle_protocol(
                    project,
                    stores["translate-001"],
                    first_ledger,
                    bundles["translate-001"],
                ),
                _persist_bundle_protocol(
                    project,
                    stores["translate-002"],
                    second_ledger,
                    bundles["translate-002"],
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
    async with prepared_store(tmp_path, content="conflicting") as (store, ledger, first):
        second = await _stage_one_artifact(
            BookProject(tmp_path),
            store,
            ledger,
            action_id="translate-002",
            canonical_relpath="chapters/final/002.md",
            content=b"recovered",
        )
        await ledger.conflict_promotion_intent(
            first.intent_id,
            error_code="artifact_checksum_conflict",
            message="canonical ownership is disputed; inspect and retain both artifacts",
        )

        for _ in range(2):
            with pytest.raises(ArtifactConflictError, match="promotion conflict"):
                await store.reconcile_all()

        assert await ledger.promotion_state(first.intent_id) == "CONFLICT"
        assert await ledger.promotion_state(second.intent_id) == "COMMITTED"
        assert not (tmp_path / "chapters/final/001.md").exists()
        assert (tmp_path / "chapters/final/002.md").read_text(encoding="utf-8") == "recovered"
        first_incidents = [
            incident
            for incident in (await ledger.load_snapshot("run-1")).incidents
            if incident.action_id == first.action_id
        ]
        assert [incident.error_code for incident in first_incidents] == [
            "artifact_checksum_conflict"
        ]


@pytest.mark.asyncio
async def test_conflict_during_commit_does_not_block_later_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch a concurrent compensation escaping reconciliation as a ledger exception."""
    async with prepared_store(tmp_path, content="conflicting") as (store, ledger, first):
        second = await _stage_one_artifact(
            BookProject(tmp_path),
            store,
            ledger,
            action_id="translate-002",
            canonical_relpath="chapters/final/002.md",
            content=b"recovered",
        )
        commit = ledger.commit_promotion_intent

        async def compensate_before_commit(intent_id: str) -> PromotionIntent:
            if intent_id == first.intent_id:
                await ledger.conflict_promotion_intent(
                    intent_id,
                    error_code="artifact_checksum_conflict",
                    message="concurrent verifier disputed canonical ownership",
                )
            return await commit(intent_id)

        monkeypatch.setattr(ledger, "commit_promotion_intent", compensate_before_commit)

        with pytest.raises(ArtifactConflictError, match="promotion conflict"):
            await store.reconcile_all()
        first_canonical = tmp_path / "chapters/final/001.md"
        assert first_canonical.read_text(encoding="utf-8") == "conflicting"
        (tmp_path / first.staged_relpath).write_text("later mutation", encoding="utf-8")

        with pytest.raises(ArtifactConflictError, match="promotion conflict"):
            await store.reconcile_all()

        assert await ledger.promotion_state(first.intent_id) == "CONFLICT"
        assert await ledger.promotion_state(second.intent_id) == "COMMITTED"
        assert first_canonical.read_text(encoding="utf-8") == "conflicting"
        assert (tmp_path / "chapters/final/002.md").read_text(encoding="utf-8") == "recovered"


@pytest.mark.asyncio
async def test_staged_mutation_after_verification_never_commits_wrong_canonical(tmp_path: Path) -> None:
    """Catch a mutable staged inode being installed after its checksum was verified."""
    async with prepared_store(tmp_path, content="expected") as (_, ledger, staged):
        staged_path = _fixture_staged_path(tmp_path)

        def mutate_staged(point: str, _: PromotionIntent | None) -> None:
            if point == "after_staged_verification":
                staged_path.write_text("wrong", encoding="utf-8")

        store = ArtifactStore(BookProject(tmp_path), ledger, test_hook=mutate_staged)
        with pytest.raises(ArtifactConflictError, match="choose the canonical artifact"):
            await store.promote(staged)

        assert await ledger.promotion_state(staged.intent_id) == "CONFLICT"
        canonical = tmp_path / "chapters/final/001.md"
        assert canonical.read_text(encoding="utf-8") == "wrong"
        assert await ledger.has_open_incident("artifact_checksum_conflict")


@pytest.mark.asyncio
async def test_post_prepare_staging_alias_never_deletes_canonical(tmp_path: Path) -> None:
    """Catch cleanup unlinking canonical after an attempt directory becomes a symlink."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, _):
        canonical = tmp_path / "chapters/final/001.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("translation", encoding="utf-8")
        staged_path = _fixture_staged_path(tmp_path)
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
    """Catch typed bundle evidence bypassing the exact attempt-staging namespace."""
    async with prepared_store(tmp_path, content="translation") as (_, ledger, _):
        source = tmp_path / "source.md"
        source.write_text("source", encoding="utf-8")
        before = await ledger.promotion_intents()

        with pytest.raises(ValueError, match="exact action/attempt namespace"):
            ArtifactBundle(
                action_id="translate-001",
                attempt=1,
                entries=(
                    ArtifactBundleEntry(
                        staged_relpath="source.md",
                        canonical_relpath="chapters/final/source.md",
                        media_type="text/markdown",
                        evidence_role="translation",
                    ),
                ),
            )

        assert source.read_text(encoding="utf-8") == "source"
        assert not (tmp_path / "chapters/final/source.md").exists()
        assert await ledger.promotion_intents() == before


@pytest.mark.asyncio
async def test_each_conflicting_intent_records_its_own_incident(tmp_path: Path) -> None:
    """Catch incident deduplication that collapses two canonical conflicts from one action."""
    async with prepared_store(tmp_path, content="first") as (store, ledger, _):
        first = tmp_path / "chapters/final/001.md"
        first.parent.mkdir(parents=True)
        first.write_text("old-first", encoding="utf-8")
        await _stage_one_artifact(
            BookProject(tmp_path),
            store,
            ledger,
            action_id="translate-002",
            canonical_relpath="chapters/final/002.md",
            content=b"second",
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
    """Catch committing to an inode whose durable canonical ancestor was renamed away."""
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
        with pytest.raises(ArtifactConflictError, match=r"repair|project directory"):
            await store.promote(staged)

        assert await ledger.promotion_state(staged.intent_id) == "CONFLICT"
        assert not (saved_chapters / "final/001.md").exists()
        assert not (outside / "final/001.md").exists()
        assert await ledger.has_open_incident("artifact_intent_invalid")


@pytest.mark.asyncio
async def test_project_root_remap_during_promotion_never_writes_external_or_commits(
    tmp_path: Path,
) -> None:
    """Catch a root rename plus symlink remap between validation and canonical creation."""
    project_root = tmp_path / "project"
    outside = tmp_path / "outside"
    outside.mkdir()
    saved_root = tmp_path / "saved-project"
    async with prepared_store(project_root, content="translation") as (_, ledger, staged):
        def remap_root(point: str, _: PromotionIntent | None) -> None:
            if point == "after_staged_verification":
                project_root.rename(saved_root)
                project_root.symlink_to(outside, target_is_directory=True)

        store = ArtifactStore(BookProject(project_root), ledger, test_hook=remap_root)
        with pytest.raises(ArtifactConflictError, match=r"project root|repair"):
            await store.promote(staged)

        assert await ledger.promotion_state(staged.intent_id) == "CONFLICT"
        assert not (outside / "chapters/final/001.md").exists()
        assert not (saved_root / "chapters/final/001.md").exists()
        assert await ledger.has_open_incident("artifact_intent_invalid")


@pytest.mark.asyncio
async def test_replaced_canonical_name_cannot_commit_or_delete_the_competing_inode(tmp_path: Path) -> None:
    """Catch canonical-name replacement after O_EXCL creation committing or deleting the competitor."""
    async with prepared_store(tmp_path, content="expected") as (_, ledger, staged):
        canonical = tmp_path / "chapters/final/001.md"

        def replace_canonical(point: str, _: PromotionIntent | None) -> None:
            if point == "after_canonical_written":
                canonical.unlink()
                canonical.write_text("competing", encoding="utf-8")

        store = ArtifactStore(BookProject(tmp_path), ledger, test_hook=replace_canonical)
        with pytest.raises(ArtifactConflictError, match=r"canonical.*replaced|choose the canonical"):
            await store.promote(staged)

        assert await ledger.promotion_state(staged.intent_id) == "CONFLICT"
        assert canonical.read_text(encoding="utf-8") == "competing"


@pytest.mark.asyncio
async def test_canonical_race_inside_ledger_commit_is_compensated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch a post-commit identity race remaining durably reported as successful."""
    async with prepared_store(tmp_path, content="expected") as (store, ledger, staged):
        canonical = tmp_path / "chapters/final/001.md"
        commit = ledger.commit_promotion_intent

        async def commit_then_replace(intent_id: str) -> PromotionIntent:
            committed = await commit(intent_id)
            canonical.unlink()
            canonical.write_text("competing", encoding="utf-8")
            return committed

        monkeypatch.setattr(ledger, "commit_promotion_intent", commit_then_replace)

        with pytest.raises(ArtifactConflictError, match="choose the canonical artifact"):
            await store.promote(staged)

        assert canonical.read_text(encoding="utf-8") == "competing"
        assert await ledger.promotion_state(staged.intent_id) == "CONFLICT"
        assert await ledger.has_open_incident("artifact_checksum_conflict")


@pytest.mark.asyncio
async def test_reconcile_compensates_drift_after_commit_before_postcheck_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch recovery trusting COMMITTED after a crash skipped filesystem postchecks."""
    async with prepared_store(tmp_path, content="expected") as (store, ledger, staged):
        commit = ledger.commit_promotion_intent

        async def commit_then_crash(intent_id: str) -> PromotionIntent:
            await commit(intent_id)
            raise InjectedCrash("injected crash after ledger commit")

        monkeypatch.setattr(ledger, "commit_promotion_intent", commit_then_crash)
        with pytest.raises(InjectedCrash, match="after ledger commit"):
            await store.promote(staged)
        assert await ledger.promotion_state(staged.intent_id) == "COMMITTED"

        canonical = tmp_path / "chapters/final/001.md"
        canonical.write_text("drifted", encoding="utf-8")
        monkeypatch.setattr(ledger, "commit_promotion_intent", commit)

        with pytest.raises(ArtifactConflictError, match="choose the canonical artifact"):
            await store.reconcile_all()

        assert canonical.read_text(encoding="utf-8") == "drifted"
        assert await ledger.promotion_state(staged.intent_id) == "CONFLICT"
        assert await ledger.has_open_incident("artifact_checksum_conflict")


@pytest.mark.asyncio
async def test_directory_chain_race_inside_ledger_commit_is_compensated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch post-commit directory rebinding leaving the intent durably successful."""
    async with prepared_store(tmp_path, content="expected") as (store, ledger, staged):
        commit = ledger.commit_promotion_intent
        chapters = tmp_path / "chapters"
        displaced = tmp_path / "displaced-chapters"

        async def commit_then_rebind(intent_id: str) -> PromotionIntent:
            committed = await commit(intent_id)
            chapters.rename(displaced)
            (chapters / "final").mkdir(parents=True)
            return committed

        monkeypatch.setattr(ledger, "commit_promotion_intent", commit_then_rebind)

        with pytest.raises(ArtifactConflictError, match=r"project directory|repair"):
            await store.promote(staged)

        assert (displaced / "final/001.md").read_text(encoding="utf-8") == "expected"
        assert not (chapters / "final/001.md").exists()
        assert await ledger.promotion_state(staged.intent_id) == "CONFLICT"
        assert await ledger.has_open_incident("artifact_intent_invalid")


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
        action_id = f"translate-special-{special}"
        canonical_relpath = f"chapters/final/{special}.md"
        special_intent = await _stage_one_artifact(
            BookProject(tmp_path),
            store,
            ledger,
            action_id=action_id,
            canonical_relpath=f"chapters/final/{special}.md",
            content=b"special",
        )
        special_path = tmp_path / f"state/staging/{action_id}/1/{canonical_relpath}"
        special_path.unlink()
        if special == "symlink":
            special_path.symlink_to(tmp_path / "source.md")
        else:
            os.mkfifo(special_path)

        with pytest.raises(ArtifactConflictError, match="repair"):
            await store.reconcile_all()

        assert (tmp_path / "chapters/final/001.md").read_text(encoding="utf-8") == "good"
        assert await ledger.has_open_incident("artifact_intent_invalid")
        assert await ledger.promotion_state(special_intent.intent_id) == "CONFLICT"


@pytest.mark.asyncio
async def test_partial_canonical_after_create_crash_is_preserved_and_never_committed(
    tmp_path: Path,
) -> None:
    """Catch recovery treating a partial O_EXCL canonical file as a successful promotion."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        with pytest.raises(InjectedCrash):
            await store.promote(staged, crash_after="after_canonical_create")
        canonical = tmp_path / "chapters/final/001.md"
        assert canonical.is_file()
        assert canonical.read_bytes() == b""
        assert not list(canonical.parent.glob(".abi-promotion-*.tmp"))

        with pytest.raises(ArtifactConflictError, match="choose the canonical artifact"):
            await store.reconcile_all()

        assert await ledger.promotion_state(staged.intent_id) == "CONFLICT"
        assert canonical.read_bytes() == b""
        assert (tmp_path / "state/staging/translate-001/1/chapters/final/001.md").read_text(
            encoding="utf-8"
        ) == "translation"
        assert await ledger.has_open_incident("artifact_checksum_conflict")


@pytest.mark.asyncio
async def test_partial_write_enospc_records_canonical_write_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch a storage write failure being mislabeled as a corrupt promotion intent."""
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        write = os.write
        writes = 0

        def partial_then_enospc(fd: int, data: bytes | memoryview) -> int:
            nonlocal writes
            if writes == 0:
                writes += 1
                return write(fd, data[:4])
            raise OSError(errno.ENOSPC, "injected storage exhaustion")

        monkeypatch.setattr(os, "write", partial_then_enospc)

        with pytest.raises(ArtifactConflictError, match=r"storage|partial canonical"):
            await store.promote(staged)

        canonical = tmp_path / "chapters/final/001.md"
        assert canonical.read_bytes() == b"tran"
        assert _fixture_staged_path(tmp_path).read_bytes() == b"translation"
        assert await ledger.promotion_state(staged.intent_id) == "CONFLICT"
        error_codes = {
            incident.error_code for incident in (await ledger.load_snapshot("run-1")).incidents
        }
        assert "canonical_write_incomplete" in error_codes
        assert "artifact_intent_invalid" not in error_codes


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical_preexists", [False, True])
async def test_parent_fsync_failure_records_canonical_write_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    canonical_preexists: bool,
) -> None:
    """Catch canonical durability failures being mislabeled as corrupt ledger paths."""
    async with prepared_store(tmp_path, content="translation") as (_, ledger, staged):
        canonical = tmp_path / "chapters/final/001.md"
        if canonical_preexists:
            canonical.parent.mkdir(parents=True)
            canonical.write_text("translation", encoding="utf-8")
        fail_directory_fsync = canonical_preexists

        def arm_failure(point: str, _: PromotionIntent | None) -> None:
            nonlocal fail_directory_fsync
            if point == "after_canonical_written":
                fail_directory_fsync = True

        store = ArtifactStore(BookProject(tmp_path), ledger, test_hook=arm_failure)
        fsync = os.fsync

        def fail_parent_fsync(fd: int) -> None:
            if fail_directory_fsync and stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.ENOSPC, "injected parent fsync exhaustion")
            fsync(fd)

        monkeypatch.setattr(os, "fsync", fail_parent_fsync)

        with pytest.raises(ArtifactConflictError, match=r"storage|partial canonical"):
            await store.promote(staged)

        assert canonical.read_text(encoding="utf-8") == "translation"
        assert await ledger.promotion_state(staged.intent_id) == "CONFLICT"
        error_codes = {
            incident.error_code for incident in (await ledger.load_snapshot("run-1")).incidents
        }
        assert "canonical_write_incomplete" in error_codes
        assert "artifact_intent_invalid" not in error_codes


@pytest.mark.asyncio
async def test_repeated_fifo_reconciliation_does_not_leak_file_descriptors(tmp_path: Path) -> None:
    """Catch the non-regular staged-file branch raising without closing its opened fd."""
    async with prepared_store(tmp_path, content="translation") as (store, _, staged):
        staged_path = _fixture_staged_path(tmp_path)
        staged_path.unlink()
        os.mkfifo(staged_path)
        baseline = len(os.listdir("/dev/fd"))

        for _ in range(20):
            with pytest.raises(ArtifactConflictError, match="repair"):
                await store.reconcile_intent(staged)

        assert len(os.listdir("/dev/fd")) <= baseline


@pytest.mark.asyncio
async def test_invalid_long_intent_does_not_block_later_valid_reconciliation(tmp_path: Path) -> None:
    """Catch ENAMETOOLONG escaping reconciliation and abandoning subsequent durable intents."""
    async with prepared_store(tmp_path, content="first") as (store, ledger, _):
        valid = await _stage_one_artifact(
            BookProject(tmp_path),
            store,
            ledger,
            action_id="translate-002",
            canonical_relpath="chapters/final/002.md",
            content=b"second",
        )
        first = (await ledger.promotion_intents())[0]
        long_component = "x" * 300
        await ledger._db.execute(
            "INSERT INTO promotion_intents (intent_id, action_id, attempt, staged_relpath, "
            "canonical_relpath, checksum, media_type, evidence_role, metadata_json, ordinal, "
            "bundle_digest, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "corrupt-long-intent",
                "translate-001",
                1,
                f"state/staging/translate-001/1/{long_component}/invalid.md",
                f"{long_component}/invalid.md",
                "a7937b64b8caa58f03721bb6bacf1b8089a7d7783c12fc0157bca5fb8e1f9f78",
                "text/markdown",
                "translation",
                '{"items":[]}',
                1,
                first.bundle_digest,
                "PENDING",
                first.created_at.isoformat(),
            ),
        )
        await ledger._db.commit()
        invalid = await ledger.get_promotion_intent("corrupt-long-intent")

        with pytest.raises(ArtifactConflictError, match="promotion conflict"):
            await store.reconcile_all()

        assert await ledger.promotion_state(invalid.intent_id) == "CONFLICT"
        assert await ledger.promotion_state(valid.intent_id) == "COMMITTED"
        assert (tmp_path / "chapters/final/002.md").read_text(encoding="utf-8") == "second"
        assert await ledger.has_open_incident("artifact_intent_invalid")


def test_staging_dir_rejects_path_escape(tmp_path: Path) -> None:
    """Catch action identifiers that would write a staging artifact outside its attempt directory."""
    store = ArtifactStore(BookProject(tmp_path), None)

    with pytest.raises(ValueError, match="safe path component"):
        store.staging_dir("../outside", 1)
