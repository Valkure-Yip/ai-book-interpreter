"""Integration tests for the SQLite business ledger."""

from __future__ import annotations

from pathlib import Path

import pytest

from abi.project.run_ledger import (
    LedgerConflictError,
    LedgerTransitionError,
    RunLedger,
    RunSeed,
    SuccessCommit,
)
from abi.types.orchestration import (
    ActionStatus,
    ArtifactRef,
    AuthorizedAction,
    GateEvidence,
    PlanPatch,
    ProposedAction,
    RunStatus,
)


def _run_seed() -> RunSeed:
    return RunSeed(run_id="run-1", budget_usd=5.0)


def _action(action_id: str = "a1") -> AuthorizedAction:
    return AuthorizedAction(
        action_id=action_id,
        proposal_id="proposal-1",
        plan_version=1,
        capability="source.ingest",
        parameters_json='{"source_relpath":"source/raw.txt"}',
        idempotency_key=f"action:{action_id}",
    )


async def _seed_authorized_action(ledger: RunLedger, action_id: str = "a1") -> None:
    run_id = await ledger.create_run(_run_seed())
    await ledger.append_plan(
        run_id,
        PlanPatch(
            objective="ingest source",
            proposed_actions=(ProposedAction(proposal_id="proposal-1", capability="source.ingest"),),
            rationale="first durable plan",
        ),
    )
    await ledger.authorize_actions(run_id, (_action(action_id),))
    await ledger.start_attempt(action_id)


def _success_commit(action_id: str, checksum: str) -> SuccessCommit:
    return SuccessCommit(
        action_id=action_id,
        attempt=1,
        artifacts=(
            ArtifactRef(
                artifact_id=f"artifact-{action_id}",
                relpath=f"source/{action_id}.json",
                sha256=checksum,
                producer_action_id=action_id,
            ),
        ),
        gate_evidence=(
            GateEvidence(
                evidence_id=f"gate-{action_id}",
                gate="source_manifest",
                passed=True,
                validator_version="1",
                artifact_checksums=(checksum,),
            ),
        ),
        cost_usd=0.25,
    )


@pytest.mark.asyncio
async def test_commit_success_is_exactly_once(tmp_path: Path) -> None:
    """Catch duplicate durable commits that create duplicate facts or events."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_authorized_action(ledger)

        first = await ledger.commit_success(_success_commit("a1", checksum="abc"))
        second = await ledger.commit_success(_success_commit("a1", checksum="abc"))

        assert first == second
        assert await ledger.count_committed_actions("a1") == 1
        assert await ledger.count_outbox_events("action.committed", "a1") == 1


@pytest.mark.asyncio
async def test_different_success_checksum_creates_conflict_incident(tmp_path: Path) -> None:
    """Catch an action replay that overwrites a previously committed artifact."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_authorized_action(ledger)
        await ledger.commit_success(_success_commit("a1", checksum="abc"))

        with pytest.raises(LedgerConflictError, match="inspect the canonical artifact"):
            await ledger.commit_success(_success_commit("a1", checksum="different"))

        assert await ledger.has_open_incident("action_commit_conflict")


@pytest.mark.asyncio
async def test_illegal_transition_rolls_back(tmp_path: Path) -> None:
    """Catch a direct terminal transition before normal work is resumed or unblocked."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        run_id = await ledger.create_run(_run_seed())

        with pytest.raises(LedgerTransitionError, match="resume or unblock"):
            await ledger.set_run_status(run_id, RunStatus.COMPLETED)

        assert (await ledger.get_run(run_id)).status is RunStatus.RUNNING


@pytest.mark.asyncio
async def test_attempt_outcomes_follow_legal_action_transitions(tmp_path: Path) -> None:
    """Catch finishing an unauthorized action or skipping the running attempt state."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_authorized_action(ledger)

        finished = await ledger.finish_attempt(
            "a1", attempt=1, status=ActionStatus.REPAIR_REQUIRED, failure_signature="terms"
        )

        assert finished.status is ActionStatus.REPAIR_REQUIRED
        snapshot = await ledger.load_snapshot("run-1")
        assert snapshot.actions[0].status is ActionStatus.REPAIR_REQUIRED
        assert snapshot.failure_signatures == ("terms",)


@pytest.mark.asyncio
async def test_paused_run_rejects_new_plan(tmp_path: Path) -> None:
    """Catch replanning work entering a run that must first be explicitly resumed."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        run_id = await ledger.create_run(_run_seed())
        await ledger.set_run_status(run_id, RunStatus.PAUSED_BUDGET)

        with pytest.raises(LedgerTransitionError, match="resume the run"):
            await ledger.append_plan(
                run_id,
                PlanPatch(
                    objective="ingest source",
                    proposed_actions=(
                        ProposedAction(proposal_id="proposal-1", capability="source.ingest"),
                    ),
                    rationale="first durable plan",
                ),
            )
