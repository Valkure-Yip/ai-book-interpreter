"""Behavioral tests for compressed planning context and structured planning."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from abi.actions.contracts import ActionDefinition, ActionExecutionContext
from abi.actions.evidence import StagingEvidenceView
from abi.actions.predicates import PredicateCatalog
from abi.actions.registry import ActionRegistry
from abi.planning.context import SnapshotBuilder
from abi.planning.planner import Planner
from abi.planning.policy import PolicyEngine
from abi.project.run_ledger import ArtifactCommit, RunLedger, RunSeed, SuccessCommit
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionKind,
    ActionOutcomeEnvelope,
    ActionSpec,
    ArtifactBundle,
    ArtifactBundleEntry,
    ArtifactRef,
    AttemptOutcomeReceiptPayload,
    ExpectedArtifact,
    ExpectedArtifactManifest,
    GateArtifactIdentity,
    GateDecision,
    GateEvidence,
    GateReceiptPayload,
    PlanningContext,
    PlanPatch,
    PlanRejectionView,
    ProposedAction,
    RetryPolicySpec,
    RunSnapshot,
    RunStatus,
    Succeeded,
    canonical_bundle_json,
    canonical_manifest_json,
    canonical_model_json,
    sha256_canonical_json,
)


class EmptyInput(FrozenModel):
    pass


async def _execute_unused(
    context: ActionExecutionContext, parameters: FrozenModel
) -> ActionOutcomeEnvelope:
    raise AssertionError("snapshot and planner tests must not execute actions")


def _validate_unused(
    evidence_view: StagingEvidenceView,
    parameters: FrozenModel,
    bundle: ArtifactBundle,
) -> GateDecision:
    return GateDecision(
        passed=True,
        reason_code="ok",
        message="valid",
        validator_id="source_manifest",
        validator_version="1",
        bundle_digest=evidence_view.bundle_digest,
        artifact_checksums=evidence_view.artifact_checksums,
    )


def _expand_unused(
    capability: str, action_id: str, parameters: FrozenModel
) -> ExpectedArtifactManifest:
    return ExpectedArtifactManifest(
        action_id=action_id,
        entries=(
            ExpectedArtifact(
                canonical_relpath="source/manifest.json",
                media_type="application/json",
                evidence_role="source_manifest",
            ),
        ),
    )


def _registry() -> ActionRegistry:
    registry = ActionRegistry(
        predicates=PredicateCatalog({"always": lambda snapshot, arguments: True}),
        validators={"source_manifest": _validate_unused},
    )
    registry.register(
        ActionDefinition(
            spec=ActionSpec(
                capability="source.ingest",
                description="Create immutable source-manifest evidence.",
                input_schema="EmptyInput",
                action_kind=ActionKind.DETERMINISTIC,
                validator="source_manifest",
            ),
            input_model=EmptyInput,
            executor=_execute_unused,
            validator=_validate_unused,
            effect_expander=_expand_unused,
        )
    )
    return registry


def _artifact_gated_registry() -> ActionRegistry:
    registry = ActionRegistry(
        predicates=PredicateCatalog(
            {"source_exists": lambda snapshot, arguments: bool(snapshot.artifacts)}
        ),
        validators={"source_manifest": _validate_unused},
    )
    registry.register(
        ActionDefinition(
            spec=ActionSpec(
                capability="source.consume",
                description="Consume immutable source-manifest evidence.",
                input_schema="EmptyInput",
                action_kind=ActionKind.DETERMINISTIC,
                prerequisites=({"name": "source_exists"},),
                validator="source_manifest",
            ),
            input_model=EmptyInput,
            executor=_execute_unused,
            validator=_validate_unused,
            effect_expander=_expand_unused,
        )
    )
    return registry


async def _seed_committed_artifacts(ledger: RunLedger, body: str) -> None:
    await ledger.create_run(RunSeed(run_id="run-1", budget_usd=5.0))
    await ledger.append_plan(
        "run-1",
        PlanPatch(
            objective="ingest source",
            proposed_actions=(ProposedAction(proposal_id="ingest", capability="source.ingest"),),
            rationale="seed committed source evidence",
        ),
    )
    from abi.types.orchestration import AuthorizedAction

    manifest = ExpectedArtifactManifest(
        action_id="ingest-1",
        entries=(
            ExpectedArtifact(
                canonical_relpath="source/manifest.json",
                media_type="application/json",
                evidence_role="source_manifest",
            ),
            ExpectedArtifact(
                canonical_relpath="source/raw.txt",
                media_type="text/plain",
                evidence_role="source_body",
            ),
        ),
    )
    retry = RetryPolicySpec(max_attempts=1)

    await ledger.authorize_actions(
        "run-1",
        (
            AuthorizedAction(
                action_id="ingest-1",
                proposal_id="ingest",
                plan_version=1,
                capability="source.ingest",
                parameters_json="{}",
                idempotency_key="ingest-1",
                expected_artifact_manifest=manifest,
                expected_artifact_manifest_digest=sha256_canonical_json(
                    canonical_manifest_json(manifest)
                ),
                retry_policy=retry,
                retry_policy_fingerprint=sha256_canonical_json(
                    canonical_model_json(retry)
                ),
            ),
        ),
    )
    await ledger.start_attempt("ingest-1")
    primary_checksum = sha256(body.encode("utf-8")).hexdigest()
    manifest_checksum = sha256(b"manifest").hexdigest()
    bundle = ArtifactBundle(
        action_id="ingest-1",
        attempt=1,
        entries=tuple(
            ArtifactBundleEntry(
                staged_relpath=f"state/staging/ingest-1/1/{entry.canonical_relpath}",
                canonical_relpath=entry.canonical_relpath,
                media_type=entry.media_type,
                evidence_role=entry.evidence_role,
            )
            for entry in manifest.entries
        ),
    )
    outcome = Succeeded(artifact_bundle=bundle)
    outcome_json = canonical_model_json(
        ActionOutcomeEnvelope(action_id="ingest-1", attempt=1, outcome=outcome)
    )
    bundle_json = canonical_bundle_json(bundle)
    bundle_digest = sha256_canonical_json(bundle_json)
    await ledger.record_attempt_outcome(
        AttemptOutcomeReceiptPayload(
            action_id="ingest-1",
            attempt=1,
            canonical_outcome_json=outcome_json,
            outcome_digest=sha256_canonical_json(outcome_json),
            canonical_bundle_json=bundle_json,
            bundle_digest=bundle_digest,
        )
    )
    checksums = (manifest_checksum, primary_checksum)
    decision = GateDecision(
        passed=True,
        reason_code="evidence_valid",
        message="valid",
        validator_id="source_manifest",
        validator_version="1",
        bundle_digest=bundle_digest,
        artifact_checksums=checksums,
    )
    decision_json = canonical_model_json(decision)
    _, intents = await ledger.create_gate_receipt_and_bundle_intents(
        GateReceiptPayload(
            action_id="ingest-1",
            attempt=1,
            validator_id="source_manifest",
            validator_version="1",
            canonical_gate_decision_json=decision_json,
            gate_decision_digest=sha256_canonical_json(decision_json),
            bundle_digest=bundle_digest,
            artifacts=tuple(
                GateArtifactIdentity(
                    staged_relpath=entry.staged_relpath,
                    canonical_relpath=entry.canonical_relpath,
                    checksum=checksum,
                )
                for entry, checksum in zip(bundle.entries, checksums, strict=True)
            ),
        )
    )
    for intent in intents:
        await ledger.commit_promotion_intent(intent.intent_id)
    await ledger.commit_success(
        SuccessCommit(
            action_id="ingest-1",
            attempt=1,
            artifacts=(
                ArtifactCommit(
                    artifact_id="source-manifest",
                    relpath="source/manifest.json",
                    sha256=manifest_checksum,
                    producer_action_id="ingest-1",
                    media_type="application/json",
                ),
                ArtifactCommit(
                    artifact_id="source-body",
                    relpath="source/raw.txt",
                    sha256=primary_checksum,
                    producer_action_id="ingest-1",
                    media_type="text/plain",
                ),
            ),
            gate_evidence=(
                GateEvidence(
                    evidence_id="source-gate",
                    gate="source_manifest",
                    passed=True,
                    validator_version="1",
                    artifact_checksums=checksums,
                ),
            ),
            cost_usd=1.25,
        )
    )
    await ledger.record_incident(
        "run-1", error_code="source_review", message="x" * 501, action_id="ingest-1"
    )
    await ledger.record_plan_rejection("run-1", plan_version=1, reason_codes=("invalid_horizon",))


@pytest.mark.asyncio
async def test_snapshot_contains_hashes_not_book_body_and_applies_limits(tmp_path: Path) -> None:
    """Catch a planner context that reads source text or sends unbounded metadata."""
    body = "SECRET BOOK BODY"
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_committed_artifacts(ledger, body)
        await ledger.create_run(RunSeed(run_id="run-2", budget_usd=5.0))
        await ledger.append_plan(
            "run-2",
            PlanPatch(
                objective="other run",
                proposed_actions=(ProposedAction(proposal_id="other", capability="source.ingest"),),
                rationale="run isolation fixture",
            ),
        )
        await ledger.record_plan_rejection(
            "run-2", plan_version=1, reason_codes=("other_run_only",)
        )

        builder = SnapshotBuilder(
            ledger=ledger,
            registry=_registry(),
            artifact_limit=1,
            incident_limit=1,
            rejection_limit=1,
            action_limit=0,
            gate_evidence_limit=0,
        )
        context = await builder.build("run-1")
        repeated = await builder.build("run-1")

    snapshot = context.planner_snapshot
    payload = snapshot.model_dump_json()
    assert context == repeated
    assert body not in payload
    assert snapshot.artifacts == (
        snapshot.artifacts[0].model_copy(
            update={"sha256": sha256(body.encode("utf-8")).hexdigest()}
        ),
    )
    assert len(snapshot.incidents) == 1
    assert snapshot.incidents[0].message == "x" * 500
    assert snapshot.remaining_budget_usd == 3.75
    assert snapshot.eligible_actions == ()
    assert snapshot.plan_rejections[0].reason_codes == ("invalid_horizon",)
    assert "other_run_only" not in payload
    assert snapshot.actions == ()
    assert snapshot.gate_evidence == ()
    assert len(context.policy_snapshot.actions) == 1
    assert len(context.policy_snapshot.gate_evidence) == 1


@pytest.mark.asyncio
async def test_context_keeps_full_policy_facts_while_planner_view_is_bounded(tmp_path: Path) -> None:
    """Catch sampled planner evidence becoming the policy authorization input."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await _seed_committed_artifacts(ledger, "SECRET BOOK BODY")
        registry = _artifact_gated_registry()
        builder = SnapshotBuilder(ledger=ledger, registry=registry, artifact_limit=0)

        context = await builder.build("run-1")
        repeated = await builder.build("run-1")

    patch = PlanPatch(
        objective="use source evidence",
        proposed_actions=(ProposedAction(proposal_id="consume", capability="source.consume"),),
        rationale="source artifact is a durable policy fact",
    )
    policy = PolicyEngine(registry)

    assert context == repeated
    assert len(context.policy_snapshot.artifacts) == 2
    assert context.planner_snapshot.artifacts == ()
    assert context.planner_snapshot.eligible_actions[0].capability == "source.consume"
    assert policy.authorize(context.policy_snapshot, patch, next_plan_version=2).authorized is True
    assert policy.authorize(context.planner_snapshot, patch, next_plan_version=2).reason_codes == (
        "hard_prerequisite_failed",
    )


class DeterministicPlannerProvider:
    """A structured provider double exposing its last boundary request."""

    def __init__(self, result: PlanPatch) -> None:
        self.result = result
        self.request: tuple[type[Any], list[Any], dict[str, Any]] | None = None

    async def invoke_structured(
        self, schema: type[Any], messages: list[Any], **kwargs: Any
    ) -> tuple[Any, None]:
        self.request = (schema, messages, kwargs)
        return self.result, None


def _context_with_ingest_eligible() -> PlanningContext:
    snapshot = RunSnapshot(
        run_id="run-1",
        status=RunStatus.RUNNING,
        eligible_actions=_registry().eligible(
            RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
        ),
        plan_rejections=(PlanRejectionView(plan_version=1, reason_codes=("invalid_horizon",)),),
    )
    return PlanningContext(
        policy_snapshot=snapshot.model_copy(
            update={
                "artifacts": (
                    ArtifactRef(
                        artifact_id="policy-only",
                        relpath="state/private-policy-evidence.json",
                        sha256="policy-only-checksum",
                        producer_action_id="policy-only",
                    ),
                )
            }
        ),
        planner_snapshot=snapshot,
    )


@pytest.mark.asyncio
async def test_planner_returns_valid_patch_from_structured_provider_boundary() -> None:
    """Catch planner calls that omit constrained context or bypass structured output."""
    router = DeterministicPlannerProvider(
        PlanPatch(
            objective="produce missing source evidence",
            proposed_actions=(
                ProposedAction(
                    proposal_id="ingest",
                    capability="source.ingest",
                    expected_evidence=("source_manifest",),
                ),
            ),
            rationale="the source manifest is missing",
        )
    )

    patch = await Planner(router=router).plan(_context_with_ingest_eligible())

    assert patch.proposed_actions[0].capability == "source.ingest"
    assert patch.objective == "produce missing source evidence"
    assert patch.proposed_actions[0].expected_evidence == ("source_manifest",)
    assert router.request is not None
    schema, messages, kwargs = router.request
    assert schema is PlanPatch
    assert "one to five actions" in str(messages[0].content)
    assert "prior rejection reasons as hard feedback" in str(messages[0].content)
    assert "repairs_reason_codes" in str(messages[0].content)
    assert "do not add upstream or downstream actions" in str(messages[0].content)
    assert '"eligible_actions"' in str(messages[1].content)
    assert '"invalid_horizon"' in str(messages[1].content)
    assert "private-policy-evidence" not in str(messages[1].content)
    assert kwargs == {
        "agent_name": "orchestration.planner",
        "prompt_version": "dynamic-plan-v1",
        "metadata": {"logical_invocation_id": "planner:run-1:plan:1"},
        "max_retries": 2,
    }


@pytest.mark.asyncio
async def test_planner_rejects_provider_patch_beyond_five_actions() -> None:
    """Catch a planner horizon expanding beyond the policy-safe five actions."""
    router = DeterministicPlannerProvider(
        PlanPatch(
            objective="too many actions",
            proposed_actions=tuple(
                ProposedAction(proposal_id=f"proposal-{index}", capability="source.ingest")
                for index in range(6)
            ),
            rationale="invalid horizon",
        )
    )

    with pytest.raises(ValueError, match="at most 5 actions"):
        await Planner(router=router).plan(_context_with_ingest_eligible())
