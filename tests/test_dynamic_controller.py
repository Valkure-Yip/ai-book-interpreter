"""Integration tests for the durable policy-gated dynamic control loop."""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest

from abi.actions.builtins.catalog import build_action_registry
from abi.actions.contracts import (
    ActionDefinition,
    ActionExecutionContext,
    ActionValidator,
)
from abi.actions.evidence import StagingEvidenceView
from abi.actions.predicates import PredicateCatalog
from abi.actions.registry import ActionRegistry
from abi.orchestrator.committer import Committer
from abi.orchestrator.controller import DynamicController
from abi.orchestrator.dispatcher import Dispatcher
from abi.orchestrator.projector import OutboxProjector
from abi.orchestrator.reconcile import Reconciler
from abi.planning.context import SnapshotBuilder
from abi.planning.policy import PolicyEngine
from abi.planning.scheduler import Scheduler
from abi.project.artifacts import ArtifactConflictError, ArtifactStore
from abi.project.layout import BookProject
from abi.project.run_ledger import (
    ActionRecord,
    LedgerConflictError,
    LedgerNotFoundError,
    RunLedger,
    RunSeed,
)
from abi.providers.observability.events import EventLogger
from abi.providers.orchestration_runtime import DurableLoopRuntime
from abi.providers.orchestration_runtime import runtime as loop_runtime_module
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionArgument,
    ActionKind,
    ActionOutcome,
    ActionOutcomeEnvelope,
    ActionSpec,
    ActionStatus,
    ArtifactBundle,
    EffectSpec,
    ExpectedArtifact,
    ExpectedArtifactManifest,
    GateDecision,
    Indeterminate,
    Paused,
    PermanentFailure,
    PlanningContext,
    PlanPatch,
    ProbeActionInput,
    ProbeResolution,
    ProposedAction,
    RepairRequired,
    RetryableFailure,
    RetryPolicySpec,
    RunSnapshot,
    RunStatus,
    Succeeded,
    canonical_failure_signature,
    sha256_canonical_json,
)
from abi.types.tools import GateRuntimeMetadata


class EmptyInput(FrozenModel):
    pass


class SuccessTemplate(FrozenModel):
    """Test instruction to emit the definition's exact staged bundle."""

    evidence_refs: tuple[str, ...] = ()


PlannedOutcome = ActionOutcome | SuccessTemplate
ValidatorFn = Callable[
    [StagingEvidenceView, FrozenModel, ArtifactBundle], GateDecision
]

_FIXTURE_OUTPUTS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "work.multi": (
        ("reports/a.json", "application/json", "report_a"),
        ("reports/b.json", "application/json", "report_b"),
    ),
}


class SequenceExecutor:
    """Real executor double whose attempt count is observable through the ledger."""

    def __init__(
        self,
        outcomes: tuple[PlannedOutcome, ...],
        store: ArtifactStore,
        capability: str,
        envelope_action_id: str | None = None,
        delays_s: tuple[float, ...] = (),
    ) -> None:
        self._outcomes = deque(outcomes)
        self._store = store
        self._capability = capability
        self._envelope_action_id = envelope_action_id
        self._delays_s = deque(delays_s)
        self.attempt_ids: list[int] = []

    async def __call__(
        self, context: ActionExecutionContext, parameters: FrozenModel
    ) -> ActionOutcomeEnvelope:
        assert context.action_id
        assert context.runtime_metadata == GateRuntimeMetadata(
            target_language="zh-Hans", publication_mode="public_domain"
        )
        self.attempt_ids.append(context.attempt)
        if self._delays_s:
            await asyncio.sleep(self._delays_s.popleft())
        planned = self._outcomes.popleft()
        if isinstance(planned, SuccessTemplate):
            writer = self._store.writer(context.action_id, context.attempt)
            for canonical_relpath, media_type, evidence_role in _fixture_outputs(
                self._capability
            ):
                writer.write_text(
                    canonical_relpath,
                    f"result from {context.action_id} at {canonical_relpath}",
                    media_type=media_type,
                    evidence_role=evidence_role,
                )
            outcome: ActionOutcome = Succeeded(
                artifact_bundle=writer.artifact_bundle(),
                evidence_refs=planned.evidence_refs,
            )
        else:
            outcome = planned
        return ActionOutcomeEnvelope(
            action_id=self._envelope_action_id or context.action_id,
            attempt=context.attempt,
            outcome=outcome,
        )


class PatchPlanner:
    def __init__(self, patches: tuple[PlanPatch, ...]) -> None:
        self._patches = deque(patches)
        self.call_count = 0

    async def plan(self, context: object) -> PlanPatch:
        self.call_count += 1
        if self._patches:
            return self._patches.popleft()
        return PlanPatch(
            objective="wait safely",
            proposed_actions=(),
            rationale="exercise bounded invalid planning until the runtime limit",
        )


def _pass_gate(
    view: StagingEvidenceView,
    parameters: FrozenModel,
    bundle: ArtifactBundle,
) -> GateDecision:
    return GateDecision(
        passed=True,
        reason_code="fixture_valid",
        message="Fixture evidence is valid.",
        validator_id="fixture",
        validator_version="1",
        bundle_digest=view.bundle_digest,
        artifact_checksums=view.artifact_checksums,
        evidence_refs=tuple(item.canonical_relpath for item in bundle.entries),
    )


def _term_drift_gate(
    view: StagingEvidenceView,
    parameters: FrozenModel,
    bundle: ArtifactBundle,
) -> GateDecision:
    return GateDecision(
        passed=False,
        reason_code="term_drift",
        message="validator found glossary drift",
        validator_id="fixture.work.validator",
        validator_version="1",
        bundle_digest=view.bundle_digest,
        artifact_checksums=view.artifact_checksums,
        evidence_refs=(),
    )


def _unknown_repair_gate(
    view: StagingEvidenceView,
    parameters: FrozenModel,
    bundle: ArtifactBundle,
) -> GateDecision:
    return GateDecision(
        passed=False,
        reason_code="unmapped_validator_reason",
        message="validator reason has no registered repair",
        validator_id="fixture.work.validator",
        validator_version="1",
        bundle_digest=view.bundle_digest,
        artifact_checksums=view.artifact_checksums,
        evidence_refs=(),
    )


def _raising_gate(
    view: StagingEvidenceView,
    parameters: FrozenModel,
    bundle: ArtifactBundle,
) -> GateDecision:
    raise RuntimeError("validator implementation crashed")


def _fixture_outputs(capability: str) -> tuple[tuple[str, str, str], ...]:
    return _FIXTURE_OUTPUTS.get(
        capability,
        ((f"output/{capability.replace('.', '-')}.txt", "text/plain", "fixture_result"),),
    )


def _expand_fixture(
    capability: str, action_id: str, parameters: FrozenModel
) -> ExpectedArtifactManifest:
    return ExpectedArtifactManifest(
        action_id=action_id,
        entries=tuple(
            ExpectedArtifact(
                canonical_relpath=canonical_relpath,
                media_type=media_type,
                evidence_role=evidence_role,
            )
            for canonical_relpath, media_type, evidence_role in _fixture_outputs(
                capability
            )
        ),
    )


def _expand_probe(
    capability: str, action_id: str, parameters: FrozenModel
) -> ExpectedArtifactManifest:
    return ExpectedArtifactManifest(action_id=action_id, entries=())


def _patch(proposal_id: str, capability: str) -> PlanPatch:
    return PlanPatch(
        objective=f"run {capability}",
        proposed_actions=(
            ProposedAction(proposal_id=proposal_id, capability=capability),
        ),
        rationale="advance using one registered capability",
    )


def _probe_patch(proposal_id: str, binding: ProbeActionInput) -> PlanPatch:
    return PlanPatch(
        objective=f"probe indeterminate operation {binding.operation_key}",
        proposed_actions=(
            ProposedAction(
                proposal_id=proposal_id,
                capability=binding.probe_capability,
                arguments=tuple(
                    ActionArgument(
                        name=name,
                        value_json=json.dumps(value, sort_keys=True),
                    )
                    for name, value in binding.model_dump().items()
                ),
            ),
        ),
        rationale="Inspect the exact durable external operation binding.",
    )


def _definition(
    capability: str,
    executor: SequenceExecutor,
    *,
    validator_id: str = "fixture",
    validator: ValidatorFn = _pass_gate,
    retryable_codes: tuple[str, ...] = ("temporary",),
    max_attempts: int = 2,
    probe_capability: str | None = None,
    may_have_side_effects: bool = False,
) -> ActionDefinition:
    return ActionDefinition(
        spec=ActionSpec(
            capability=capability,
            description=f"Run {capability}.",
            input_schema="EmptyInput",
            action_kind=ActionKind.DETERMINISTIC,
            effects=tuple(
                EffectSpec(name="artifact.produced", artifact_pattern=path)
                for path, _, _ in _fixture_outputs(capability)
            ),
            retry_policy=RetryPolicySpec(
                max_attempts=max_attempts,
                retryable_codes=retryable_codes,
                base_delay_s=0,
                max_delay_s=0,
            ),
            validator=validator_id,
            probe_capability=probe_capability,
            may_have_side_effects=may_have_side_effects,
            write_set=tuple(path for path, _, _ in _fixture_outputs(capability)),
        ),
        input_model=EmptyInput,
        executor=executor,
        validator=cast(ActionValidator, validator),
        effect_expander=_expand_fixture,
    )


def _probe_definition(
    capability: str, executor: SequenceExecutor
) -> ActionDefinition:
    return ActionDefinition(
        spec=ActionSpec(
            capability=capability,
            description=f"Probe through {capability}.",
            input_schema="ProbeActionInput",
            action_kind=ActionKind.DETERMINISTIC,
            retry_policy=RetryPolicySpec(max_attempts=1),
            validator="fixture",
            may_have_side_effects=False,
        ),
        input_model=ProbeActionInput,
        executor=executor,
        validator=cast(ActionValidator, _pass_gate),
        effect_expander=_expand_probe,
    )


class ControllerRig:
    def __init__(
        self,
        *,
        run_id: str,
        ledger: RunLedger,
        controller: DynamicController,
        runtime: DurableLoopRuntime,
        events_path: Path,
        store: ArtifactStore,
        registry: ActionRegistry,
        dispatcher: Dispatcher,
        committer: Committer,
        reconciler: Reconciler,
        planner: PatchPlanner,
        executors: dict[str, SequenceExecutor],
    ) -> None:
        self.run_id = run_id
        self.ledger = ledger
        self.controller = controller
        self.runtime = runtime
        self.events_path = events_path
        self.store = store
        self.registry = registry
        self.dispatcher = dispatcher
        self.committer = committer
        self.reconciler = reconciler
        self.planner = planner
        self.executors = executors


@asynccontextmanager
async def _controller_rig(
    tmp_path: Path,
    *,
    definitions: tuple[tuple[str, tuple[PlannedOutcome, ...]], ...],
    patches: tuple[PlanPatch, ...],
    complete_when: Callable[[RunSnapshot], bool] | None = None,
    max_cycles: int = 12,
    spec_options: dict[str, dict[str, object]] | None = None,
    dispatcher_hook: Callable[[str, object], None] | None = None,
    committer_hook: Callable[[str, object], None] | None = None,
    semantic_repair_mappings: tuple[tuple[str, str], ...] = (),
    validators: dict[str, ValidatorFn] | None = None,
    probe_capabilities: frozenset[str] = frozenset(),
    controller_hook: Callable[[str, object], None] | None = None,
    timeout_s: float = 2,
) -> AsyncIterator[ControllerRig]:
    project = BookProject(tmp_path)
    project.root.mkdir(parents=True, exist_ok=True)
    project.run_db.parent.mkdir(parents=True, exist_ok=True)
    project.state_path.parent.mkdir(parents=True, exist_ok=True)
    async with RunLedger.open(project.run_db) as ledger:
        run_id = await ledger.create_run(RunSeed(run_id="run-1"))
        store = ArtifactStore(project, ledger)
        validator_catalog: dict[str, ActionValidator] = {
            "fixture": cast(ActionValidator, _pass_gate)
        }
        for capability, validator in (validators or {}).items():
            validator_catalog[f"fixture.{capability}"] = cast(
                ActionValidator, validator
            )
        registry = ActionRegistry(
            predicates=PredicateCatalog(),
            validators=validator_catalog,
            semantic_repair_mappings=semantic_repair_mappings,
        )
        executors: dict[str, SequenceExecutor] = {}
        for capability, outcomes in definitions:
            options = (spec_options or {}).get(capability, {})
            raw_retryable_codes = options.get("retryable_codes", ("temporary",))
            assert isinstance(raw_retryable_codes, tuple)
            assert all(isinstance(code, str) for code in raw_retryable_codes)
            raw_max_attempts = options.get("max_attempts", 2)
            assert isinstance(raw_max_attempts, int)
            executor = SequenceExecutor(
                outcomes,
                store,
                capability,
                envelope_action_id=(
                    str(options["envelope_action_id"])
                    if options.get("envelope_action_id") is not None
                    else None
                ),
                delays_s=cast(tuple[float, ...], options.get("delays_s", ())),
            )
            executors[capability] = executor
            registry.register(
                _probe_definition(capability, executor)
                if capability in probe_capabilities
                else _definition(
                    capability,
                    executor,
                    validator_id=(
                        f"fixture.{capability}"
                        if capability in (validators or {})
                        else "fixture"
                    ),
                    validator=(validators or {}).get(capability, _pass_gate),
                    retryable_codes=raw_retryable_codes,
                    max_attempts=raw_max_attempts,
                    probe_capability=(
                        str(options["probe_capability"])
                        if options.get("probe_capability") is not None
                        else None
                    ),
                    may_have_side_effects=bool(
                        options.get("may_have_side_effects", False)
                    ),
                )
            )
        registry.validate_startup()
        events_path = project.root / "events.jsonl"
        projector = OutboxProjector(
            ledger=ledger,
            events=EventLogger(events_path, run_id),
            status_path=project.status_projection,
        )
        dispatcher = Dispatcher(
            ledger=ledger,
            registry=registry,
            project=project,
            runtime_metadata=GateRuntimeMetadata(
                target_language="zh-Hans", publication_mode="public_domain"
            ),
            source_lang="en",
            source_target="en-zh-Hans",
            book_slug="fixture",
            timeout_s=timeout_s,
            test_hook=dispatcher_hook,
        )
        committer = Committer(
            ledger=ledger,
            registry=registry,
            project=project,
            artifacts=store,
            test_hook=committer_hook,
        )
        planner = PatchPlanner(patches)
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
            dispatcher=dispatcher,
            committer=committer,
            reconciler=reconciler,
            projector=projector,
            complete_when=complete_when or (lambda snapshot: False),
            test_hook=controller_hook,
        )
        runtime = DurableLoopRuntime(
            checkpoint_path=project.graph_checkpoints,
            max_cycles=max_cycles,
            on_exhausted=controller.on_cycles_exhausted,
        )
        try:
            yield ControllerRig(
                run_id=run_id,
                ledger=ledger,
                controller=controller,
                runtime=runtime,
                events_path=events_path,
                store=store,
                registry=registry,
                dispatcher=dispatcher,
                committer=committer,
                reconciler=reconciler,
                planner=planner,
                executors=executors,
            )
        finally:
            store.close()


def _completed_capability(capability: str) -> Callable[[RunSnapshot], bool]:
    return lambda snapshot: any(
        action.capability == capability and action.status.value == "SUCCEEDED"
        for action in snapshot.actions
    )


async def _authorize_one(
    rig: ControllerRig, capability: str, *, proposal_id: str = "work"
) -> tuple[ActionRecord, RunSnapshot]:
    context = await SnapshotBuilder(ledger=rig.ledger, registry=rig.registry).build(
        rig.run_id
    )
    patch = _patch(proposal_id, capability)
    plan = await rig.ledger.append_plan(rig.run_id, patch)
    decision = PolicyEngine(rig.registry).authorize(
        context.policy_snapshot, patch, next_plan_version=plan.version
    )
    assert decision.authorized
    records = await rig.ledger.authorize_actions(rig.run_id, decision.actions)
    assert len(records) == 1
    return records[0], context.policy_snapshot


async def _assert_repair_incident_outbox_matches_rows(ledger: RunLedger) -> None:
    """Assert the durable incident event contains its atomic repair classification."""
    incidents = await ledger._fetch_all(
        "SELECT incident_id, run_id, action_id, error_code, repair_class, repair_source, "
        "reason_code FROM incidents WHERE repair_class IS NOT NULL ORDER BY incident_id",
        (),
    )
    events = await ledger._fetch_all(
        "SELECT payload_json FROM event_outbox WHERE event_name = 'incident.created'",
        (),
    )
    parsed = tuple(json.loads(row["payload_json"]) for row in events)
    assert incidents
    for incident in incidents:
        expected = {
            "incident_id": incident["incident_id"],
            "action_id": incident["action_id"],
            "error_code": incident["error_code"],
            "repair_class": incident["repair_class"],
            "repair_source": incident["repair_source"],
            "reason_code": incident["reason_code"],
            "run_id": incident["run_id"],
        }
        matching = tuple(
            payload
            for payload in parsed
            if payload.get("incident_id") == incident["incident_id"]
        )
        assert matching == (expected,)


class BoundaryCrash(RuntimeError):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crash_point", "receipt_durable"),
    (("before_outcome_receipt", False), ("after_action_output", True)),
)
async def test_dispatcher_receipt_boundary_precedes_controller_hook(
    tmp_path: Path, crash_point: str, receipt_durable: bool
) -> None:
    """Catch a controller-visible outcome escaping before its immutable receipt."""
    observed: list[str] = []

    def crash(point: str, _detail: object) -> None:
        observed.append(point)
        if point == crash_point:
            raise BoundaryCrash(point)

    async with _controller_rig(
        tmp_path,
        definitions=(("work.receipt", (SuccessTemplate(),)),),
        patches=(),
        dispatcher_hook=crash,
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.receipt")
        with pytest.raises(BoundaryCrash, match=crash_point):
            await rig.dispatcher.execute(
                run_id=rig.run_id,
                action=action,
                snapshot=snapshot,
                attempt=1,
            )

        assert await rig.ledger.attempt_status(action.action_id, 1) is ActionStatus.RUNNING
        if receipt_durable:
            receipt = await rig.ledger.get_attempt_outcome(action.action_id, 1)
            envelope = ActionOutcomeEnvelope.model_validate_json(
                receipt.canonical_outcome_json
            )
            assert envelope.action_id == action.action_id
            assert envelope.attempt == 1
            assert observed == [
                "before_outcome_receipt",
                "after_outcome_receipt",
                "after_action_output",
            ]
        else:
            with pytest.raises(LedgerNotFoundError):
                await rig.ledger.get_attempt_outcome(action.action_id, 1)
            assert observed == ["before_outcome_receipt"]


@pytest.mark.asyncio
async def test_dispatcher_rejects_mismatched_envelope_without_rebinding_identity(
    tmp_path: Path,
) -> None:
    """Catch Dispatcher silently replacing an executor's wrong action identity."""
    async with _controller_rig(
        tmp_path,
        definitions=((
            "work.identity",
            (PermanentFailure(error_code="wrong", message="wrong identity"),),
        ),),
        patches=(),
        spec_options={"work.identity": {"envelope_action_id": "wrong-action"}},
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.identity")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id, action=action, snapshot=snapshot, attempt=1
        )

        assert isinstance(envelope.outcome, RepairRequired)
        assert envelope.outcome.repair_class == "integrity"
        assert envelope.outcome.reason_code == "artifact_identity_conflict"
        receipt = await rig.ledger.get_attempt_outcome(action.action_id, 1)
        assert receipt.outcome_digest == sha256_canonical_json(
            receipt.canonical_outcome_json
        )


@pytest.mark.asyncio
async def test_dispatcher_rejects_probe_resolution_from_non_probe(tmp_path: Path) -> None:
    """Catch an ordinary capability resolving an unrelated external operation."""
    async with _controller_rig(
        tmp_path,
        definitions=((
            "work.not-probe",
            (ProbeResolution(
                operation_key="external:1",
                disposition="succeeded",
                evidence_refs=("external:1",),
                message="not authorized as a probe",
            ),),
        ),),
        patches=(),
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.not-probe")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id, action=action, snapshot=snapshot, attempt=1
        )

        assert isinstance(envelope.outcome, RepairRequired)
        assert envelope.outcome.repair_class == "integrity"
        assert envelope.outcome.reason_code == "probe_resolution_conflict"
        assert await rig.ledger.get_attempt_outcome(action.action_id, 1)


@pytest.mark.asyncio
async def test_committer_persists_complete_multi_file_bundle_before_success(
    tmp_path: Path,
    ) -> None:
    """Catch single-file promotion or success before every exact intent is committed."""
    async with _controller_rig(
        tmp_path,
        definitions=(("work.multi", (SuccessTemplate(),)),),
        patches=(),
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.multi")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id, action=action, snapshot=snapshot, attempt=1
        )
        assert isinstance(envelope.outcome, Succeeded)
        assert not (tmp_path / "reports/a.json").exists()
        assert not (tmp_path / "reports/b.json").exists()

        committed = await rig.committer.commit(
            run_id=rig.run_id,
            action=action,
            attempt=1,
            outcome=envelope.outcome,
        )

        _, intents = await rig.ledger.get_gate_receipt_and_intents(action.action_id, 1)
        assert tuple(item.canonical_relpath for item in intents) == (
            "reports/a.json",
            "reports/b.json",
        )
        assert {item.status for item in intents} == {"COMMITTED"}
        assert tuple(item.relpath for item in committed.artifacts) == (
            "reports/a.json",
            "reports/b.json",
        )
        assert await rig.ledger.action_status(action.action_id) is ActionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_committer_has_zero_canonical_copies_until_gate_and_all_intents_durable(
    tmp_path: Path,
) -> None:
    """Catch per-entry intent creation interleaved with canonical copying."""
    observed: list[str] = []

    def crash(point: str, detail: object) -> None:
        observed.append(point)
        if point == "after_gate_receipt_and_intents":
            assert not (tmp_path / "reports/a.json").exists()
            assert not (tmp_path / "reports/b.json").exists()
            raise BoundaryCrash(point)

    async with _controller_rig(
        tmp_path,
        definitions=(("work.multi", (SuccessTemplate(),)),),
        patches=(),
        committer_hook=crash,
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.multi")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id, action=action, snapshot=snapshot, attempt=1
        )
        assert isinstance(envelope.outcome, Succeeded)

        with pytest.raises(BoundaryCrash, match="after_gate_receipt_and_intents"):
            await rig.committer.commit(
                run_id=rig.run_id,
                action=action,
                attempt=1,
                outcome=envelope.outcome,
            )

        gate, intents = await rig.ledger.get_gate_receipt_and_intents(action.action_id, 1)
        assert gate.action_id == action.action_id
        assert len(intents) == 2
        assert {item.status for item in intents} == {"PENDING"}
        assert await rig.ledger.attempt_status(action.action_id, 1) is ActionStatus.RUNNING
        assert observed == [
            "before_gate_receipt_and_intents",
            "after_gate_receipt_and_intents",
        ]


@pytest.mark.asyncio
async def test_committer_unified_postcheck_blocks_one_drifted_sibling(
    tmp_path: Path,
) -> None:
    """Catch per-file success that misses drift among already committed siblings."""

    def drift(point: str, detail: object) -> None:
        if point == "after_all_intents_committed":
            path = tmp_path / "reports/b.json"
            path.unlink()
            path.write_bytes(b"drift")

    async with _controller_rig(
        tmp_path,
        definitions=(("work.multi", (SuccessTemplate(),)),),
        patches=(),
        committer_hook=drift,
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.multi")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id, action=action, snapshot=snapshot, attempt=1
        )
        assert isinstance(envelope.outcome, Succeeded)

        with pytest.raises(ArtifactConflictError):
            await rig.committer.commit(
                run_id=rig.run_id,
                action=action,
                attempt=1,
                outcome=envelope.outcome,
            )

        assert await rig.ledger.attempt_status(action.action_id, 1) is ActionStatus.REPAIR_REQUIRED
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        assert await rig.ledger.action_status(action.action_id) is ActionStatus.REPAIR_REQUIRED


@pytest.mark.asyncio
async def test_committer_crash_after_unified_postcheck_does_not_mark_success(
    tmp_path: Path,
) -> None:
    """Catch the filesystem postcheck being mistaken for the ledger success boundary."""

    def crash(point: str, detail: object) -> None:
        if point == "after_unified_bundle_postcheck":
            raise BoundaryCrash(point)

    async with _controller_rig(
        tmp_path,
        definitions=(("work.multi", (SuccessTemplate(),)),),
        patches=(),
        committer_hook=crash,
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.multi")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id, action=action, snapshot=snapshot, attempt=1
        )
        assert isinstance(envelope.outcome, Succeeded)

        with pytest.raises(BoundaryCrash, match="after_unified_bundle_postcheck"):
            await rig.committer.commit(
                run_id=rig.run_id,
                action=action,
                attempt=1,
                outcome=envelope.outcome,
            )

        _, intents = await rig.ledger.get_gate_receipt_and_intents(action.action_id, 1)
        assert {item.status for item in intents} == {"COMMITTED"}
        assert await rig.ledger.attempt_status(action.action_id, 1) is ActionStatus.RUNNING
        assert await rig.ledger.count_artifacts_for(action.action_id) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "before_outcome_receipt",
        "after_action_output",
        "after_gate_receipt_and_intents",
        "after_first_intent_promotion",
        "after_second_intent_promotion",
        "after_all_intents_committed",
        "after_unified_bundle_postcheck",
        "before_success_ledger_commit",
        "after_success_ledger_commit",
    ),
)
async def test_two_entry_crash_matrix_recovers_without_redispatch(
    tmp_path: Path, boundary: str
) -> None:
    def crash_at(point: str, detail: object) -> None:
        matches = point == boundary
        if boundary == "after_first_intent_promotion":
            matches = (
                point == "after_intent_promotion"
                and getattr(detail, "ordinal", None) == 0
            )
        elif boundary == "after_second_intent_promotion":
            matches = (
                point == "after_intent_promotion"
                and getattr(detail, "ordinal", None) == 1
            )
        if matches:
            raise BoundaryCrash(boundary)

    dispatcher_boundary = boundary in {
        "before_outcome_receipt",
        "after_action_output",
    }
    async with _controller_rig(
        tmp_path,
        definitions=(("work.multi", (SuccessTemplate(),)),),
        patches=(),
        dispatcher_hook=crash_at if dispatcher_boundary else None,
        committer_hook=None if dispatcher_boundary else crash_at,
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.multi")
        if dispatcher_boundary:
            with pytest.raises(BoundaryCrash, match=boundary):
                await rig.dispatcher.execute(
                    run_id=rig.run_id,
                    action=action,
                    snapshot=snapshot,
                    attempt=1,
                )
        else:
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

        if boundary == "before_outcome_receipt":
            with pytest.raises(LedgerNotFoundError):
                await rig.ledger.get_attempt_outcome(action.action_id, 1)
        else:
            assert await rig.ledger.get_attempt_outcome(action.action_id, 1)
        if boundary == "after_gate_receipt_and_intents":
            _, crash_intents = await rig.ledger.get_gate_receipt_and_intents(
                action.action_id, 1
            )
            assert len(crash_intents) == 2
            assert {intent.status for intent in crash_intents} == {"PENDING"}
            assert not (tmp_path / "reports/a.json").exists()
            assert not (tmp_path / "reports/b.json").exists()
        if boundary == "after_first_intent_promotion":
            _, crash_intents = await rig.ledger.get_gate_receipt_and_intents(
                action.action_id, 1
            )
            assert tuple(intent.status for intent in crash_intents) == (
                "COMMITTED",
                "PENDING",
            )
        if boundary == "after_second_intent_promotion":
            _, crash_intents = await rig.ledger.get_gate_receipt_and_intents(
                action.action_id, 1
            )
            assert {intent.status for intent in crash_intents} == {"COMMITTED"}
        if boundary == "after_success_ledger_commit":
            assert await rig.ledger.action_status(action.action_id) is ActionStatus.SUCCEEDED
            assert await rig.ledger.count_artifacts_for(action.action_id) == 2
        else:
            assert await rig.ledger.action_status(action.action_id) is ActionStatus.RUNNING
            assert await rig.ledger.count_artifacts_for(action.action_id) == 0

        clean_committer = Committer(
            ledger=rig.ledger,
            registry=rig.registry,
            project=BookProject(tmp_path),
            artifacts=rig.store,
        )
        clean_reconciler = Reconciler(
            ledger=rig.ledger,
            artifacts=rig.store,
            registry=rig.registry,
            committer=clean_committer,
        )
        await clean_reconciler.reconcile(rig.run_id)

        assert rig.executors["work.multi"].attempt_ids == [1]
        assert await rig.ledger.action_status(action.action_id) is ActionStatus.SUCCEEDED
        assert await rig.ledger.count_committed_actions(action.action_id) == 1
        assert await rig.ledger.count_artifacts_for(action.action_id) == 2
        _, intents = await rig.ledger.get_gate_receipt_and_intents(action.action_id, 1)
        assert len(intents) == 2
        assert {intent.status for intent in intents} == {"COMMITTED"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    (
        "missing",
        "extra",
        "empty_dir",
        "symlink",
        "directory",
        "permission",
        "bundle_identity",
    ),
)
async def test_committer_fails_closed_on_unsafe_or_mismatched_bundle(
    tmp_path: Path, defect: str
) -> None:
    async with _controller_rig(
        tmp_path,
        definitions=(("work.multi", (SuccessTemplate(),)),),
        patches=(),
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.multi")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id, action=action, snapshot=snapshot, attempt=1
        )
        assert isinstance(envelope.outcome, Succeeded)
        outcome = envelope.outcome
        first_staged = tmp_path / outcome.artifact_bundle.entries[0].staged_relpath
        if defect == "missing":
            first_staged.unlink()
        elif defect == "extra":
            (first_staged.parent / "extra.txt").write_text("extra", encoding="utf-8")
        elif defect == "empty_dir":
            (first_staged.parent / "unexpected").mkdir()
        elif defect == "symlink":
            external = tmp_path / "external.txt"
            external.write_text("external", encoding="utf-8")
            first_staged.unlink()
            first_staged.symlink_to(external)
        elif defect == "directory":
            first_staged.unlink()
            first_staged.mkdir()
        elif defect == "permission":
            action = action.model_copy(update={"write_set": ()})
        else:
            outcome = outcome.model_copy(
                update={
                    "artifact_bundle": outcome.artifact_bundle.model_copy(
                        update={"action_id": "wrong-action"}
                    )
                }
            )

        with pytest.raises(ArtifactConflictError):
            await rig.committer.commit(
                run_id=rig.run_id,
                action=action,
                attempt=1,
                outcome=outcome,
            )

        persisted_action = (await rig.ledger.list_actions(rig.run_id))[0]
        assert persisted_action.status is ActionStatus.REPAIR_REQUIRED
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        assert await rig.ledger.promotion_intents(rig.run_id) == ()
        assert await rig.ledger.count_artifacts_for(persisted_action.action_id) == 0
        with pytest.raises(LedgerNotFoundError, match="gate receipt"):
            await rig.ledger.get_gate_receipt_and_intents(
                persisted_action.action_id, 1
            )
        assert not (tmp_path / "reports/a.json").exists()
        assert not (tmp_path / "reports/b.json").exists()
        await _assert_repair_incident_outbox_matches_rows(rig.ledger)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_site", ("registry", "validator"))
async def test_committer_boundary_exception_durably_compensates_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
) -> None:
    validators: dict[str, ValidatorFn] = (
        {"work.commit-boundary": _raising_gate}
        if failure_site == "validator"
        else {}
    )
    async with _controller_rig(
        tmp_path,
        definitions=(("work.commit-boundary", (SuccessTemplate(),)),),
        patches=(),
        validators=validators,
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.commit-boundary")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id, action=action, snapshot=snapshot, attempt=1
        )
        assert isinstance(envelope.outcome, Succeeded)
        staged_paths = tuple(
            tmp_path / entry.staged_relpath
            for entry in envelope.outcome.artifact_bundle.entries
        )
        if failure_site == "registry":
            def fail_resolve(capability: str, parameters_json: str) -> object:
                raise RuntimeError("registry implementation crashed")

            monkeypatch.setattr(rig.registry, "resolve_json", fail_resolve)

        first = await rig.reconciler.reconcile(rig.run_id)
        second = await rig.reconciler.reconcile(rig.run_id)

        assert first.status is RunStatus.BLOCKED
        assert second.status is RunStatus.BLOCKED
        assert await rig.ledger.action_status(action.action_id) is ActionStatus.REPAIR_REQUIRED
        attempt = await rig.ledger.get_attempt(action.action_id, 1)
        assert (attempt.repair_class, attempt.repair_source, attempt.reason_code) == (
            "integrity",
            "integrity_guard",
            "gate_binding_conflict",
        )
        assert all(path.is_file() for path in staged_paths)
        assert await rig.ledger.promotion_intents(rig.run_id) == ()
        assert await rig.ledger.count_artifacts_for(action.action_id) == 0
        assert sum(
            incident.reason_code == "gate_binding_conflict"
            for incident in second.incidents
        ) == 1


@pytest.mark.asyncio
async def test_real_source_ingest_stages_receipts_promotes_bundle_then_succeeds(
    tmp_path: Path,
) -> None:
    """Catch built-ins that bypass staging or the exact multi-intent success protocol."""
    project = BookProject(tmp_path)
    project.root.mkdir(parents=True, exist_ok=True)
    project.source_raw.parent.mkdir(parents=True, exist_ok=True)
    (project.root / "source/raw.txt").write_text(
        "Fixture Book\n\nChapter One\n\nA public-domain fixture paragraph.",
        encoding="utf-8",
    )
    project.run_db.parent.mkdir(parents=True, exist_ok=True)
    registry = build_action_registry()
    async with RunLedger.open(project.run_db) as ledger:
        run_id = await ledger.create_run(RunSeed(run_id="run-builtin"))
        store = ArtifactStore(project, ledger)
        try:
            context = await SnapshotBuilder(ledger=ledger, registry=registry).build(run_id)
            patch = PlanPatch(
                objective="ingest the source",
                proposed_actions=(
                    ProposedAction(
                        proposal_id="ingest",
                        capability="source.ingest",
                        arguments=(
                            ActionArgument(
                                name="source_relpath", value_json='"source/raw.txt"'
                            ),
                        ),
                    ),
                ),
                rationale="exercise the real deterministic built-in",
            )
            plan = await ledger.append_plan(run_id, patch)
            decision = PolicyEngine(registry).authorize(
                context.policy_snapshot, patch, next_plan_version=plan.version
            )
            assert decision.authorized
            (action,) = await ledger.authorize_actions(run_id, decision.actions)
            dispatcher = Dispatcher(
                ledger=ledger,
                registry=registry,
                project=project,
                runtime_metadata=GateRuntimeMetadata(
                    target_language="zh-Hans", publication_mode="public_domain"
                ),
                source_lang="en",
                source_target="en-zh-Hans",
                book_slug="fixture",
                timeout_s=2,
            )
            envelope = await dispatcher.execute(
                run_id=run_id,
                action=action,
                snapshot=context.policy_snapshot,
                attempt=1,
            )
            assert isinstance(envelope.outcome, Succeeded)
            assert not project.source_manifest.exists()
            assert not project.source_clean.exists()
            assert await ledger.get_attempt_outcome(action.action_id, 1)

            committed = await Committer(
                ledger=ledger,
                registry=registry,
                project=project,
                artifacts=store,
            ).commit(
                run_id=run_id,
                action=action,
                attempt=1,
                outcome=envelope.outcome,
            )

            assert project.source_manifest.is_file()
            assert project.source_clean.is_file()
            assert tuple(item.relpath for item in committed.artifacts) == (
                "source/source_manifest.json",
                "source/source_text.txt",
            )
            _, intents = await ledger.get_gate_receipt_and_intents(action.action_id, 1)
            assert len(intents) == 2
            assert {item.status for item in intents} == {"COMMITTED"}
        finally:
            store.close()


def _expected_replan_event_sequence() -> tuple[str, ...]:
    return (
        "plan.proposed",
        "plan.authorized",
        "action.authorized",
        "action.started",
        "action.outcome",
        "incident.created",
        "run.replanned",
        "plan.proposed",
        "plan.authorized",
        "action.authorized",
        "action.started",
        "action.outcome",
        "action.validated",
        "action.committed",
        "action.reconciled",
        "run.completed",
    )


@pytest.mark.asyncio
async def test_controller_replans_after_repair_and_completes(tmp_path: Path) -> None:
    """Catch repair routing that retries the failed Action or trusts its PASS claim."""
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
            (
                "repair.glossary",
                (SuccessTemplate(),),
            ),
        ),
        patches=(
            _patch("a1", "work.initial"),
            _patch("a2", "repair.glossary"),
        ),
        semantic_repair_mappings=(("term_drift", "repair.glossary"),),
        complete_when=_completed_capability("repair.glossary"),
    ) as rig:
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.COMPLETED
        assert await rig.ledger.event_names() == _expected_replan_event_sequence()
        assert await rig.ledger.count_attempts(capability="work.initial") == 1
        assert await rig.ledger.count_attempts(capability="repair.glossary") == 1
        await _assert_repair_incident_outbox_matches_rows(rig.ledger)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "after_repair_fact_commit",
        "before_semantic_replan",
        "after_repair_plan_append",
        "after_repair_authorization",
    ),
)
async def test_semantic_repair_crash_boundaries_create_one_replacement_action(
    tmp_path: Path, boundary: str
) -> None:
    enabled = True

    def crash_at(point: str, detail: object) -> None:
        if not enabled:
            return
        matches = False
        if boundary == "after_repair_fact_commit" and point == "after_reconcile":
            matches = isinstance(detail, RunSnapshot) and any(
                action.repair_class == "semantic" for action in detail.actions
            )
        elif boundary == "before_semantic_replan" and point == "before_planner":
            context = cast(PlanningContext, detail)
            matches = any(
                incident.repair_class == "semantic"
                for incident in context.policy_snapshot.incidents
            )
        elif boundary == "after_repair_plan_append" and point == "after_plan_append":
            matches = getattr(detail, "version", None) == 2
        elif boundary == "after_repair_authorization" and point == "after_authorization":
            authorized = cast(tuple[ActionRecord, ...], detail)
            matches = any(
                action.capability == "repair.glossary" for action in authorized
            )
        if matches:
            raise BoundaryCrash(boundary)

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
        controller_hook=crash_at,
    ) as rig:
        with pytest.raises(BoundaryCrash, match=boundary):
            await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        snapshot = await rig.ledger.load_snapshot(rig.run_id)
        original = next(
            action for action in snapshot.actions if action.capability == "work.initial"
        )
        assert snapshot.status is RunStatus.RUNNING
        assert original.status is ActionStatus.REPAIR_REQUIRED
        assert await rig.ledger.attempt_numbers(original.action_id) == (1,)
        if boundary in {"after_repair_fact_commit", "before_semantic_replan"}:
            assert snapshot.plan_version == 1
            assert len(snapshot.actions) == 1
        elif boundary == "after_repair_plan_append":
            assert snapshot.plan_version == 2
            assert len(snapshot.actions) == 1
        else:
            assert snapshot.plan_version == 2
            assert len(snapshot.actions) == 2

        enabled = False
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
        final = await rig.ledger.load_snapshot(rig.run_id)
        assert final.status is RunStatus.COMPLETED
        assert final.plan_version == 2
        assert len(final.actions) == 2
        assert rig.executors["work.initial"].attempt_ids == [1]
        assert rig.executors["repair.glossary"].attempt_ids == [1]
        assert await rig.ledger.attempt_numbers(original.action_id) == (1,)


@pytest.mark.asyncio
async def test_validator_failure_receipt_drives_repair_without_pass_or_promotion(
    tmp_path: Path,
) -> None:
    """Catch a failed GateDecision being persisted as PASS or copied canonically."""
    async with _controller_rig(
        tmp_path,
        definitions=(
            ("work.validator", (SuccessTemplate(),)),
            ("repair.glossary", (SuccessTemplate(),)),
        ),
        patches=(
            _patch("validate", "work.validator"),
            _patch("repair", "repair.glossary"),
        ),
        validators={"work.validator": _term_drift_gate},
        semantic_repair_mappings=(("term_drift", "repair.glossary"),),
        complete_when=_completed_capability("repair.glossary"),
    ) as rig:
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        initial = next(
            action
            for action in await rig.ledger.list_actions(rig.run_id)
            if action.capability == "work.validator"
        )
        raw_failure = await rig.ledger.get_validator_failure_receipt(
            initial.action_id, 1
        )
        decision = GateDecision.model_validate_json(
            raw_failure.canonical_gate_decision_json
        )
        assert not decision.passed
        assert (initial.repair_class, initial.repair_source, initial.reason_code) == (
            "semantic",
            "validator",
            "term_drift",
        )
        with pytest.raises(LedgerNotFoundError, match="gate receipt"):
            await rig.ledger.get_gate_receipt_and_intents(initial.action_id, 1)
        assert not any(
            intent.action_id == initial.action_id
            for intent in await rig.ledger.promotion_intents(rig.run_id)
        )
        assert await rig.ledger.count_artifacts_for(initial.action_id) == 0
        assert not (tmp_path / "output/work-validator.txt").exists()
        outcome_events = tuple(
            json.loads(line)
            for line in rig.events_path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") == "action.outcome"
            and json.loads(line).get("action_id") == initial.action_id
        )
        assert len(outcome_events) == 1
        assert {
            key: outcome_events[0][key]
            for key in ("repair_class", "repair_source", "reason_code")
        } == {
            "repair_class": "semantic",
            "repair_source": "validator",
            "reason_code": "term_drift",
        }
        await _assert_repair_incident_outbox_matches_rows(rig.ledger)


@pytest.mark.asyncio
async def test_unmapped_validator_failure_preserves_raw_fact_and_blocks_integrity(
    tmp_path: Path,
) -> None:
    async with _controller_rig(
        tmp_path,
        definitions=(("work.validator", (SuccessTemplate(),)),),
        patches=(_patch("validate", "work.validator"),),
        validators={"work.validator": _unknown_repair_gate},
    ) as rig:
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        action = (await rig.ledger.list_actions(rig.run_id))[0]
        raw_failure = await rig.ledger.get_validator_failure_receipt(
            action.action_id, 1
        )
        raw_decision = GateDecision.model_validate_json(
            raw_failure.canonical_gate_decision_json
        )
        assert raw_decision.reason_code == "unmapped_validator_reason"
        assert (action.repair_class, action.repair_source, action.reason_code) == (
            "integrity",
            "integrity_guard",
            "repair_class_unknown",
        )
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        with pytest.raises(LedgerNotFoundError, match="gate receipt"):
            await rig.ledger.get_gate_receipt_and_intents(action.action_id, 1)
        assert await rig.ledger.promotion_intents(rig.run_id) == ()
        assert await rig.ledger.count_artifacts_for(action.action_id) == 0
        assert not (tmp_path / "output/work-validator.txt").exists()
        outcome = next(
            json.loads(line)
            for line in rig.events_path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("event") == "action.outcome"
        )
        assert {
            key: outcome[key]
            for key in ("repair_class", "repair_source", "reason_code")
        } == {
            "repair_class": "integrity",
            "repair_source": "integrity_guard",
            "reason_code": "repair_class_unknown",
        }
        await _assert_repair_incident_outbox_matches_rows(rig.ledger)


@pytest.mark.asyncio
async def test_integrity_repair_blocks_without_planner_or_replacement(
    tmp_path: Path,
) -> None:
    """Catch integrity evidence entering the semantic Planner repair path."""
    async with _controller_rig(
        tmp_path,
        definitions=((
            "work.integrity",
            (RepairRequired(
                repair_class="integrity",
                repair_source="action_outcome",
                reason_code="artifact_identity_conflict",
                defect_codes=("artifact_identity_conflict",),
                message="preserve evidence for human resolution",
            ),),
        ),),
        patches=(_patch("a1", "work.integrity"),),
    ) as rig:
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        assert rig.planner.call_count == 1
        assert len(await rig.ledger.list_actions(rig.run_id)) == 1
        action = (await rig.ledger.list_actions(rig.run_id))[0]
        assert action.repair_class == "integrity"
        assert action.reason_code == "artifact_identity_conflict"
        await _assert_repair_incident_outbox_matches_rows(rig.ledger)


@pytest.mark.asyncio
async def test_unmapped_semantic_repair_becomes_integrity_unknown(
    tmp_path: Path,
) -> None:
    """Catch an unmapped semantic reason defaulting to automatic repair."""
    async with _controller_rig(
        tmp_path,
        definitions=((
            "work.unknown-repair",
            (RepairRequired(
                repair_class="semantic",
                repair_source="action_outcome",
                reason_code="unknown_quality_reason",
                defect_codes=("unknown_quality_reason",),
                message="classification has no registry mapping",
            ),),
        ),),
        patches=(_patch("a1", "work.unknown-repair"),),
    ) as rig:
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        action = (await rig.ledger.list_actions(rig.run_id))[0]
        assert action.repair_class == "integrity"
        assert action.repair_source == "integrity_guard"
        assert action.reason_code == "repair_class_unknown"
        assert len(await rig.ledger.list_actions(rig.run_id)) == 1

        # A graph/checkpoint replay must defer to the already committed ledger fact.
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
        assert await rig.ledger.count_attempts() == 1
        assert len(await rig.ledger.list_actions(rig.run_id)) == 1


@pytest.mark.asyncio
async def test_permanent_failure_blocks_without_retry(tmp_path: Path) -> None:
    """Catch permanent copyright denial entering the retry loop."""
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "rights.check",
                (
                    PermanentFailure(
                        error_code="copyright_denied",
                        message="supply a license or use private mode",
                    ),
                ),
            ),
        ),
        patches=(_patch("rights", "rights.check"),),
    ) as rig:
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        assert await rig.ledger.count_attempts(capability="rights.check") == 1
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED

        before = len(rig.events_path.read_text(encoding="utf-8").splitlines())
        await rig.ledger._db.execute(
            "UPDATE event_outbox SET delivered_at = NULL WHERE event_name = 'run.blocked'"
        )
        await rig.ledger._db.commit()
        restarted_projector = OutboxProjector(
            ledger=rig.ledger,
            events=EventLogger(rig.events_path, rig.run_id),
            status_path=tmp_path / "state/restarted-status.json",
        )
        await restarted_projector.flush(rig.run_id)
        assert len(rig.events_path.read_text(encoding="utf-8").splitlines()) == before
        assert await rig.ledger.undelivered_events() == ()


@pytest.mark.asyncio
async def test_outbox_projection_is_scoped_to_one_run(tmp_path: Path) -> None:
    """Catch one run consuming and acknowledging another run's projection events."""
    project = BookProject(tmp_path)
    project.root.mkdir(parents=True, exist_ok=True)
    project.run_db.parent.mkdir(parents=True, exist_ok=True)
    async with RunLedger.open(project.run_db) as ledger:
        first = await ledger.create_run(RunSeed(run_id="run-a"))
        second = await ledger.create_run(RunSeed(run_id="run-b"))
        await ledger.append_plan(first, _patch("a", "work.a"))
        await ledger.append_plan(second, _patch("b", "work.b"))
        events_path = tmp_path / "events-a.jsonl"
        projector = OutboxProjector(
            ledger=ledger,
            events=EventLogger(events_path, first),
            status_path=tmp_path / "status-a.json",
        )

        assert await projector.flush(first) == 1
        records = tuple(
            json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()
        )
        assert {record["run_id"] for record in records} == {first}
        assert tuple(event.run_id for event in await ledger.undelivered_events(second)) == (
            second,
        )


def test_durable_loop_runtime_has_no_business_module_dependency() -> None:
    """Catch the provider-generic cycle graph importing controller business models."""
    source = inspect.getsource(loop_runtime_module)
    forbidden = (
        "abi.actions",
        "abi.orchestrator",
        "abi.planning",
        "abi.project",
        "abi.types.orchestration",
        "abi.providers.agent_runtime",
    )
    assert not any(module in source for module in forbidden)


@pytest.mark.asyncio
async def test_durable_loop_runtime_reuses_run_id_as_checkpoint_thread(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "graph-checkpoints.sqlite"
    calls: list[str] = []

    async def stop(run_id: str) -> bool:
        calls.append(run_id)
        return False

    await DurableLoopRuntime(
        checkpoint_path=checkpoint_path, max_cycles=2
    ).run(run_id="run-a", tick=stop)
    await DurableLoopRuntime(
        checkpoint_path=checkpoint_path, max_cycles=2
    ).run(run_id="run-a", tick=stop)
    await DurableLoopRuntime(
        checkpoint_path=checkpoint_path, max_cycles=2
    ).run(run_id="run-b", tick=stop)

    with sqlite3.connect(checkpoint_path) as db:
        thread_ids = {
            str(row[0])
            for row in db.execute("SELECT DISTINCT thread_id FROM checkpoints")
        }
    assert thread_ids == {"run-a", "run-b"}
    assert calls == ["run-a", "run-a", "run-b"]


@pytest.mark.asyncio
async def test_outbox_flush_preserves_run_order_across_restart_crash_window(
    tmp_path: Path,
) -> None:
    project = BookProject(tmp_path)
    project.run_db.parent.mkdir(parents=True, exist_ok=True)
    async with RunLedger.open(project.run_db) as ledger:
        run_id = await ledger.create_run(RunSeed(run_id="ordered-run"))
        for ordinal in range(3):
            await ledger.record_event(
                run_id=run_id,
                event_name=f"fixture.{ordinal}",
                aggregate_id=run_id,
                payload_json=json.dumps({"ordinal": ordinal}),
                idempotency_key=f"fixture:{run_id}:{ordinal}",
            )
        events_path = tmp_path / "events.jsonl"
        projector = OutboxProjector(
            ledger=ledger,
            events=EventLogger(events_path, run_id),
            status_path=tmp_path / "status.json",
            metrics_path=tmp_path / "metrics.json",
        )
        assert await projector.flush(run_id) == 3
        first_projection = tuple(
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
        )
        assert tuple(item["ordinal"] for item in first_projection) == (0, 1, 2)
        assert tuple(item["sequence"] for item in first_projection) == tuple(
            sorted(item["sequence"] for item in first_projection)
        )
        first_status = (tmp_path / "status.json").read_text(encoding="utf-8")
        first_metrics = json.loads(
            (tmp_path / "metrics.json").read_text(encoding="utf-8")
        )

        await ledger._db.execute(
            "UPDATE event_outbox SET delivered_at = NULL WHERE idempotency_key = ?",
            ("fixture:ordered-run:1",),
        )
        await ledger._db.commit()
        restarted = OutboxProjector(
            ledger=ledger,
            events=EventLogger(events_path, run_id),
            status_path=tmp_path / "status.json",
            metrics_path=tmp_path / "metrics.json",
        )
        assert await restarted.flush(run_id) == 1
        assert len(events_path.read_text(encoding="utf-8").splitlines()) == 3
        assert await ledger.undelivered_events(run_id) == ()
        assert (tmp_path / "status.json").read_text(encoding="utf-8") == first_status
        assert json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8")) == (
            first_metrics
        )


@pytest.mark.asyncio
async def test_retryable_failure_retries_only_with_registered_bounded_code(
    tmp_path: Path,
) -> None:
    """Catch retry classification bypassing the ActionSpec code and attempt limit."""
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "network.fetch",
                (
                    RetryableFailure(error_code="temporary", message="try again"),
                    SuccessTemplate(),
                ),
            ),
        ),
        patches=(_patch("fetch", "network.fetch"),),
        complete_when=_completed_capability("network.fetch"),
    ) as rig:
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        assert await rig.ledger.count_attempts(capability="network.fetch") == 2
        action = (await rig.ledger.list_actions(rig.run_id))[0]
        assert rig.executors["network.fetch"].attempt_ids == [1, 2]
        assert await rig.ledger.attempt_numbers(action.action_id) == (1, 2)
        assert (await rig.ledger.get_attempt(action.action_id, 1)).status is ActionStatus.RETRY_WAIT
        assert (await rig.ledger.get_attempt(action.action_id, 2)).status is ActionStatus.SUCCEEDED
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "after_retry_wait",
        "before_create_next_attempt",
        "after_create_next_attempt",
        "before_dispatch_next_attempt",
    ),
)
async def test_retry_successor_crash_boundaries_never_reenter_attempt_one(
    tmp_path: Path, boundary: str
) -> None:
    enabled = True

    def crash_at(point: str, detail: object) -> None:
        if not enabled:
            return
        if boundary == "after_retry_wait" and point == "after_reconcile":
            snapshot = detail
            if isinstance(snapshot, RunSnapshot) and any(
                action.status is ActionStatus.RETRY_WAIT
                for action in snapshot.actions
            ):
                raise BoundaryCrash(boundary)
        elif boundary == point:
            raise BoundaryCrash(boundary)
        elif boundary == "before_dispatch_next_attempt" and point == "before_dispatch":
            _, attempt = cast(tuple[ActionRecord, int], detail)
            if attempt == 2:
                raise BoundaryCrash(boundary)

    hook_point = {
        "before_dispatch_next_attempt": "before_dispatch_next_attempt",
    }.get(boundary, boundary)
    async with _controller_rig(
        tmp_path,
        definitions=((
            "network.fetch",
            (
                RetryableFailure(error_code="temporary", message="try again"),
                SuccessTemplate(),
            ),
        ),),
        patches=(_patch("fetch", "network.fetch"),),
        complete_when=_completed_capability("network.fetch"),
        controller_hook=crash_at,
    ) as rig:
        with pytest.raises(BoundaryCrash, match=hook_point):
            await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        action = (await rig.ledger.list_actions(rig.run_id))[0]
        assert (await rig.ledger.get_attempt(action.action_id, 1)).status is ActionStatus.RETRY_WAIT
        assert rig.executors["network.fetch"].attempt_ids == [1]
        if boundary in {"after_create_next_attempt", "before_dispatch_next_attempt"}:
            assert await rig.ledger.attempt_numbers(action.action_id) == (1, 2)
            assert (await rig.ledger.get_attempt(action.action_id, 2)).status is ActionStatus.AUTHORIZED
        else:
            assert await rig.ledger.attempt_numbers(action.action_id) == (1,)

        enabled = False
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
        assert rig.executors["network.fetch"].attempt_ids == [1, 2]
        assert await rig.ledger.attempt_numbers(action.action_id) == (1, 2)
        assert await rig.ledger.action_status(action.action_id) is ActionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_reconciler_rebuilds_safe_missing_receipt_without_redispatch(
    tmp_path: Path,
) -> None:
    """Catch a RUNNING receipt gap causing the same executor to run twice."""
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "work.recover",
                (SuccessTemplate(),),
            ),
        ),
        patches=(),
        complete_when=_completed_capability("work.recover"),
    ) as rig:
        action, _ = await _authorize_one(rig, "work.recover", proposal_id="recover")
        await rig.ledger.start_attempt(action.action_id, attempt=1)
        rig.store.writer(action.action_id, 1).write_text(
            _fixture_outputs("work.recover")[0][0],
            "durable executor bytes",
            media_type="text/plain",
            evidence_role="fixture_result",
        )

        await rig.reconciler.reconcile(rig.run_id)

        assert rig.executors["work.recover"].attempt_ids == []
        assert await rig.ledger.get_attempt_outcome(action.action_id, 1)
        assert await rig.ledger.action_status(action.action_id) is ActionStatus.SUCCEEDED
        assert await rig.ledger.count_attempts(capability="work.recover") == 1


@pytest.mark.asyncio
async def test_reconciler_blocks_unsafe_missing_receipt_without_redispatch(
    tmp_path: Path,
) -> None:
    """Catch missing/extra staging being guessed into a success receipt."""
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "work.missing",
                (SuccessTemplate(),),
            ),
        ),
        patches=(),
        complete_when=_completed_capability("work.missing"),
    ) as rig:
        action, _ = await _authorize_one(rig, "work.missing", proposal_id="missing")
        await rig.ledger.start_attempt(action.action_id, attempt=1)

        await rig.reconciler.reconcile(rig.run_id)

        assert rig.executors["work.missing"].attempt_ids == []
        assert await rig.ledger.attempt_status(action.action_id, 1) is ActionStatus.REPAIR_REQUIRED
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        assert await rig.ledger.has_open_incident("artifact_bundle_conflict")


@pytest.mark.asyncio
async def test_reconciler_recovers_partial_committed_bundle_without_validator_or_executor(
    tmp_path: Path,
) -> None:
    """Catch recovery rerunning an Action/validator after gate+all-intents are durable."""
    promoted = 0

    def crash(point: str, detail: object) -> None:
        nonlocal promoted
        if point == "after_intent_promotion":
            promoted += 1
            if promoted == 1:
                raise BoundaryCrash(point)

    async with _controller_rig(
        tmp_path,
        definitions=(("work.multi", (SuccessTemplate(),)),),
        patches=(),
        committer_hook=crash,
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.multi")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id, action=action, snapshot=snapshot, attempt=1
        )
        assert isinstance(envelope.outcome, Succeeded)
        with pytest.raises(BoundaryCrash, match="after_intent_promotion"):
            await rig.committer.commit(
                run_id=rig.run_id,
                action=action,
                attempt=1,
                outcome=envelope.outcome,
            )
        _, intents = await rig.ledger.get_gate_receipt_and_intents(action.action_id, 1)
        assert tuple(item.status for item in intents) == ("COMMITTED", "PENDING")
        (tmp_path / intents[0].staged_relpath).unlink()

        clean_committer = Committer(
            ledger=rig.ledger,
            registry=rig.registry,
            project=BookProject(tmp_path),
            artifacts=rig.store,
        )
        await Reconciler(
            ledger=rig.ledger,
            artifacts=rig.store,
            registry=rig.registry,
            committer=clean_committer,
        ).reconcile(rig.run_id)

        assert rig.executors["work.multi"].attempt_ids == [1]
        assert await rig.ledger.action_status(action.action_id) is ActionStatus.SUCCEEDED
        _, recovered = await rig.ledger.get_gate_receipt_and_intents(action.action_id, 1)
        assert {item.status for item in recovered} == {"COMMITTED"}


@pytest.mark.asyncio
async def test_reconciler_compensates_post_success_drift(tmp_path: Path) -> None:
    """Catch a prior success suppressing later canonical bundle drift."""
    async with _controller_rig(
        tmp_path,
        definitions=(("work.multi", (SuccessTemplate(),)),),
        patches=(),
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.multi")
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id, action=action, snapshot=snapshot, attempt=1
        )
        assert isinstance(envelope.outcome, Succeeded)
        await rig.committer.commit(
            run_id=rig.run_id,
            action=action,
            attempt=1,
            outcome=envelope.outcome,
        )
        canonical = tmp_path / "reports/a.json"
        canonical.unlink()
        canonical.write_bytes(b"post-success drift")

        await rig.reconciler.reconcile(rig.run_id)

        assert await rig.ledger.action_status(action.action_id) is ActionStatus.REPAIR_REQUIRED
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        assert await rig.ledger.has_open_incident("post_success_drift")


@pytest.mark.asyncio
async def test_indeterminate_without_registered_probe_blocks_without_reexecution(
    tmp_path: Path,
) -> None:
    """Catch an uncertain side effect being blindly executed again."""
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "release.publish",
                (
                    Indeterminate(
                        operation_key="publish-1",
                        error_code="provider_timeout",
                        failure_signature=canonical_failure_signature(
                            "release.publish", "{}", "provider_timeout"
                        ),
                        message="remote result unknown",
                    ),
                ),
            ),
        ),
        patches=(_patch("publish", "release.publish"),),
    ) as rig:
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        assert await rig.ledger.count_attempts(capability="release.publish") == 1
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        assert await rig.ledger.has_open_incident("indeterminate_probe_missing")


@pytest.mark.asyncio
async def test_side_effecting_timeout_persists_indeterminate_and_dispatches_one_probe(
    tmp_path: Path,
) -> None:
    """A timeout cannot authorize a duplicate external side effect."""
    operation_outcome = SuccessTemplate()
    async with _controller_rig(
        tmp_path,
        definitions=(
            ("release.publish", (operation_outcome,)),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key="placeholder-replaced-below",
                        disposition="unknown",
                        evidence_refs=("provider:query-1",),
                        message="provider still cannot resolve the operation",
                    ),
                ),
            ),
        ),
        patches=(),
        spec_options={
            "release.publish": {
                "probe_capability": "release.probe",
                "retryable_codes": ("provider_timeout",),
                "may_have_side_effects": True,
                "delays_s": (0.05,),
            }
        },
        probe_capabilities=frozenset({"release.probe"}),
        timeout_s=0.001,
    ) as rig:
        action, snapshot = await _authorize_one(rig, "release.publish")
        invocation_json = json.dumps(
            {
                "action_id": action.action_id,
                "attempt": 1,
                "capability": action.capability,
                "parameters_json": action.parameters_json,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        expected_operation_key = f"operation:{sha256_canonical_json(invocation_json)}"
        rig.executors["release.probe"]._outcomes = deque(
            (
                ProbeResolution(
                    operation_key=expected_operation_key,
                    disposition="unknown",
                    evidence_refs=("provider:query-1",),
                    message="provider still cannot resolve the operation",
                ),
            )
        )

        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id, action=action, snapshot=snapshot, attempt=1
        )

        assert isinstance(envelope.outcome, Indeterminate)
        assert envelope.outcome.operation_key == expected_operation_key
        assert envelope.outcome.failure_signature == canonical_failure_signature(
            action.capability, action.parameters_json, "provider_timeout"
        )
        await rig.reconciler.reconcile(rig.run_id)
        await rig.controller.tick(rig.run_id)
        await rig.reconciler.reconcile(rig.run_id)

        actions = await rig.ledger.list_actions(rig.run_id)
        probes = tuple(item for item in actions if item.capability == "release.probe")
        assert len(probes) == 1
        assert rig.executors["release.publish"].attempt_ids == [1]
        assert await rig.ledger.attempt_numbers(action.action_id) == (1,)
        assert rig.executors["release.probe"].attempt_ids == [1]


@pytest.mark.asyncio
async def test_side_effect_free_timeout_uses_frozen_bounded_retry(tmp_path: Path) -> None:
    async with _controller_rig(
        tmp_path,
        definitions=(("work.timeout", (SuccessTemplate(),)),),
        patches=(),
        complete_when=_completed_capability("work.timeout"),
        spec_options={
            "work.timeout": {
                "retryable_codes": ("provider_timeout",),
                "max_attempts": 2,
                "delays_s": (0.05, 0.0),
            }
        },
        timeout_s=0.001,
    ) as rig:
        action, snapshot = await _authorize_one(rig, "work.timeout")
        first = await rig.dispatcher.execute(
            run_id=rig.run_id, action=action, snapshot=snapshot, attempt=1
        )
        assert isinstance(first.outcome, RetryableFailure)

        await rig.controller.tick(rig.run_id)
        await rig.controller.tick(rig.run_id)

        assert rig.executors["work.timeout"].attempt_ids == [1, 2]
        assert await rig.ledger.attempt_numbers(action.action_id) == (1, 2)
        assert await rig.ledger.action_status(action.action_id) is ActionStatus.SUCCEEDED


class _SecondBuildBarrier:
    """Test-only snapshot wrapper that aligns probe check/create windows."""

    def __init__(self, delegate: SnapshotBuilder, participants: int) -> None:
        self._delegate = delegate
        self._barrier = asyncio.Barrier(participants)
        self._calls_by_task: dict[asyncio.Task[object], int] = {}

    async def build(self, run_id: str) -> PlanningContext:
        task = asyncio.current_task()
        assert task is not None
        count = self._calls_by_task.get(task, 0) + 1
        self._calls_by_task[task] = count
        if count == 2:
            await self._barrier.wait()
        return await self._delegate.build(run_id)


@pytest.mark.asyncio
async def test_probe_binding_replay_ignores_newer_speculative_plan_identity(
    tmp_path: Path,
) -> None:
    operation_key = "publish:durable-binding-1"
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
                ),
            ),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key=operation_key,
                        disposition="unknown",
                        evidence_refs=("provider:query-1",),
                        message="remote result remains unknown",
                    ),
                ),
            ),
        ),
        patches=(),
        spec_options={"release.publish": {"probe_capability": "release.probe"}},
        probe_capabilities=frozenset({"release.probe"}),
    ) as rig:
        original, snapshot = await _authorize_one(rig, "release.publish")
        await rig.dispatcher.execute(
            run_id=rig.run_id, action=original, snapshot=snapshot, attempt=1
        )
        await rig.reconciler.reconcile(rig.run_id)
        binding = ProbeActionInput(
            original_action_id=original.action_id,
            original_attempt=1,
            operation_key=operation_key,
            probe_capability="release.probe",
        )

        plan2_context = await SnapshotBuilder(
            ledger=rig.ledger, registry=rig.registry
        ).build(rig.run_id)
        patch2 = _probe_patch("probe-stored", binding)
        decision2 = PolicyEngine(rig.registry).authorize(
            plan2_context.policy_snapshot, patch2, next_plan_version=2
        )
        assert decision2.authorized and len(decision2.actions) == 1
        stored, created = await rig.ledger.authorize_probe_action(
            rig.run_id,
            binding=binding,
            patch=patch2,
            action=decision2.actions[0],
            expected_previous_plan_version=1,
        )
        assert created
        assert stored.plan_version == 2

        plan3_context = await SnapshotBuilder(
            ledger=rig.ledger, registry=rig.registry
        ).build(rig.run_id)
        patch3 = _probe_patch("probe-speculative", binding)
        decision3 = PolicyEngine(rig.registry).authorize(
            plan3_context.policy_snapshot, patch3, next_plan_version=3
        )
        assert decision3.authorized and len(decision3.actions) == 1
        before_outbox = await rig.ledger._count(
            "SELECT COUNT(*) FROM event_outbox WHERE run_id = ?", (rig.run_id,)
        )

        replayed, replay_created = await rig.ledger.authorize_probe_action(
            rig.run_id,
            binding=binding,
            patch=patch3,
            action=decision3.actions[0],
            expected_previous_plan_version=2,
        )

        assert replayed.action_id == stored.action_id
        assert not replay_created
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM plan_versions WHERE run_id = ?", (rig.run_id,)
        ) == 2
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM actions WHERE run_id = ?", (rig.run_id,)
        ) == 2
        assert await rig.ledger._count("SELECT COUNT(*) FROM probe_bindings", ()) == 1
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM incidents WHERE run_id = ?", (rig.run_id,)
        ) == 0
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM event_outbox WHERE run_id = ?", (rig.run_id,)
        ) == before_outbox
        assert await rig.ledger.action_status(original.action_id) is ActionStatus.INDETERMINATE
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict_field", ("operation_key", "probe_capability"))
async def test_probe_binding_replay_blocks_conflicting_durable_binding(
    tmp_path: Path,
    conflict_field: str,
) -> None:
    operation_key = "publish:durable-binding-conflict"
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
                ),
            ),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key=operation_key,
                        disposition="unknown",
                        evidence_refs=("provider:query-1",),
                        message="remote result remains unknown",
                    ),
                ),
            ),
        ),
        patches=(),
        spec_options={"release.publish": {"probe_capability": "release.probe"}},
        probe_capabilities=frozenset({"release.probe"}),
    ) as rig:
        original, snapshot = await _authorize_one(rig, "release.publish")
        await rig.dispatcher.execute(
            run_id=rig.run_id, action=original, snapshot=snapshot, attempt=1
        )
        await rig.reconciler.reconcile(rig.run_id)
        binding = ProbeActionInput(
            original_action_id=original.action_id,
            original_attempt=1,
            operation_key=operation_key,
            probe_capability="release.probe",
        )
        context = await SnapshotBuilder(
            ledger=rig.ledger, registry=rig.registry
        ).build(rig.run_id)
        patch = _probe_patch("probe-stored", binding)
        decision = PolicyEngine(rig.registry).authorize(
            context.policy_snapshot, patch, next_plan_version=2
        )
        assert decision.authorized and len(decision.actions) == 1
        await rig.ledger.authorize_probe_action(
            rig.run_id,
            binding=binding,
            patch=patch,
            action=decision.actions[0],
            expected_previous_plan_version=1,
        )
        conflicting = binding.model_copy(
            update={
                conflict_field: (
                    "publish:different-operation"
                    if conflict_field == "operation_key"
                    else "release.different-probe"
                )
            }
        )

        with pytest.raises(LedgerConflictError, match="probe binding conflicts"):
            await rig.ledger.authorize_probe_action(
                rig.run_id,
                binding=conflicting,
                patch=patch,
                action=decision.actions[0],
                expected_previous_plan_version=2,
            )

        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        assert await rig.ledger.action_status(original.action_id) is ActionStatus.REPAIR_REQUIRED
        assert await rig.ledger.has_open_incident("probe_binding_conflict")
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM plan_versions WHERE run_id = ?", (rig.run_id,)
        ) == 2
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM actions WHERE run_id = ?", (rig.run_id,)
        ) == 2


@pytest.mark.asyncio
async def test_concurrent_ticks_authorize_exactly_one_durable_probe_binding(
    tmp_path: Path,
) -> None:
    operation_key = "publish:concurrent-1"
    participants = 8
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
                ),
            ),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key=operation_key,
                        disposition="unknown",
                        evidence_refs=("provider:query-1",),
                        message="remote result remains unknown",
                    ),
                ),
            ),
        ),
        patches=(),
        spec_options={"release.publish": {"probe_capability": "release.probe"}},
        probe_capabilities=frozenset({"release.probe"}),
    ) as rig:
        original, snapshot = await _authorize_one(rig, "release.publish")
        await rig.dispatcher.execute(
            run_id=rig.run_id, action=original, snapshot=snapshot, attempt=1
        )
        await rig.reconciler.reconcile(rig.run_id)
        rig.controller._snapshots = _SecondBuildBarrier(  # type: ignore[assignment]
            SnapshotBuilder(ledger=rig.ledger, registry=rig.registry), participants
        )

        results = await asyncio.gather(
            *(rig.controller.tick(rig.run_id) for _ in range(participants)),
            return_exceptions=True,
        )

        actions = await rig.ledger.list_actions(rig.run_id)
        probes = tuple(item for item in actions if item.capability == "release.probe")
        errors = tuple(
            (type(item).__name__, str(item))
            for item in results
            if isinstance(item, BaseException)
        )
        durable = await rig.ledger.load_snapshot(rig.run_id)
        probe_attempt_count = 0
        for probe in probes:
            probe_attempt_count += len(await rig.ledger.attempt_numbers(probe.action_id))
        observed = (
            errors,
            len(probes),
            durable.plan_version,
            await rig.ledger.attempt_numbers(original.action_id),
            probe_attempt_count,
            tuple(rig.executors["release.publish"].attempt_ids),
            tuple(rig.executors["release.probe"].attempt_ids),
        )
        assert observed == ((), 1, 2, (1,), 1, (1,), (1,))
        binding_count = await rig.ledger._count(
            "SELECT COUNT(*) FROM probe_bindings", ()
        )
        plan_count = await rig.ledger._count(
            "SELECT COUNT(*) FROM plan_versions WHERE run_id = ?", (rig.run_id,)
        )
        action_count = await rig.ledger._count(
            "SELECT COUNT(*) FROM actions WHERE run_id = ?", (rig.run_id,)
        )
        outbox_rows = await rig.ledger._fetch_all(
            "SELECT event_name, COUNT(*) AS event_count FROM event_outbox "
            "WHERE run_id = ? GROUP BY event_name ORDER BY event_name",
            (rig.run_id,),
        )
        outbox_counts = {
            row["event_name"]: row["event_count"] for row in outbox_rows
        }
        assert (binding_count, plan_count, action_count) == (1, 2, 2)
        assert outbox_counts == {
            "action.authorized": 2,
            "action.committed": 1,
            "action.outcome": 2,
            "action.resolved": 1,
            "action.started": 2,
            "incident.created": 1,
            "plan.authorized": 2,
            "plan.proposed": 2,
        }
        assert await rig.ledger.get_attempt_outcome(probes[0].action_id, 1)

        for _ in range(3):
            assert not await rig.controller.tick(rig.run_id)
        assert await rig.ledger._count("SELECT COUNT(*) FROM probe_bindings", ()) == 1
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM plan_versions WHERE run_id = ?", (rig.run_id,)
        ) == 2
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM actions WHERE run_id = ?", (rig.run_id,)
        ) == 2
        assert rig.executors["release.publish"].attempt_ids == [1]
        assert rig.executors["release.probe"].attempt_ids == [1]

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "probe_outcome",
    (
        ProbeResolution(
            operation_key="wrong-operation",
            disposition="unknown",
            evidence_refs=("external:wrong",),
            message="mismatched operation",
        ),
        SuccessTemplate(),
    ),
    ids=("mismatched-operation", "ordinary-success"),
)
async def test_bound_probe_rejects_wrong_resolution_shape_as_integrity(
    tmp_path: Path, probe_outcome: PlannedOutcome
) -> None:
    operation_key = "publish:external-1"
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
                ),
            ),
            ("release.probe", (probe_outcome,)),
        ),
        patches=(_patch("publish", "release.publish"),),
        spec_options={"release.publish": {"probe_capability": "release.probe"}},
        probe_capabilities=frozenset({"release.probe"}),
    ) as rig:
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        actions = await rig.ledger.list_actions(rig.run_id)
        original = next(item for item in actions if item.capability == "release.publish")
        probe = next(item for item in actions if item.capability == "release.probe")
        assert await rig.ledger.action_status(original.action_id) is ActionStatus.INDETERMINATE
        assert await rig.ledger.action_status(probe.action_id) is ActionStatus.REPAIR_REQUIRED
        assert (await rig.ledger.get_action(probe.action_id)).reason_code == (
            "probe_resolution_conflict"
        )
        assert await rig.ledger.has_open_incident("probe_resolution_conflict")
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        classified_receipt = await rig.ledger.get_attempt_outcome(probe.action_id, 1)
        classified = ActionOutcomeEnvelope.model_validate_json(
            classified_receipt.canonical_outcome_json
        )
        assert isinstance(classified.outcome, RepairRequired)
        assert classified.outcome.reason_code == "probe_resolution_conflict"
        assert rig.executors["release.publish"].attempt_ids == [1]
        assert rig.executors["release.probe"].attempt_ids == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "disposition",
        "original_outcomes",
        "expected_original_attempts",
        "expected_run_status",
    ),
    (
        ("succeeded", (None,), (1,), RunStatus.COMPLETED),
        ("absent", (None, SuccessTemplate()), (1, 2), RunStatus.COMPLETED),
        ("unknown", (None,), (1,), RunStatus.BLOCKED),
    ),
)
async def test_indeterminate_runtime_dispatches_one_bound_probe_without_blind_reissue(
    tmp_path: Path,
    disposition: str,
    original_outcomes: tuple[SuccessTemplate | None, ...],
    expected_original_attempts: tuple[int, ...],
    expected_run_status: RunStatus,
) -> None:
    operation_key = "publish:external-1"
    indeterminate = Indeterminate(
        operation_key=operation_key,
        error_code="provider_timeout",
        failure_signature=canonical_failure_signature(
            "release.publish", "{}", "provider_timeout"
        ),
        message="remote result unknown",
    )
    planned_original: tuple[PlannedOutcome, ...] = tuple(
        indeterminate if item is None else item for item in original_outcomes
    )
    async with _controller_rig(
        tmp_path,
        definitions=(
            ("release.publish", planned_original),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key=operation_key,
                        disposition=disposition,
                        evidence_refs=("external:release-1",),
                        message="read-only provider evidence",
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
    ) as rig:
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        actions = await rig.ledger.list_actions(rig.run_id)
        original = next(item for item in actions if item.capability == "release.publish")
        probes = tuple(item for item in actions if item.capability == "release.probe")
        assert len(probes) == 1
        probe = probes[0]
        assert ProbeActionInput.model_validate_json(probe.parameters_json) == ProbeActionInput(
            original_action_id=original.action_id,
            original_attempt=1,
            operation_key=operation_key,
            probe_capability="release.probe",
        )
        assert tuple(rig.executors["release.publish"].attempt_ids) == (
            expected_original_attempts
        )
        assert rig.executors["release.probe"].attempt_ids == [1]
        assert await rig.ledger.attempt_numbers(original.action_id) == (
            expected_original_attempts
        )
        assert await rig.ledger.attempt_numbers(probe.action_id) == (1,)
        assert await rig.ledger.action_status(probe.action_id) is ActionStatus.SUCCEEDED
        resolution = await rig.ledger.get_probe_resolution(original.action_id, 1)
        assert resolution.disposition == disposition
        assert (await rig.ledger.get_run(rig.run_id)).status is expected_run_status
        if disposition == "absent":
            assert await rig.ledger.attempt_status(
                original.action_id, 1
            ) is ActionStatus.RETRY_WAIT

        calls_before_resume = (
            tuple(rig.executors["release.publish"].attempt_ids),
            tuple(rig.executors["release.probe"].attempt_ids),
        )
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
        assert (
            tuple(rig.executors["release.publish"].attempt_ids),
            tuple(rig.executors["release.probe"].attempt_ids),
        ) == calls_before_resume


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "status"),
    (("budget", RunStatus.PAUSED_BUDGET), ("hitl", RunStatus.PAUSED_HITL)),
)
async def test_paused_outcome_maps_exactly_to_run_pause(
    tmp_path: Path, reason: str, status: RunStatus
) -> None:
    """Catch budget and HITL pause reasons collapsing into a generic failure."""
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "work.pause",
                (Paused(reason=reason, message="wait for external input"),),
            ),
        ),
        patches=(_patch("pause", "work.pause"),),
    ) as rig:
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        assert (await rig.ledger.get_run(rig.run_id)).status is status
        assert await rig.ledger.count_attempts(capability="work.pause") == 1


@pytest.mark.asyncio
async def test_max_cycles_creates_incident_and_blocks(tmp_path: Path) -> None:
    """Catch bounded runtime exhaustion being mistaken for normal completion."""
    async with _controller_rig(
        tmp_path,
        definitions=(("work.never", (PermanentFailure(error_code="unused", message="unused"),)),),
        patches=(),
        max_cycles=2,
    ) as rig:
        await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)

        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        assert await rig.ledger.has_open_incident("controller_max_cycles_exhausted")
        assert await rig.ledger.count_attempts() == 0


@pytest.mark.asyncio
async def test_event_logger_refuses_duplicate_outbox_event_after_restart(
    tmp_path: Path,
) -> None:
    """Catch the crash window after JSONL append but before outbox delivery marking."""
    events_path = tmp_path / "events.jsonl"
    first = EventLogger(events_path, "run-1")
    assert first.append_record("event-1", {"event": "action.committed", "value": 1})
    restarted = EventLogger(events_path, "run-1")

    assert not restarted.append_record(
        "event-1", {"event": "action.committed", "value": 1}
    )
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 1
