"""Durable authorization, receipt, retry, gate, and repair handoffs."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from abi.project.run_ledger import (
    ArtifactCommit,
    LedgerConflictError,
    LedgerTransitionError,
    RunLedger,
    RunSeed,
    SuccessCommit,
)
from abi.types.orchestration import (
    ActionOutcomeEnvelope,
    ActionStatus,
    ArtifactBundle,
    ArtifactBundleEntry,
    AttemptOutcomeReceiptPayload,
    AuthorizedAction,
    ExpectedArtifact,
    ExpectedArtifactManifest,
    GateArtifactIdentity,
    GateDecision,
    GateEvidence,
    GateReceiptPayload,
    PlanPatch,
    ProposedAction,
    RepairRequired,
    RetryableFailure,
    RetryPolicySpec,
    RunStatus,
    Succeeded,
    canonical_bundle_json,
    canonical_manifest_json,
    canonical_model_json,
    sha256_canonical_json,
)


def _manifest(action_id: str = "a1") -> ExpectedArtifactManifest:
    return ExpectedArtifactManifest(
        action_id=action_id,
        entries=(
            ExpectedArtifact(
                canonical_relpath="reports/a.json",
                media_type="application/json",
                evidence_role="report",
            ),
            ExpectedArtifact(
                canonical_relpath="reports/b.json",
                media_type="application/json",
                evidence_role="report",
            ),
        ),
    )


def _authorized(action_id: str = "a1") -> AuthorizedAction:
    manifest = _manifest(action_id)
    policy = RetryPolicySpec(max_attempts=2, retryable_codes=("provider_timeout",))
    return AuthorizedAction(
        action_id=action_id,
        proposal_id="p1",
        plan_version=1,
        capability="report.build",
        parameters_json="{}",
        write_set=("reports",),
        idempotency_key=f"action:{action_id}",
        expected_artifact_manifest=manifest,
        expected_artifact_manifest_digest=sha256_canonical_json(
            canonical_manifest_json(manifest)
        ),
        expected_evidence_refs=("report",),
        retry_policy=policy,
        retry_policy_fingerprint=sha256_canonical_json(canonical_model_json(policy)),
    )


async def _seed(ledger: RunLedger) -> None:
    await ledger.create_run(RunSeed(run_id="run-1"))
    await ledger.append_plan(
        "run-1",
        PlanPatch(
            objective="build reports",
            proposed_actions=(ProposedAction(proposal_id="p1", capability="report.build"),),
            rationale="reports are absent",
        ),
    )
    await ledger.authorize_actions("run-1", (_authorized(),))


def _retry_receipt(error_code: str = "provider_timeout") -> AttemptOutcomeReceiptPayload:
    outcome = RetryableFailure(error_code=error_code, message="retry later")
    encoded = canonical_model_json(
        ActionOutcomeEnvelope(action_id="a1", attempt=1, outcome=outcome)
    )
    return AttemptOutcomeReceiptPayload(
        action_id="a1",
        attempt=1,
        canonical_outcome_json=encoded,
        outcome_digest=sha256_canonical_json(encoded),
        error_code=error_code,
    )


def _bundle() -> ArtifactBundle:
    return ArtifactBundle(
        action_id="a1",
        attempt=1,
        entries=tuple(
            ArtifactBundleEntry(
                staged_relpath=f"state/staging/a1/1/reports/{name}.json",
                canonical_relpath=f"reports/{name}.json",
                media_type="application/json",
                evidence_role="report",
            )
            for name in ("a", "b")
        ),
    )


def _success_receipt(bundle: ArtifactBundle) -> AttemptOutcomeReceiptPayload:
    outcome = Succeeded(artifact_bundle=bundle, evidence_refs=("report",))
    encoded = canonical_model_json(
        ActionOutcomeEnvelope(action_id="a1", attempt=1, outcome=outcome)
    )
    bundle_json = canonical_bundle_json(bundle)
    return AttemptOutcomeReceiptPayload(
        action_id="a1",
        attempt=1,
        canonical_outcome_json=encoded,
        outcome_digest=sha256_canonical_json(encoded),
        canonical_bundle_json=bundle_json,
        bundle_digest=sha256_canonical_json(bundle_json),
        evidence_refs=("report",),
    )


def _gate_receipt(bundle: ArtifactBundle) -> GateReceiptPayload:
    digest = sha256_canonical_json(canonical_bundle_json(bundle))
    checksums = ("a" * 64, "b" * 64)
    decision = GateDecision(
        passed=True,
        reason_code="evidence_valid",
        message="valid",
        validator_id="report.build",
        validator_version="1",
        bundle_digest=digest,
        artifact_checksums=checksums,
        evidence_refs=("report",),
    )
    encoded = canonical_model_json(decision)
    return GateReceiptPayload(
        action_id="a1",
        attempt=1,
        validator_id="report.build",
        validator_version="1",
        canonical_gate_decision_json=encoded,
        gate_decision_digest=sha256_canonical_json(encoded),
        bundle_digest=digest,
        artifacts=tuple(
            GateArtifactIdentity(
                staged_relpath=entry.staged_relpath,
                canonical_relpath=entry.canonical_relpath,
                checksum=checksum,
            )
            for entry, checksum in zip(bundle.entries, checksums, strict=True)
        ),
        evidence_refs=("report",),
    )


@pytest.mark.asyncio
async def test_attempt_snapshots_manifest_and_retry_policy_before_dispatch(tmp_path: Path) -> None:
    """Catch catalog drift changing effect or retry facts after executor dispatch."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed(ledger)
        attempt = await ledger.start_attempt("a1", attempt=1)
        assert attempt.expected_manifest_digest == _authorized().expected_artifact_manifest_digest
        assert attempt.retry_policy == _authorized().retry_policy
        assert attempt.retry_policy_fingerprint == _authorized().retry_policy_fingerprint
        assert attempt.staging_relpath == "state/staging/a1/1"


@pytest.mark.asyncio
async def test_outcome_receipt_is_immutable_before_separate_retry_route(
    tmp_path: Path,
) -> None:
    """Catch automatic retry re-entering attempt one or allocating duplicate successors."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed(ledger)
        await ledger.start_attempt("a1", attempt=1)
        receipt = _retry_receipt()
        assert await ledger.record_attempt_outcome(receipt) == await ledger.record_attempt_outcome(receipt)
        assert await ledger.attempt_status("a1", 1) is ActionStatus.RUNNING
        await ledger.route_retry_from_receipt("a1", attempt=1)
        assert await ledger.attempt_status("a1", 1) is ActionStatus.RETRY_WAIT
        first, second = await asyncio.gather(
            ledger.create_next_attempt("a1", previous_attempt=1),
            ledger.create_next_attempt("a1", previous_attempt=1),
        )
        assert first == second
        assert first.attempt == 2
        assert first.status is ActionStatus.AUTHORIZED
        assert first.retry_of_attempt == 1
        assert first.staging_relpath == "state/staging/a1/2"
        assert await ledger.attempt_numbers("a1") == (1, 2)


@pytest.mark.asyncio
async def test_denied_retry_never_rolls_back_durable_outcome_receipt(tmp_path: Path) -> None:
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed(ledger)
        await ledger.start_attempt("a1", attempt=1)
        payload = _retry_receipt("unregistered_error")
        await ledger.record_attempt_outcome(payload)

        with pytest.raises(LedgerTransitionError, match="frozen retry policy"):
            await ledger.route_retry_from_receipt("a1", attempt=1)

        assert (await ledger.get_attempt_outcome("a1", 1)).outcome_digest == payload.outcome_digest
        assert await ledger.attempt_status("a1", 1) is ActionStatus.RUNNING


@pytest.mark.asyncio
async def test_gate_receipt_and_complete_bundle_intents_are_one_idempotent_transaction(
    tmp_path: Path,
) -> None:
    """Catch canonical promotion becoming possible from a partial intent set."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed(ledger)
        await ledger.start_attempt("a1", attempt=1)
        bundle = _bundle()
        await ledger.record_attempt_outcome(_success_receipt(bundle))
        gate = _gate_receipt(bundle)
        first = await ledger.create_gate_receipt_and_bundle_intents(gate)
        second = await ledger.create_gate_receipt_and_bundle_intents(gate)
        assert first == second
        receipt, intents = first
        assert receipt.bundle_digest == gate.bundle_digest
        assert tuple(item.ordinal for item in intents) == (0, 1)
        assert tuple(item.canonical_relpath for item in intents) == (
            "reports/a.json",
            "reports/b.json",
        )
        assert {item.status for item in intents} == {"PENDING"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repair_class", "mapped", "expected_status"),
    (("semantic", True, RunStatus.RUNNING), ("integrity", False, RunStatus.BLOCKED)),
)
async def test_repair_fact_is_receipt_bound_and_never_creates_retry_attempt(
    tmp_path: Path, repair_class: str, mapped: bool, expected_status: RunStatus
) -> None:
    """Catch semantic/integrity repair collapsing into one retry or replan route."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed(ledger)
        await ledger.start_attempt("a1", attempt=1)
        outcome = RepairRequired(
            repair_class=repair_class,
            repair_source="action_outcome" if repair_class == "semantic" else "integrity_guard",
            reason_code="term_drift" if repair_class == "semantic" else "artifact_bundle_conflict",
            defect_codes=("term_drift",) if repair_class == "semantic" else ("artifact_bundle_conflict",),
            message="preserve and repair",
        )
        encoded = canonical_model_json(
            ActionOutcomeEnvelope(action_id="a1", attempt=1, outcome=outcome)
        )
        await ledger.record_attempt_outcome(
            AttemptOutcomeReceiptPayload(
                action_id="a1",
                attempt=1,
                canonical_outcome_json=encoded,
                outcome_digest=sha256_canonical_json(encoded),
            )
        )
        fact = await ledger.record_repair_required(
            action_id="a1",
            attempt=1,
            repair_class=repair_class,
            repair_source=outcome.repair_source,
            reason_code=outcome.reason_code,
            defect_codes=outcome.defect_codes,
            evidence_refs=(),
            message=outcome.message,
            semantic_reason_mapped=mapped,
        )
        assert fact.repair_class == repair_class
        assert (await ledger.get_run("run-1")).status is expected_status
        assert await ledger.attempt_status("a1", 1) is ActionStatus.REPAIR_REQUIRED
        assert await ledger.attempt_numbers("a1") == (1,)


@pytest.mark.asyncio
async def test_unknown_repair_source_fails_closed_as_integrity(
    tmp_path: Path,
) -> None:
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed(ledger)
        await ledger.start_attempt("a1", attempt=1)
        outcome = RepairRequired(
            repair_class="semantic",
            repair_source="action_outcome",
            reason_code="term_drift",
            defect_codes=("term_drift",),
            message="preserve and repair",
        )
        encoded = canonical_model_json(
            ActionOutcomeEnvelope(action_id="a1", attempt=1, outcome=outcome)
        )
        await ledger.record_attempt_outcome(
            AttemptOutcomeReceiptPayload(
                action_id="a1",
                attempt=1,
                canonical_outcome_json=encoded,
                outcome_digest=sha256_canonical_json(encoded),
            )
        )

        fact = await ledger.record_repair_required(
            action_id="a1",
            attempt=1,
            repair_class="semantic",
            repair_source="invented_source",  # type: ignore[arg-type]
            reason_code="term_drift",
            defect_codes=("term_drift",),
            evidence_refs=(),
            message="preserve and repair",
            semantic_reason_mapped=True,
        )

        assert fact.repair_class == "integrity"
        assert fact.repair_source == "integrity_guard"
        assert fact.reason_code == "repair_class_unknown"
        assert (await ledger.get_run("run-1")).status is RunStatus.BLOCKED


@pytest.mark.asyncio
async def test_commit_success_rejects_direct_ledger_bypass_without_receipts_or_intents(
    tmp_path: Path,
) -> None:
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed(ledger)
        await ledger.start_attempt("a1", attempt=1)
        commit = SuccessCommit(
            action_id="a1",
            attempt=1,
            artifacts=tuple(
                ArtifactCommit(
                    artifact_id=f"artifact-{name}",
                    relpath=f"reports/{name}.json",
                    sha256=checksum,
                    producer_action_id="a1",
                    media_type="application/json",
                )
                for name, checksum in (("a", "a" * 64), ("b", "b" * 64))
            ),
            gate_evidence=(
                GateEvidence(
                    evidence_id="gate-a1",
                    gate="report.build",
                    passed=True,
                    validator_version="1",
                    artifact_checksums=("a" * 64, "b" * 64),
                ),
            ),
        )

        with pytest.raises(LedgerTransitionError, match=r"receipt|intent|bundle"):
            await ledger.commit_success(commit)

        assert await ledger.attempt_status("a1", 1) is ActionStatus.RUNNING


@pytest.mark.asyncio
async def test_gate_replay_compares_full_ordered_intent_identity(tmp_path: Path) -> None:
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed(ledger)
        await ledger.start_attempt("a1", attempt=1)
        bundle = _bundle()
        await ledger.record_attempt_outcome(_success_receipt(bundle))
        gate = _gate_receipt(bundle)
        _, intents = await ledger.create_gate_receipt_and_bundle_intents(gate)
        await ledger._db.execute(
            "UPDATE promotion_intents SET checksum = ? WHERE intent_id = ?",
            ("f" * 64, intents[0].intent_id),
        )
        await ledger._db.commit()

        with pytest.raises(LedgerConflictError, match=r"intent|conflict"):
            await ledger.create_gate_receipt_and_bundle_intents(gate)


@pytest.mark.asyncio
async def test_finish_attempt_requires_receipt_and_repair_facts(tmp_path: Path) -> None:
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed(ledger)
        await ledger.start_attempt("a1", attempt=1)
        with pytest.raises(LedgerTransitionError, match=r"receipt|repair"):
            await ledger.finish_attempt(
                "a1", attempt=1, status=ActionStatus.REPAIR_REQUIRED
            )
