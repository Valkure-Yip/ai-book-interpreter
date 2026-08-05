"""Integration tests for the SQLite business ledger."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from abi.project.run_ledger import (
    ArtifactCommit,
    LedgerConflictError,
    LedgerError,
    LedgerTransitionError,
    RunLedger,
    RunSeed,
    SuccessCommit,
)
from abi.types.orchestration import (
    ActionStatus,
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


async def _seed_promotion_intent(ledger: RunLedger) -> str:
    await _seed_authorized_action(ledger)
    intent = await ledger.create_promotion_intent(
        action_id="a1",
        attempt=1,
        staged_relpath="state/staging/a1/1/source.json",
        canonical_relpath="source/a1.json",
        checksum="abc",
        media_type="application/json",
    )
    return intent.intent_id


def _success_commit(
    action_id: str,
    checksum: str,
    *,
    cost_usd: float = 0.25,
    evidence: GateEvidence | None = None,
) -> SuccessCommit:
    return SuccessCommit(
        action_id=action_id,
        attempt=1,
        artifacts=(
            ArtifactCommit(
                artifact_id=f"artifact-{action_id}",
                relpath=f"source/{action_id}.json",
                sha256=checksum,
                producer_action_id=action_id,
                media_type="application/json",
            ),
        ),
        gate_evidence=(
            evidence
            or GateEvidence(
                evidence_id=f"gate-{action_id}",
                gate="source_manifest",
                passed=True,
                validator_version="1",
                artifact_checksums=(checksum,),
            ),
        ),
        cost_usd=cost_usd,
    )


@pytest.mark.asyncio
async def test_commit_success_is_exactly_once(tmp_path: Path) -> None:
    """Catch duplicate durable commits that create duplicate facts or events."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_authorized_action(ledger)

        first = await ledger.commit_success(_success_commit("a1", checksum="abc"))
        second = await ledger.commit_success(_success_commit("a1", checksum="abc"))

        assert first == second
        assert first.artifacts[0].media_type == "application/json"
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
@pytest.mark.parametrize(
    ("commit", "repair"),
    [
        (
            SuccessCommit(action_id="a1", attempt=1),
            "supply at least one committed artifact",
        ),
        (
            SuccessCommit(
                action_id="a1",
                attempt=1,
                artifacts=(
                    ArtifactCommit(
                        artifact_id="artifact-a1",
                        relpath="source/a1.json",
                        sha256="abc",
                        producer_action_id="a1",
                        media_type="application/json",
                    ),
                ),
            ),
            "supply at least one passing gate evidence record",
        ),
        (
            _success_commit(
                "a1",
                "abc",
                evidence=GateEvidence(
                    evidence_id="gate-a1",
                    gate="source_manifest",
                    passed=False,
                    validator_version="1",
                    artifact_checksums=("abc",),
                ),
            ),
            "repair the failed gate",
        ),
        (
            _success_commit(
                "a1",
                "abc",
                evidence=GateEvidence(
                    evidence_id="gate-a1",
                    gate="source_manifest",
                    passed=True,
                    validator_version="1",
                    artifact_checksums=("different",),
                ),
            ),
            "re-run the validator",
        ),
    ],
)
async def test_invalid_success_fact_set_rolls_back(
    tmp_path: Path, commit: SuccessCommit, repair: str
) -> None:
    """Catch success commits that lack deterministic proof or mutate before validation."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_authorized_action(ledger)

        with pytest.raises(LedgerTransitionError, match=repair):
            await ledger.commit_success(commit)

        assert await ledger.count_committed_actions("a1") == 0
        assert await ledger.count_artifacts_for("a1") == 0
        assert (await ledger.get_action("a1")).status is ActionStatus.RUNNING


@pytest.mark.asyncio
async def test_replay_with_different_cost_creates_conflict_incident(tmp_path: Path) -> None:
    """Catch replays that alter a committed fact while retaining artifact checksums."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_authorized_action(ledger)
        await ledger.commit_success(_success_commit("a1", "abc", cost_usd=0.25))

        with pytest.raises(LedgerConflictError, match="inspect the canonical artifact"):
            await ledger.commit_success(_success_commit("a1", "abc", cost_usd=0.5))

        assert await ledger.has_open_incident("action_commit_conflict")


def _replay_variant(commit: SuccessCommit, field: str) -> SuccessCommit:
    artifact = commit.artifacts[0]
    evidence = commit.gate_evidence[0]
    if field == "attempt":
        return commit.model_copy(update={"attempt": 2})
    if field == "artifact_id":
        return commit.model_copy(
            update={"artifacts": (artifact.model_copy(update={"artifact_id": "artifact-other"}),)}
        )
    if field == "media_type":
        return commit.model_copy(
            update={"artifacts": (artifact.model_copy(update={"media_type": "text/plain"}),)}
        )
    if field == "evidence_id":
        return commit.model_copy(
            update={"gate_evidence": (evidence.model_copy(update={"evidence_id": "gate-other"}),)}
        )
    if field == "gate":
        return commit.model_copy(
            update={"gate_evidence": (evidence.model_copy(update={"gate": "different_gate"}),)}
        )
    if field == "validator_version":
        return commit.model_copy(
            update={"gate_evidence": (evidence.model_copy(update={"validator_version": "2"}),)}
        )
    if field == "evidence_checksum_set":
        return commit.model_copy(
            update={
                "gate_evidence": (
                    evidence.model_copy(update={"artifact_checksums": ("abc", "different")}),
                )
            }
        )
    if field == "cost":
        return commit.model_copy(update={"cost_usd": 0.5})
    raise AssertionError(f"unknown replay variant {field}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    (
        "attempt",
        "artifact_id",
        "media_type",
        "evidence_id",
        "gate",
        "validator_version",
        "evidence_checksum_set",
        "cost",
    ),
)
async def test_replay_difference_creates_conflict_incident(
    tmp_path: Path, field: str
) -> None:
    """Catch replays that alter any canonical success fact behind a stable action key."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_authorized_action(ledger)
        commit = _success_commit("a1", "abc")
        await ledger.commit_success(commit)

        with pytest.raises(LedgerConflictError, match="inspect the canonical artifact"):
            await ledger.commit_success(_replay_variant(commit, field))

        snapshot = await ledger.load_snapshot("run-1")
        assert [incident.error_code for incident in snapshot.incidents] == [
            "action_commit_conflict"
        ]
        assert await ledger.count_outbox_events("action.committed", "a1") == 1


@pytest.mark.asyncio
async def test_canonical_artifact_collision_rolls_back_mid_transaction(tmp_path: Path) -> None:
    """Catch an artifact constraint failure that leaves partial action success facts behind."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_authorized_action(ledger)
        await ledger.authorize_actions("run-1", (_action("a2"),))
        await ledger.start_attempt("a2")
        await ledger.commit_success(_success_commit("a1", "abc"))
        second = _success_commit("a2", "different").model_copy(
            update={
                "artifacts": (
                    ArtifactCommit(
                        artifact_id="artifact-a2",
                        relpath="source/a1.json",
                        sha256="different",
                        producer_action_id="a2",
                        media_type="application/json",
                    ),
                ),
                "gate_evidence": (
                    GateEvidence(
                        evidence_id="gate-a2",
                        gate="source_manifest",
                        passed=True,
                        validator_version="1",
                        artifact_checksums=("different",),
                    ),
                ),
            }
        )

        with pytest.raises(LedgerConflictError, match="choose the canonical artifact"):
            await ledger.commit_success(second)

        snapshot = await ledger.load_snapshot("run-1")
        assert [action.status for action in snapshot.actions if action.action_id == "a2"] == [
            ActionStatus.RUNNING
        ]
        attempt = await ledger.get_attempt("a2", 1)
        assert attempt.status is ActionStatus.RUNNING
        assert attempt.finished_at is None
        assert await ledger.count_artifacts_for("a2") == 0
        assert await ledger.count_outbox_events("action.committed", "a2") == 0
        assert snapshot.remaining_budget_usd == 4.75
        assert [evidence.evidence_id for evidence in snapshot.gate_evidence] == ["gate-a1"]


@pytest.mark.asyncio
async def test_concurrent_identical_commits_serialize_without_duplicate_facts(tmp_path: Path) -> None:
    """Catch shared-connection transactions that interleave and issue nested BEGIN calls."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_authorized_action(ledger)
        commit = _success_commit("a1", "abc")

        first, second = await asyncio.gather(
            ledger.commit_success(commit), ledger.commit_success(commit)
        )

        assert first == second
        assert await ledger.count_committed_actions("a1") == 1
        assert await ledger.count_outbox_events("action.committed", "a1") == 1


@pytest.mark.asyncio
async def test_unknown_persisted_run_status_has_repair_error(tmp_path: Path) -> None:
    """Catch corrupt enum values escaping as raw parsing exceptions."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        run_id = await ledger.create_run(_run_seed())
        await ledger._db.execute("UPDATE runs SET status = 'BROKEN' WHERE run_id = ?", (run_id,))
        await ledger._db.commit()

        with pytest.raises(LedgerError, match=r"runs.status.*BROKEN.*repair or recreate"):
            await ledger.get_run(run_id)


@pytest.mark.asyncio
async def test_unknown_persisted_action_status_has_repair_error(tmp_path: Path) -> None:
    """Catch corrupt action enums escaping as raw parsing exceptions."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_authorized_action(ledger)
        await ledger._db.execute("UPDATE actions SET status = 'BROKEN' WHERE action_id = 'a1'")
        await ledger._db.commit()

        with pytest.raises(LedgerError, match=r"actions.status.*BROKEN.*repair or recreate"):
            await ledger.get_action("a1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "canonical_relpath",
    (
        "STATE/STAGING/escape.json",
        "CHAPTERS/FINAL/001.md",
        "state/staging/escape.json",
        "\u017ftate/staging/escape.json",
        "state/\u017ftaging/escape.json",
        "chapters/final/\u212a.json",
        "chapters/./final.json",
        "chapters/../final.json",
        "chapters//final.json",
        "chapters/final/",
        r"chapters\final\001.md",
        "/chapters/final/001.md",
        "c:/chapters/final/001.md",
    ),
    ids=(
        "uppercase-staging",
        "uppercase-canonical",
        "staging-prefix",
        "unicode-casefold-state",
        "unicode-casefold-staging",
        "unicode-casefold-leaf",
        "dot-component",
        "dot-dot-component",
        "empty-component",
        "trailing-empty-component",
        "backslash",
        "posix-absolute",
        "drive-absolute",
    ),
)
async def test_ledger_rejects_nonportable_canonical_reservation_key_without_mutation(
    tmp_path: Path, canonical_relpath: str
) -> None:
    """Catch platform aliases or unsafe syntax entering the canonical reservation index."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_authorized_action(ledger)

        with pytest.raises(ValueError):
            await ledger.create_promotion_intent(
                action_id="a1",
                attempt=1,
                staged_relpath="state/staging/a1/1/source.json",
                canonical_relpath=canonical_relpath,
                checksum="abc",
                media_type="application/json",
            )

        assert await ledger.promotion_intents() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "canonical_relpath",
    (
        "chapters/final/001.md",
        "qa/gates/chapter_001-final.v1.json",
        ".abi/meta-1.json",
    ),
)
async def test_portable_lowercase_canonical_keys_are_reserved_verbatim_and_uniquely(
    tmp_path: Path, canonical_relpath: str
) -> None:
    """Catch a validated canonical key being normalized or reserved by two actions."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_authorized_action(ledger)
        first = await ledger.create_promotion_intent(
            action_id="a1",
            attempt=1,
            staged_relpath="state/staging/a1/1/source.json",
            canonical_relpath=canonical_relpath,
            checksum="abc",
            media_type="application/json",
        )
        replay = await ledger.create_promotion_intent(
            action_id="a1",
            attempt=1,
            staged_relpath="state/staging/a1/1/source.json",
            canonical_relpath=canonical_relpath,
            checksum="abc",
            media_type="application/json",
        )
        await ledger.authorize_actions("run-1", (_action("a2"),))
        await ledger.start_attempt("a2")

        with pytest.raises(LedgerConflictError, match="choose the canonical artifact"):
            await ledger.create_promotion_intent(
                action_id="a2",
                attempt=1,
                staged_relpath="state/staging/a2/1/source.json",
                canonical_relpath=canonical_relpath,
                checksum="different",
                media_type="application/json",
            )

        assert replay.intent_id == first.intent_id
        assert first.canonical_relpath == canonical_relpath
        assert await ledger.promotion_intents() == (first,)


@pytest.mark.asyncio
async def test_conflict_is_a_valid_persisted_promotion_status(tmp_path: Path) -> None:
    """Catch valid compensated intents being rejected as corrupt ledger rows."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        intent_id = await _seed_promotion_intent(ledger)
        await ledger._db.execute(
            "UPDATE promotion_intents SET status = 'CONFLICT' WHERE intent_id = ?", (intent_id,)
        )
        await ledger._db.commit()

        intent = await ledger.get_promotion_intent(intent_id)

        assert intent.status == "CONFLICT"
        assert await ledger.promotion_state(intent_id) == "CONFLICT"


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_first", [False, True])
async def test_promotion_conflict_compensation_is_atomic_and_idempotent(
    tmp_path: Path, commit_first: bool
) -> None:
    """Catch compensation that leaves a pending/successful intent or duplicates its incident."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        intent_id = await _seed_promotion_intent(ledger)
        if commit_first:
            await ledger.commit_promotion_intent(intent_id)

        first = await ledger.conflict_promotion_intent(
            intent_id,
            error_code="artifact_checksum_conflict",
            message="canonical bytes drifted; inspect and retain both artifacts",
        )
        second = await ledger.conflict_promotion_intent(
            intent_id,
            error_code="artifact_checksum_conflict",
            message="canonical bytes drifted; inspect and retain both artifacts",
        )

        assert first == second
        assert first.status == "CONFLICT"
        assert await ledger.promotion_state(intent_id) == "CONFLICT"
        incidents = [
            incident
            for incident in (await ledger.load_snapshot("run-1")).incidents
            if incident.error_code == "artifact_checksum_conflict"
        ]
        assert len(incidents) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("commit_first", "expected_status"), [(False, "PENDING"), (True, "COMMITTED")]
)
async def test_promotion_conflict_compensation_rolls_back_if_incident_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit_first: bool,
    expected_status: str,
) -> None:
    """Catch the status update escaping its transaction when incident persistence fails."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        intent_id = await _seed_promotion_intent(ledger)
        if commit_first:
            await ledger.commit_promotion_intent(intent_id)

        async def fail_incident(*args: object, **kwargs: object) -> None:
            raise RuntimeError("injected incident failure")

        monkeypatch.setattr(ledger, "_insert_incident", fail_incident)

        with pytest.raises(RuntimeError, match="injected incident failure"):
            await ledger.conflict_promotion_intent(
                intent_id,
                error_code="artifact_checksum_conflict",
                message="canonical bytes drifted; inspect and retain both artifacts",
            )

        assert await ledger.promotion_state(intent_id) == expected_status
        assert not await ledger.has_open_incident("artifact_checksum_conflict")


@pytest.mark.asyncio
async def test_conflicted_promotion_cannot_transition_back_to_committed(tmp_path: Path) -> None:
    """Catch a compensated promotion being reported as successful on a later retry."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        intent_id = await _seed_promotion_intent(ledger)
        await ledger.conflict_promotion_intent(
            intent_id,
            error_code="artifact_checksum_conflict",
            message="canonical bytes drifted; inspect and retain both artifacts",
        )

        with pytest.raises(LedgerTransitionError, match=r"CONFLICT.*repair"):
            await ledger.commit_promotion_intent(intent_id)

        assert await ledger.promotion_state(intent_id) == "CONFLICT"


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
