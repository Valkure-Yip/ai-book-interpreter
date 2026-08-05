"""Crash recovery and retry-transition protocol tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from abi.orchestrator.committer import Committer
from abi.orchestrator.reconcile import Reconciler
from abi.project.artifacts import ArtifactConflictError
from abi.project.layout import BookProject
from abi.project.run_ledger import (
    LedgerConflictError,
    LedgerError,
    LedgerTransitionError,
    RunLedger,
)
from abi.providers.orchestration_runtime.runtime import (
    DurableLoopRuntime,
    _compact_checkpoint_history,
)
from abi.types.orchestration import (
    ActionStatus,
    Indeterminate,
    ProbeResolution,
    RepairRequired,
    RetryableFailure,
    RunStatus,
    Succeeded,
    canonical_failure_signature,
)
from tests.test_dynamic_controller import (
    BoundaryCrash,
    SuccessTemplate,
    _authorize_one,
    _completed_capability,
    _controller_rig,
    _patch,
)
from tests.test_phase_a_ledger_protocol import _retry_receipt, _seed


@pytest.mark.asyncio
async def test_retry_successor_replay_rejects_drifted_frozen_policy(
    tmp_path: Path,
) -> None:
    """Catch exact replay accepting a successor whose frozen retry facts drifted."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed(ledger)
        await ledger.start_attempt("a1", attempt=1)
        await ledger.record_attempt_outcome(_retry_receipt())
        await ledger.route_retry_from_receipt("a1", attempt=1)
        await ledger.create_next_attempt("a1", previous_attempt=1)
        await ledger._db.execute(
            "UPDATE action_attempts SET retry_policy_fingerprint = ? "
            "WHERE action_id = ? AND attempt = ?",
            ("f" * 64, "a1", 2),
        )
        await ledger._db.commit()

        with pytest.raises(LedgerConflictError, match="frozen facts"):
            await ledger.create_next_attempt("a1", previous_attempt=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    (ActionStatus.RUNNING, ActionStatus.INDETERMINATE, ActionStatus.REPAIR_REQUIRED),
)
async def test_create_next_attempt_rejects_every_non_retry_wait_state(
    tmp_path: Path, status: ActionStatus
) -> None:
    """Catch successor allocation from a claimed, uncertain, or repair-blocked attempt."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed(ledger)
        await ledger.start_attempt("a1", attempt=1)
        await ledger._db.execute(
            "UPDATE action_attempts SET status = ? WHERE action_id = 'a1' AND attempt = 1",
            (status.value,),
        )
        await ledger._db.execute(
            "UPDATE actions SET status = ? WHERE action_id = 'a1'",
            (status.value,),
        )
        await ledger._db.commit()

        with pytest.raises(LedgerTransitionError, match="RETRY_WAIT"):
            await ledger.create_next_attempt("a1", previous_attempt=1)

        assert await ledger.attempt_numbers("a1") == (1,)


@pytest.mark.asyncio
async def test_create_next_attempt_rejects_unknown_persisted_status(
    tmp_path: Path,
) -> None:
    """Catch corrupt status parsing falling through into successor creation."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed(ledger)
        await ledger.start_attempt("a1", attempt=1)
        await ledger._db.execute(
            "UPDATE action_attempts SET status = 'UNKNOWN' "
            "WHERE action_id = 'a1' AND attempt = 1"
        )
        await ledger._db.commit()

        with pytest.raises(LedgerError, match=r"UNKNOWN.*repair or recreate"):
            await ledger.create_next_attempt("a1", previous_attempt=1)

        assert await ledger.attempt_numbers("a1") == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict_entry", (0, 1))
async def test_two_entry_canonical_conflict_preserves_protocol_and_siblings(
    tmp_path: Path, conflict_entry: int
) -> None:
    """Catch entry-local canonical conflict erasing receipts or mutating later siblings."""
    canonical_paths = (tmp_path / "reports/a.json", tmp_path / "reports/b.json")
    conflict_path = canonical_paths[conflict_entry]
    conflict_path.parent.mkdir(parents=True, exist_ok=True)
    conflict_path.write_bytes(b"operator-selected-canonical")

    async with _controller_rig(
        tmp_path,
        definitions=(("work.multi", (SuccessTemplate(),)),),
        patches=(),
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.multi")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id,
            action=action,
            snapshot=snapshot,
            attempt=1,
        )
        assert isinstance(envelope.outcome, Succeeded)
        receipt_before = await rig.ledger.get_attempt_outcome(action.action_id, 1)
        staged_before = tuple(
            (tmp_path / entry.staged_relpath).read_bytes()
            for entry in envelope.outcome.artifact_bundle.entries
        )

        with pytest.raises(ArtifactConflictError):
            await rig.committer.commit(
                run_id=rig.run_id,
                action=action,
                attempt=1,
                outcome=envelope.outcome,
            )

        final = await rig.ledger.load_snapshot(rig.run_id)
        receipt_after = await rig.ledger.get_attempt_outcome(action.action_id, 1)
        _, intents = await rig.ledger.get_gate_receipt_and_intents(action.action_id, 1)
        assert final.status is RunStatus.BLOCKED
        assert await rig.ledger.action_status(action.action_id) is ActionStatus.REPAIR_REQUIRED
        assert await rig.ledger.attempt_status(action.action_id, 1) is ActionStatus.REPAIR_REQUIRED
        assert receipt_after == receipt_before
        assert len(intents) == 2
        assert {intent.status for intent in intents} == {"CONFLICT"}
        assert conflict_path.read_bytes() == b"operator-selected-canonical"
        assert tuple(
            (tmp_path / entry.staged_relpath).read_bytes()
            for entry in envelope.outcome.artifact_bundle.entries
        ) == staged_before
        if conflict_entry == 0:
            assert not canonical_paths[1].exists()
        else:
            assert canonical_paths[0].read_bytes() == staged_before[0]
        integrity = tuple(
            incident
            for incident in final.incidents
            if incident.action_id == action.action_id
        )
        assert len(integrity) == 1
        assert integrity[0].subject == "attempt:1"
        assert (
            integrity[0].repair_class,
            integrity[0].repair_source,
            integrity[0].reason_code,
        ) == (
            "integrity",
            "integrity_guard",
            "artifact_checksum_conflict",
        )
        assert rig.executors["work.multi"].attempt_ids == [1]


@pytest.mark.asyncio
async def test_conflict_recovery_requires_new_plan_action_and_staging_namespace(
    tmp_path: Path,
) -> None:
    """Catch an operator unblock reviving the immutable conflicting Action attempt."""
    conflict_path = tmp_path / "reports/a.json"
    conflict_path.parent.mkdir(parents=True, exist_ok=True)
    conflict_path.write_bytes(b"operator-selected-canonical")
    async with _controller_rig(
        tmp_path,
        definitions=(
            ("work.multi", (SuccessTemplate(),)),
            ("work.replacement", (SuccessTemplate(),)),
        ),
        patches=(_patch("replacement", "work.replacement"),),
        complete_when=_completed_capability("work.replacement"),
    ) as rig:
        original, snapshot = await _authorize_one(rig, "work.multi", proposal_id="initial")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id,
            action=original,
            snapshot=snapshot,
            attempt=1,
        )
        assert isinstance(envelope.outcome, Succeeded)
        with pytest.raises(ArtifactConflictError):
            await rig.committer.commit(
                run_id=rig.run_id,
                action=original,
                attempt=1,
                outcome=envelope.outcome,
            )

        old_receipt = await rig.ledger.get_attempt_outcome(original.action_id, 1)
        old_gate, old_intents = await rig.ledger.get_gate_receipt_and_intents(
            original.action_id, 1
        )
        old_attempt = await rig.ledger.get_attempt(original.action_id, 1)
        with pytest.raises(LedgerTransitionError):
            await rig.ledger.create_next_attempt(original.action_id, previous_attempt=1)

        # Task 10 owns the public unblock command. Here we emulate its explicit operator
        # cleanup boundary while preserving every old protocol row.
        for canonical in (tmp_path / "reports/a.json", tmp_path / "reports/b.json"):
            canonical.unlink(missing_ok=True)
        await rig.ledger._db.execute(
            "UPDATE incidents SET status = 'RESOLVED', resolved_at = ? WHERE run_id = ?",
            ("2026-08-06T00:00:00+00:00", rig.run_id),
        )
        await rig.ledger._db.commit()
        await rig.ledger.set_run_status(rig.run_id, RunStatus.RUNNING)

        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        actions = await rig.ledger.list_actions(rig.run_id)
        replacements = tuple(
            action for action in actions if action.action_id != original.action_id
        )
        assert len(replacements) == 1
        replacement = replacements[0]
        replacement_attempt = await rig.ledger.get_attempt(replacement.action_id, 1)
        assert replacement.plan_version == 2
        assert replacement.action_id != original.action_id
        assert replacement.status is ActionStatus.SUCCEEDED
        assert replacement_attempt.staging_relpath == (
            f"state/staging/{replacement.action_id}/1"
        )
        assert replacement_attempt.staging_relpath != old_attempt.staging_relpath
        assert await rig.ledger.get_attempt_outcome(original.action_id, 1) == old_receipt
        assert await rig.ledger.get_gate_receipt_and_intents(
            original.action_id, 1
        ) == (old_gate, old_intents)
        assert await rig.ledger.attempt_numbers(original.action_id) == (1,)
        assert await rig.ledger.action_status(original.action_id) is ActionStatus.REPAIR_REQUIRED
        assert rig.executors["work.multi"].attempt_ids == [1]
        assert rig.executors["work.replacement"].attempt_ids == [1]
        replacement_artifacts = tuple(
            artifact
            for artifact in (await rig.ledger.load_snapshot(rig.run_id)).artifacts
            if artifact.producer_action_id == replacement.action_id
        )
        assert replacement_artifacts


@pytest.mark.asyncio
async def test_durable_loop_compacts_abi_owned_checkpoint_history(
    tmp_path: Path,
) -> None:
    """Catch repeated resumes growing ABI-owned checkpoint history without a bound."""
    checkpoint_path = tmp_path / "graph-checkpoints.sqlite"
    runtime = DurableLoopRuntime(checkpoint_path=checkpoint_path, max_cycles=1)
    calls = 0

    async def tick(_run_id: str) -> bool:
        nonlocal calls
        calls += 1
        return False

    for _ in range(12):
        await runtime.run(run_id="run-1", tick=tick)

    with sqlite3.connect(checkpoint_path) as db:
        checkpoint_count = int(
            db.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?",
                ("run-1",),
            ).fetchone()[0]
        )
        orphan_writes = int(
            db.execute(
                "SELECT COUNT(*) FROM writes AS w LEFT JOIN checkpoints AS c "
                "ON c.thread_id = w.thread_id "
                "AND c.checkpoint_ns = w.checkpoint_ns "
                "AND c.checkpoint_id = w.checkpoint_id "
                "WHERE w.thread_id = ? AND c.checkpoint_id IS NULL",
                ("run-1",),
            ).fetchone()[0]
        )

    assert calls == 12
    assert checkpoint_count <= 8
    assert orphan_writes == 0

    await runtime.run(run_id="run-1", tick=tick)
    assert calls == 13


@pytest.mark.asyncio
async def test_checkpoint_compaction_isolated_by_namespace_and_thread(
    tmp_path: Path,
) -> None:
    """Catch one loop namespace evicting another namespace or pending thread."""
    checkpoint_path = tmp_path / "namespaced-checkpoints.sqlite"
    async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        await saver.setup()

        for namespace in ("planner", "pending"):
            config: RunnableConfig = {
                "configurable": {
                    "thread_id": "run-1",
                    "checkpoint_ns": namespace,
                }
            }
            for value in range(12):
                checkpoint = empty_checkpoint()
                checkpoint["channel_values"] = {"value": value}
                config = await saver.aput(
                    config,
                    checkpoint,
                    {"source": "loop", "step": value, "parents": {}},
                    {},
                )

        other_config: RunnableConfig = {
            "configurable": {
                "thread_id": "pending-run",
                "checkpoint_ns": "hitl",
            }
        }
        other_checkpoint = empty_checkpoint()
        other_checkpoint["channel_values"] = {"pending": True}
        await saver.aput(
            other_config,
            other_checkpoint,
            {"source": "loop", "step": 1, "parents": {}},
            {},
        )
        cursor = await saver.conn.execute(
            "SELECT checkpoint_ns, checkpoint_id FROM checkpoints "
            "WHERE thread_id = ? ORDER BY checkpoint_ns, checkpoint_id",
            ("pending-run",),
        )
        other_before = tuple(await cursor.fetchall())
        await cursor.close()

        await _compact_checkpoint_history(saver, thread_id="run-1")

        cursor = await saver.conn.execute(
            "SELECT checkpoint_ns, COUNT(*) FROM checkpoints "
            "WHERE thread_id = ? GROUP BY checkpoint_ns ORDER BY checkpoint_ns",
            ("run-1",),
        )
        counts = {str(row[0]): int(row[1]) for row in await cursor.fetchall()}
        await cursor.close()
        assert set(counts) == {"pending", "planner"}
        assert all(1 <= count <= 8 for count in counts.values())

        for namespace in ("planner", "pending"):
            config = {
                "configurable": {
                    "thread_id": "run-1",
                    "checkpoint_ns": namespace,
                }
            }
            latest = await saver.aget_tuple(config)
            assert latest is not None
            continued = empty_checkpoint()
            continued["channel_values"] = {"continued": namespace}
            continued_config = await saver.aput(
                latest.config,
                continued,
                {"source": "loop", "step": 13, "parents": {}},
                {},
            )
            resumed = await saver.aget_tuple(continued_config)
            assert resumed is not None
            assert resumed.parent_config == latest.config

        cursor = await saver.conn.execute(
            "SELECT checkpoint_ns, checkpoint_id FROM checkpoints "
            "WHERE thread_id = ? ORDER BY checkpoint_ns, checkpoint_id",
            ("pending-run",),
        )
        assert tuple(await cursor.fetchall()) == other_before
        await cursor.close()


@pytest.mark.asyncio
async def test_after_outcome_receipt_is_a_distinct_durable_crash_boundary(
    tmp_path: Path,
) -> None:
    """Catch receipt durability and controller-visible output sharing one hook."""

    def crash(point: str, _detail: object) -> None:
        if point == "after_outcome_receipt":
            raise BoundaryCrash(point)

    async with _controller_rig(
        tmp_path,
        definitions=(("work.receipt", (SuccessTemplate(),)),),
        patches=(),
        dispatcher_hook=crash,
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.receipt")
        with pytest.raises(BoundaryCrash, match="after_outcome_receipt"):
            await rig.dispatcher.execute(
                run_id=rig.run_id,
                action=action,
                snapshot=snapshot,
                attempt=1,
            )

        assert await rig.ledger.get_attempt_outcome(action.action_id, 1)
        assert (await rig.ledger.attempt_status(action.action_id, 1)).value == "RUNNING"
        assert rig.executors["work.receipt"].attempt_ids == [1]


@pytest.mark.asyncio
async def test_before_graph_checkpoint_crash_keeps_committed_action_exactly_once(
    tmp_path: Path,
) -> None:
    """Catch a graph-checkpoint crash causing a committed Action to execute twice."""

    def crash(point: str, _detail: object) -> None:
        if point == "before_graph_checkpoint":
            raise BoundaryCrash(point)

    async with _controller_rig(
        tmp_path,
        definitions=(("work.checkpoint", (SuccessTemplate(),)),),
        patches=(_patch("checkpoint", "work.checkpoint"),),
        complete_when=_completed_capability("work.checkpoint"),
    ) as rig:
        crashing_runtime = DurableLoopRuntime(
            checkpoint_path=BookProject(tmp_path).graph_checkpoints,
            max_cycles=4,
            on_exhausted=rig.controller.on_cycles_exhausted,
            test_hook=crash,
        )
        with pytest.raises(BoundaryCrash, match="before_graph_checkpoint"):
            await crashing_runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        action = (await rig.ledger.list_actions(rig.run_id))[0]
        assert (await rig.ledger.action_status(action.action_id)).value == "SUCCEEDED"
        assert rig.executors["work.checkpoint"].attempt_ids == [1]

        clean_runtime = DurableLoopRuntime(
            checkpoint_path=BookProject(tmp_path).graph_checkpoints,
            max_cycles=4,
            on_exhausted=rig.controller.on_cycles_exhausted,
        )
        await clean_runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
        assert rig.executors["work.checkpoint"].attempt_ids == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "before_dispatch",
        "after_outcome_receipt",
        "before_gate_receipt_and_intents",
        "before_graph_checkpoint",
    ),
)
async def test_two_entry_controller_and_checkpoint_boundaries_resume_exactly_once(
    tmp_path: Path, boundary: str
) -> None:
    """Join controller/receipt/gate/checkpoint edges to the two-entry bundle matrix."""
    enabled = True

    def crash(point: str, _detail: object) -> None:
        if enabled and point == boundary:
            raise BoundaryCrash(point)

    async with _controller_rig(
        tmp_path,
        definitions=(("work.multi", (SuccessTemplate(),)),),
        patches=(_patch("multi", "work.multi"),),
        complete_when=_completed_capability("work.multi"),
        controller_hook=crash,
        dispatcher_hook=crash,
        committer_hook=crash,
    ) as rig:
        crashing_runtime = DurableLoopRuntime(
            checkpoint_path=BookProject(tmp_path).graph_checkpoints,
            max_cycles=4,
            on_exhausted=rig.controller.on_cycles_exhausted,
            test_hook=crash,
        )
        with pytest.raises(BoundaryCrash, match=boundary):
            await crashing_runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        action = (await rig.ledger.list_actions(rig.run_id))[0]
        if boundary == "before_dispatch":
            assert await rig.ledger.attempt_numbers(action.action_id) == ()
            assert rig.executors["work.multi"].attempt_ids == []
        else:
            assert await rig.ledger.get_attempt_outcome(action.action_id, 1)
            assert rig.executors["work.multi"].attempt_ids == [1]
        if boundary == "before_gate_receipt_and_intents":
            assert await rig.ledger.promotion_intents(rig.run_id) == ()
            assert await rig.ledger.attempt_status(action.action_id, 1) is ActionStatus.RUNNING
        if boundary == "before_graph_checkpoint":
            assert await rig.ledger.action_status(action.action_id) is ActionStatus.SUCCEEDED
            assert await rig.ledger.count_artifacts_for(action.action_id) == 2

        enabled = False
        clean_runtime = DurableLoopRuntime(
            checkpoint_path=BookProject(tmp_path).graph_checkpoints,
            max_cycles=4,
            on_exhausted=rig.controller.on_cycles_exhausted,
        )
        await clean_runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        assert await rig.ledger.action_status(action.action_id) is ActionStatus.SUCCEEDED
        assert await rig.ledger.count_committed_actions(action.action_id) == 1
        assert await rig.ledger.count_artifacts_for(action.action_id) == 2
        assert rig.executors["work.multi"].attempt_ids == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "after_first_intent_commit_before_postcheck",
        "between_bundle_entries",
    ),
)
async def test_two_entry_internal_promotion_boundaries_resume_exactly_once(
    tmp_path: Path, boundary: str
) -> None:
    """Catch the first durable intent being replayed or the second entry being skipped."""
    enabled = True

    def crash(point: str, detail: object) -> None:
        if not enabled:
            return
        if (
            boundary == "after_first_intent_commit_before_postcheck"
            and point == "after_intent_commit_before_postcheck"
            and getattr(detail, "ordinal", None) == 0
        ):
            raise BoundaryCrash(boundary)
        if boundary == "between_bundle_entries" and point == boundary:
            raise BoundaryCrash(boundary)

    async with _controller_rig(
        tmp_path,
        definitions=(("work.multi", (SuccessTemplate(),)),),
        patches=(),
        committer_hook=crash,
    ) as rig:
        rig.store._test_hook = crash
        action, snapshot = await _authorize_one(rig, "work.multi")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id,
            action=action,
            snapshot=snapshot,
            attempt=1,
        )
        assert isinstance(envelope.outcome, Succeeded)
        with pytest.raises(BoundaryCrash, match=boundary):
            await rig.committer.commit(
                run_id=rig.run_id,
                action=action,
                attempt=1,
                outcome=envelope.outcome,
            )

        _, interrupted = await rig.ledger.get_gate_receipt_and_intents(
            action.action_id, 1
        )
        assert tuple(intent.status for intent in interrupted) == (
            "COMMITTED",
            "PENDING",
        )
        assert await rig.ledger.attempt_status(action.action_id, 1) is ActionStatus.RUNNING

        enabled = False
        rig.store._test_hook = None
        await Reconciler(
            ledger=rig.ledger,
            artifacts=rig.store,
            registry=rig.registry,
            committer=Committer(
                ledger=rig.ledger,
                registry=rig.registry,
                project=BookProject(tmp_path),
                artifacts=rig.store,
            ),
        ).reconcile(rig.run_id)

        assert await rig.ledger.action_status(action.action_id) is ActionStatus.SUCCEEDED
        assert await rig.ledger.count_artifacts_for(action.action_id) == 2
        assert rig.executors["work.multi"].attempt_ids == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary", ("after_first_canonical_create", "after_first_canonical_write")
)
async def test_two_entry_first_canonical_io_boundary_has_exact_recovery_semantics(
    tmp_path: Path, boundary: str
) -> None:
    """Recover both high-level boundaries only after the first copy has complete bytes."""
    enabled = True

    def crash(point: str, detail: object) -> None:
        if (
            enabled
            and point == boundary
            and getattr(detail, "ordinal", None) == 0
        ):
            raise BoundaryCrash(boundary)

    async with _controller_rig(
        tmp_path,
        definitions=(("work.multi", (SuccessTemplate(),)),),
        patches=(),
    ) as rig:
        rig.store._test_hook = crash
        action, snapshot = await _authorize_one(rig, "work.multi")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id,
            action=action,
            snapshot=snapshot,
            attempt=1,
        )
        assert isinstance(envelope.outcome, Succeeded)
        staged_before = tuple(
            (tmp_path / entry.staged_relpath).read_bytes()
            for entry in envelope.outcome.artifact_bundle.entries
        )
        with pytest.raises(BoundaryCrash, match=boundary):
            await rig.committer.commit(
                run_id=rig.run_id,
                action=action,
                attempt=1,
                outcome=envelope.outcome,
            )

        first = tmp_path / "reports/a.json"
        second = tmp_path / "reports/b.json"
        _, interrupted = await rig.ledger.get_gate_receipt_and_intents(
            action.action_id, 1
        )
        assert tuple(intent.status for intent in interrupted) == ("PENDING", "PENDING")
        assert not second.exists()
        assert first.read_bytes() == staged_before[0]

        enabled = False
        rig.store._test_hook = None
        await rig.reconciler.reconcile(rig.run_id)

        assert tuple(
            (tmp_path / entry.staged_relpath).read_bytes()
            for entry in envelope.outcome.artifact_bundle.entries
        ) == staged_before
        assert rig.executors["work.multi"].attempt_ids == [1]
        assert await rig.ledger.action_status(action.action_id) is ActionStatus.SUCCEEDED
        assert await rig.ledger.count_artifacts_for(action.action_id) == 2
        assert second.read_bytes() == staged_before[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "after_retry_wait",
        "before_start_next_attempt",
        "before_dispatch_next_attempt",
    ),
)
async def test_retry_transition_exact_hooks_resume_only_attempt_two(
    tmp_path: Path, boundary: str
) -> None:
    """Catch retry crash aliases that obscure the exact durable transition."""
    enabled = True

    def crash(point: str, _detail: object) -> None:
        if enabled and point == boundary:
            raise BoundaryCrash(point)

    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "network.fetch",
                (
                    RetryableFailure(error_code="temporary", message="retry"),
                    SuccessTemplate(),
                ),
            ),
        ),
        patches=(_patch("fetch", "network.fetch"),),
        complete_when=_completed_capability("network.fetch"),
        controller_hook=crash,
    ) as rig:
        with pytest.raises(BoundaryCrash, match=boundary):
            await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        action = (await rig.ledger.list_actions(rig.run_id))[0]
        assert await rig.ledger.attempt_status(action.action_id, 1) is ActionStatus.RETRY_WAIT
        if boundary == "after_retry_wait":
            assert await rig.ledger.attempt_numbers(action.action_id) == (1,)
        else:
            assert await rig.ledger.attempt_numbers(action.action_id) == (1, 2)
            assert await rig.ledger.attempt_status(
                action.action_id, 2
            ) is ActionStatus.AUTHORIZED

        enabled = False
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
        assert rig.executors["network.fetch"].attempt_ids == [1, 2]
        assert await rig.ledger.attempt_numbers(action.action_id) == (1, 2)


@pytest.mark.asyncio
async def test_retry_crash_after_claim_never_invokes_attempt_two_executor(
    tmp_path: Path,
) -> None:
    """Catch recovery demoting a claimed retry attempt and executing it again."""
    enabled = True

    def crash(point: str, _detail: object) -> None:
        if enabled and point == "after_start_next_attempt":
            raise BoundaryCrash(point)

    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "network.fetch",
                (
                    RetryableFailure(error_code="temporary", message="retry"),
                    SuccessTemplate(),
                ),
            ),
        ),
        patches=(_patch("fetch", "network.fetch"),),
        complete_when=_completed_capability("network.fetch"),
        dispatcher_hook=crash,
    ) as rig:
        with pytest.raises(BoundaryCrash, match="after_start_next_attempt"):
            await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        action = (await rig.ledger.list_actions(rig.run_id))[0]
        assert await rig.ledger.attempt_numbers(action.action_id) == (1, 2)
        assert await rig.ledger.attempt_status(action.action_id, 1) is ActionStatus.RETRY_WAIT
        assert await rig.ledger.attempt_status(action.action_id, 2) is ActionStatus.RUNNING
        assert rig.executors["network.fetch"].attempt_ids == [1]

        enabled = False
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
        assert rig.executors["network.fetch"].attempt_ids == [1]
        assert await rig.ledger.attempt_status(
            action.action_id, 2
        ) is ActionStatus.REPAIR_REQUIRED
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "after_retry_wait",
        "before_create_next_attempt",
        "after_create_next_attempt",
        "before_start_next_attempt",
        "before_dispatch_next_attempt",
    ),
)
async def test_absent_probe_retry_crashes_create_and_dispatch_only_attempt_two(
    tmp_path: Path, boundary: str
) -> None:
    """Catch probe-absent recovery creating a successor inside resolution or reusing attempt one."""
    enabled = True
    operation_key = "publish:absent-crash-1"

    def crash(point: str, _detail: object) -> None:
        if enabled and point == boundary:
            raise BoundaryCrash(point)

    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "release.publish",
                (
                    Indeterminate(
                        operation_key=operation_key,
                        error_code="provider_timeout",
                        failure_signature=canonical_failure_signature(
                            "release.publish", "{}", "provider_timeout"
                        ),
                        message="remote result unknown",
                    ),
                    SuccessTemplate(),
                ),
            ),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key=operation_key,
                        disposition="absent",
                        evidence_refs=("external:not-found",),
                        message="remote operation absent",
                    ),
                ),
            ),
        ),
        patches=(_patch("publish", "release.publish"),),
        spec_options={
            "release.publish": {
                "probe_capability": "release.probe",
                "retryable_codes": ("provider_timeout",),
            }
        },
        probe_capabilities=frozenset({"release.probe"}),
        complete_when=_completed_capability("release.publish"),
        controller_hook=crash,
    ) as rig:
        with pytest.raises(BoundaryCrash, match=boundary):
            await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        original = next(
            action
            for action in await rig.ledger.list_actions(rig.run_id)
            if action.capability == "release.publish"
        )
        assert await rig.ledger.attempt_status(
            original.action_id, 1
        ) is ActionStatus.RETRY_WAIT
        assert rig.executors["release.publish"].attempt_ids == [1]
        assert rig.executors["release.probe"].attempt_ids == [1]
        if boundary in {"after_retry_wait", "before_create_next_attempt"}:
            assert await rig.ledger.attempt_numbers(original.action_id) == (1,)
        else:
            assert await rig.ledger.attempt_numbers(original.action_id) == (1, 2)
            assert await rig.ledger.attempt_status(
                original.action_id, 2
            ) is ActionStatus.AUTHORIZED

        enabled = False
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
        assert rig.executors["release.publish"].attempt_ids == [1, 2]
        assert rig.executors["release.probe"].attempt_ids == [1]
        assert await rig.ledger.attempt_numbers(original.action_id) == (1, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "before_repair_fact_commit",
        "after_repair_fact_commit",
        "before_semantic_replan",
        "after_semantic_replan",
        "before_repair_action_authorization",
        "after_repair_action_authorization",
    ),
)
async def test_semantic_repair_exact_crash_boundaries_create_one_new_action(
    tmp_path: Path, boundary: str
) -> None:
    """Catch semantic recovery reentering the original attempt or duplicating repair work."""
    enabled = True

    def crash(point: str, _detail: object) -> None:
        if enabled and point == boundary:
            raise BoundaryCrash(point)

    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "work.initial",
                (
                    RepairRequired(
                        repair_class="semantic",
                        repair_source="action_outcome",
                        reason_code="term_drift",
                        defect_codes=("term_drift",),
                        message="repair glossary",
                    ),
                ),
            ),
            ("repair.glossary", (SuccessTemplate(),)),
        ),
        patches=(
            _patch("initial", "work.initial"),
            _patch("repair", "repair.glossary"),
        ),
        semantic_repair_mappings=(("term_drift", "repair.glossary"),),
        complete_when=_completed_capability("repair.glossary"),
        controller_hook=crash,
    ) as rig:
        rig.ledger._test_hook = crash
        with pytest.raises(BoundaryCrash, match=boundary):
            await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        assert rig.executors["work.initial"].attempt_ids == [1]
        original = next(
            action
            for action in await rig.ledger.list_actions(rig.run_id)
            if action.capability == "work.initial"
        )
        assert await rig.ledger.attempt_numbers(original.action_id) == (1,)

        enabled = False
        rig.ledger._test_hook = None
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        final = await rig.ledger.load_snapshot(rig.run_id)
        repairs = tuple(
            action for action in final.actions if action.capability == "repair.glossary"
        )
        assert final.status is RunStatus.COMPLETED
        assert final.plan_version == 2
        assert len(repairs) == 1
        assert repairs[0].action_id != original.action_id
        repair_attempt = await rig.ledger.get_attempt(repairs[0].action_id, 1)
        original_attempt = await rig.ledger.get_attempt(original.action_id, 1)
        assert await rig.ledger.attempt_numbers(repairs[0].action_id) == (1,)
        assert repair_attempt.staging_relpath == f"state/staging/{repairs[0].action_id}/1"
        assert repair_attempt.staging_relpath != original_attempt.staging_relpath
        assert rig.executors["work.initial"].attempt_ids == [1]
        assert rig.executors["repair.glossary"].attempt_ids == [1]


@pytest.mark.asyncio
async def test_integrity_incident_between_repair_fact_and_authorization_blocks_replan(
    tmp_path: Path,
) -> None:
    """Catch a stale semantic repair authorizing work after integrity evidence arrives."""
    enabled = True

    def crash(point: str, _detail: object) -> None:
        if enabled and point == "after_repair_fact_commit":
            raise BoundaryCrash(point)

    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "work.initial",
                (
                    RepairRequired(
                        repair_class="semantic",
                        repair_source="action_outcome",
                        reason_code="term_drift",
                        defect_codes=("term_drift",),
                        message="repair glossary",
                    ),
                ),
            ),
            ("repair.glossary", (SuccessTemplate(),)),
        ),
        patches=(
            _patch("initial", "work.initial"),
            _patch("repair", "repair.glossary"),
        ),
        semantic_repair_mappings=(("term_drift", "repair.glossary"),),
        controller_hook=crash,
    ) as rig:
        rig.ledger._test_hook = crash
        with pytest.raises(BoundaryCrash, match="after_repair_fact_commit"):
            await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        original = (await rig.ledger.list_actions(rig.run_id))[0]
        receipt_before = await rig.ledger.get_attempt_outcome(original.action_id, 1)
        await rig.ledger.record_incident(
            rig.run_id,
            error_code="artifact_checksum_conflict",
            message="canonical integrity changed before repair authorization",
            action_id=original.action_id,
            repair_class="integrity",
            repair_source="integrity_guard",
            reason_code="artifact_checksum_conflict",
        )
        await rig.ledger.set_run_status(rig.run_id, RunStatus.BLOCKED)

        injected = next(
            incident
            for incident in (await rig.ledger.load_snapshot(rig.run_id)).incidents
            if incident.error_code == "artifact_checksum_conflict"
        )
        assert (
            injected.repair_class,
            injected.repair_source,
            injected.reason_code,
        ) == ("integrity", "integrity_guard", "artifact_checksum_conflict")
        incident_event = next(
            event
            for event in await rig.ledger.undelivered_events(rig.run_id)
            if event.event_name == "incident.created"
            and event.event_id == f"incident.created:{injected.incident_id}"
        )
        incident_payload = json.loads(incident_event.payload_json)
        assert (
            incident_payload["repair_class"],
            incident_payload["repair_source"],
            incident_payload["reason_code"],
        ) == ("integrity", "integrity_guard", "artifact_checksum_conflict")

        enabled = False
        rig.ledger._test_hook = None
        planner_calls = rig.planner.call_count
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        assert rig.planner.call_count == planner_calls
        assert len(await rig.ledger.list_actions(rig.run_id)) == 1
        assert await rig.ledger.get_attempt_outcome(original.action_id, 1) == receipt_before
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        assert rig.executors["work.initial"].attempt_ids == [1]
        assert rig.executors["repair.glossary"].attempt_ids == []
