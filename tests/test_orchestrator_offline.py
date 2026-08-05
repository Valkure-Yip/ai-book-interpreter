"""Offline end-to-end proof for the durable dynamic controller."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from abi.actions import ActionDefinition, ActionRegistry, PredicateCatalog
from abi.actions.evidence import StagingEvidenceView
from abi.orchestrator.committer import Committer
from abi.orchestrator.controller import DynamicController
from abi.orchestrator.dispatcher import Dispatcher
from abi.orchestrator.projector import OutboxProjector
from abi.orchestrator.reconcile import Reconciler
from abi.planning.context import SnapshotBuilder
from abi.planning.policy import PolicyEngine
from abi.planning.scheduler import Scheduler
from abi.project import ArtifactStore, RunLedger, RunSeed, ScaffoldRequest, scaffold_project
from abi.providers.observability.events import EventLogger
from abi.providers.orchestration_runtime import DurableLoopRuntime
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionArgument,
    ActionKind,
    ActionOutcomeEnvelope,
    ActionSpec,
    ActionStatus,
    EffectSpec,
    EvidenceSpec,
    ExpectedArtifact,
    ExpectedArtifactManifest,
    GateDecision,
    PlanningContext,
    PlanPatch,
    PredicateSpec,
    ProposedAction,
    RetryPolicySpec,
    RunSnapshot,
    RunStatus,
    Succeeded,
)
from abi.types.tools import GateRuntimeMetadata


class EmptyInput(FrozenModel):
    pass


class _DeterministicPlanner:
    """Propose ingest first, then a conflict-free two-chapter batch."""

    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, context: PlanningContext) -> PlanPatch:
        self.calls += 1
        if context.policy_snapshot.plan_version == 0:
            return PlanPatch(
                objective="ingest the source",
                proposed_actions=(
                    ProposedAction(proposal_id="ingest", capability="source.ingest"),
                ),
                rationale="Create the committed source evidence first.",
            )
        return PlanPatch(
            objective="translate independent chapters",
            proposed_actions=(
                ProposedAction(proposal_id="chapter-001", capability="chapter.translate.001"),
                ProposedAction(proposal_id="chapter-002", capability="chapter.translate.002"),
            ),
            rationale="The chapters have disjoint canonical write sets.",
        )


class _ChapterBatchBarrier:
    """The test deadlocks if the two chapter actions are not dispatched together."""

    def __init__(self) -> None:
        self.started: set[str] = set()
        self.ready = asyncio.Event()

    async def enter(self, capability: str) -> None:
        self.started.add(capability)
        if len(self.started) == 2:
            self.ready.set()
        await asyncio.wait_for(self.ready.wait(), timeout=1)


_OUTPUTS = {
    "source.ingest": "source/source.txt",
    "chapter.translate.001": "chapters/translated/001.md",
    "chapter.translate.002": "chapters/translated/002.md",
}


def _action_succeeded(snapshot: object, arguments: tuple[ActionArgument, ...]) -> bool:
    if len(arguments) != 1 or arguments[0].name != "capability":
        return False
    capability = json.loads(arguments[0].value_json)
    return any(
        action.capability == capability and action.status is ActionStatus.SUCCEEDED
        for action in getattr(snapshot, "actions", ())
    )


def _manifest(
    capability: str, action_id: str, _parameters: FrozenModel
) -> ExpectedArtifactManifest:
    return ExpectedArtifactManifest(
        action_id=action_id,
        entries=(
            ExpectedArtifact(
                canonical_relpath=_OUTPUTS[capability],
                media_type="text/markdown",
                evidence_role="offline-evidence",
            ),
        ),
    )


def _gate(
    view: StagingEvidenceView,
    _parameters: FrozenModel,
    _bundle: object,
) -> GateDecision:
    assert view.read_text(next(iter(view.paths()))).strip()
    return GateDecision(
        passed=True,
        reason_code="offline_pass",
        message="deterministic fixture evidence passed",
        validator_id="offline",
        validator_version="1",
        bundle_digest=view.bundle_digest,
        artifact_checksums=view.artifact_checksums,
        evidence_refs=("offline-evidence",),
    )


def _definition(
    capability: str,
    *,
    store: ArtifactStore,
    barrier: _ChapterBatchBarrier,
) -> ActionDefinition:
    async def execute(context, _parameters):  # type: ignore[no-untyped-def]
        if capability.startswith("chapter.translate"):
            await barrier.enter(capability)
        writer = store.writer(context.action_id, context.attempt)
        writer.write_text(
            _OUTPUTS[capability],
            f"durable output for {capability}\n",
            media_type="text/markdown",
            evidence_role="offline-evidence",
        )
        return ActionOutcomeEnvelope(
            action_id=context.action_id,
            attempt=context.attempt,
            outcome=Succeeded(
                artifact_bundle=writer.artifact_bundle(),
                evidence_refs=("offline-evidence",),
            ),
        )

    prerequisites = ()
    if capability.startswith("chapter.translate"):
        prerequisites = (
            PredicateSpec(
                name="action.succeeded",
                arguments=(ActionArgument(name="capability", value_json='"source.ingest"'),),
            ),
        )
    output = _OUTPUTS[capability]
    return ActionDefinition(
        spec=ActionSpec(
            capability=capability,
            description=f"offline {capability}",
            input_schema="EmptyInput",
            action_kind=ActionKind.DETERMINISTIC,
            prerequisites=prerequisites,
            effects=(EffectSpec(name="artifact.produced", artifact_pattern=output),),
            expected_evidence=(EvidenceSpec(name="offline-evidence"),),
            tool_allowlist=(),
            read_set=("source/source.txt",) if prerequisites else (),
            write_set=(output,),
            retry_policy=RetryPolicySpec(max_attempts=1),
            validator="offline",
        ),
        input_model=EmptyInput,
        executor=execute,
        validator=_gate,  # type: ignore[arg-type]
        effect_expander=_manifest,
    )


def _completed(snapshot: RunSnapshot) -> bool:
    completed = {
        action.capability for action in snapshot.actions if action.status is ActionStatus.SUCCEEDED
    }
    return {
        "chapter.translate.001",
        "chapter.translate.002",
    } <= completed


@pytest.mark.asyncio
async def test_dynamic_run_replans_batches_chapters_and_commits_durable_evidence(
    tmp_path: Path,
) -> None:
    """Catch fixed-path orchestration or executors that bypass attempt staging."""
    project = scaffold_project(
        ScaffoldRequest(
            target_root=tmp_path,
            book_slug="offline",
            source_lang="en",
            target_lang="zh-hans",
            source_target="en-zh-hans",
        ),
        root=tmp_path / "offline",
    )
    planner = _DeterministicPlanner()
    barrier = _ChapterBatchBarrier()

    async with RunLedger.open(project.run_db) as ledger:
        run_id = await ledger.create_run(RunSeed(run_id="offline-run"))
        store = ArtifactStore(project, ledger)
        try:
            registry = ActionRegistry(
                predicates=PredicateCatalog({"action.succeeded": _action_succeeded}),
                validators={"offline": _gate},  # type: ignore[dict-item]
            )
            for capability in _OUTPUTS:
                registry.register(_definition(capability, store=store, barrier=barrier))
            registry.validate_startup()
            committer = Committer(
                ledger=ledger,
                registry=registry,
                project=project,
                artifacts=store,
            )
            reconciler = Reconciler(
                ledger=ledger,
                artifacts=store,
                registry=registry,
                committer=committer,
            )
            controller = DynamicController(
                ledger=ledger,
                registry=registry,
                planner=planner,
                policy=PolicyEngine(registry),
                snapshots=SnapshotBuilder(ledger=ledger, registry=registry),
                scheduler=Scheduler(max_parallel=2),
                dispatcher=Dispatcher(
                    ledger=ledger,
                    registry=registry,
                    project=project,
                    runtime_metadata=GateRuntimeMetadata(
                        target_language="zh-hans",
                        publication_mode="public_domain",
                    ),
                    source_lang="en",
                    source_target="en-zh-hans",
                    book_slug="offline",
                    timeout_s=2,
                ),
                committer=committer,
                reconciler=reconciler,
                projector=OutboxProjector(
                    ledger=ledger,
                    events=EventLogger(project.root / "events.jsonl", run_id),
                    status_path=project.status_projection,
                ),
                complete_when=_completed,
            )
            await DurableLoopRuntime(
                checkpoint_path=project.graph_checkpoints,
                max_cycles=8,
                on_exhausted=controller.on_cycles_exhausted,
            ).run(run_id=run_id, tick=controller.tick)

            snapshot = await ledger.load_snapshot(run_id)
            assert snapshot.status is RunStatus.COMPLETED
            assert planner.calls >= 2
            assert barrier.started == {
                "chapter.translate.001",
                "chapter.translate.002",
            }
            assert len(snapshot.artifacts) == 3
            assert len(snapshot.gate_evidence) == 3
            assert all(
                intent.status == "COMMITTED" for intent in await ledger.promotion_intents(run_id)
            )
            for action in await ledger.list_actions(run_id):
                assert (await ledger.get_attempt_outcome(action.action_id, 1)).outcome_digest
                assert (await ledger.get_gate_receipt_and_intents(action.action_id, 1))[0]
            assert all(
                not definition.spec.tool_allowlist
                for definition in (
                    registry.get("source.ingest"),
                    registry.get("chapter.translate.001"),
                    registry.get("chapter.translate.002"),
                )
            )
            assert not (project.root / "state/pipeline_state.json").exists()
        finally:
            store.close()
