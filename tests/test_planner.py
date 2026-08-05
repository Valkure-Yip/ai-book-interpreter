"""Behavioral tests for compressed planning context and structured planning."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from abi.actions.contracts import ActionDefinition, ActionExecutionContext
from abi.actions.predicates import PredicateCatalog
from abi.actions.registry import ActionRegistry
from abi.planning.context import SnapshotBuilder
from abi.planning.planner import Planner
from abi.project.run_ledger import ArtifactCommit, RunLedger, RunSeed, SuccessCommit
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionKind,
    ActionOutcomeEnvelope,
    ActionSpec,
    GateDecision,
    GateEvidence,
    PlanPatch,
    PlanRejectionView,
    ProposedAction,
    RunSnapshot,
    RunStatus,
)


class EmptyInput(FrozenModel):
    pass


async def _execute_unused(
    context: ActionExecutionContext, parameters: FrozenModel
) -> ActionOutcomeEnvelope:
    raise AssertionError("snapshot and planner tests must not execute actions")


def _validate_unused(project: object, parameters: FrozenModel) -> GateDecision:
    return GateDecision(passed=True, reason_code="ok", message="valid")


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
            ),
        ),
    )
    await ledger.start_attempt("ingest-1")
    primary_checksum = sha256(body.encode("utf-8")).hexdigest()
    await ledger.commit_success(
        SuccessCommit(
            action_id="ingest-1",
            attempt=1,
            artifacts=(
                ArtifactCommit(
                    artifact_id="source-body",
                    relpath="source/raw.txt",
                    sha256=primary_checksum,
                    producer_action_id="ingest-1",
                    media_type="text/plain",
                ),
                ArtifactCommit(
                    artifact_id="source-manifest",
                    relpath="source/manifest.json",
                    sha256="manifest-checksum",
                    producer_action_id="ingest-1",
                    media_type="application/json",
                ),
            ),
            gate_evidence=(
                GateEvidence(
                    evidence_id="source-gate",
                    gate="source_manifest",
                    passed=True,
                    validator_version="1",
                    artifact_checksums=(primary_checksum, "manifest-checksum"),
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

        snapshot = await SnapshotBuilder(
            ledger=ledger,
            registry=_registry(),
            artifact_limit=1,
            incident_limit=1,
            rejection_limit=1,
        ).build("run-1")

    payload = snapshot.model_dump_json()
    assert body not in payload
    assert snapshot.artifacts == (
        snapshot.artifacts[0].model_copy(
            update={"sha256": sha256(body.encode("utf-8")).hexdigest()}
        ),
    )
    assert len(snapshot.incidents) == 1
    assert snapshot.incidents[0].message == "x" * 500
    assert snapshot.remaining_budget_usd == 3.75
    assert snapshot.eligible_actions[0].capability == "source.ingest"
    assert snapshot.plan_rejections[0].reason_codes == ("invalid_horizon",)


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


def _snapshot_with_ingest_eligible() -> RunSnapshot:
    return RunSnapshot(
        run_id="run-1",
        status=RunStatus.RUNNING,
        eligible_actions=_registry().eligible(
            RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
        ),
        plan_rejections=(PlanRejectionView(plan_version=1, reason_codes=("invalid_horizon",)),),
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

    patch = await Planner(router=router).plan(_snapshot_with_ingest_eligible())

    assert patch.proposed_actions[0].capability == "source.ingest"
    assert patch.objective == "produce missing source evidence"
    assert patch.proposed_actions[0].expected_evidence == ("source_manifest",)
    assert router.request is not None
    schema, messages, kwargs = router.request
    assert schema is PlanPatch
    assert "one to five actions" in str(messages[0].content)
    assert "prior rejection reasons as hard feedback" in str(messages[0].content)
    assert '"eligible_actions"' in str(messages[1].content)
    assert '"invalid_horizon"' in str(messages[1].content)
    assert kwargs == {
        "agent_name": "orchestration.planner",
        "prompt_version": "dynamic-plan-v1",
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
        await Planner(router=router).plan(_snapshot_with_ingest_eligible())
