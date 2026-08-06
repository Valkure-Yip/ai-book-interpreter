"""Behavioral tests for deterministic plan authorization."""

from __future__ import annotations

import json

import pytest

from abi.actions.builtins.catalog import build_action_registry
from abi.actions.builtins.inputs import ChapterBatchInput
from abi.actions.contracts import AccessExpander, ActionAccess, ActionDefinition
from abi.actions.predicates import PredicateCatalog
from abi.actions.registry import ActionRegistry
from abi.planning.policy import PolicyEngine
from abi.planning.scheduler import Scheduler
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionArgument,
    ActionKind,
    ActionSpec,
    ActionStatus,
    ActionView,
    EligibleAction,
    ExpectedArtifact,
    ExpectedArtifactManifest,
    GateDecision,
    IncidentView,
    PlanPatch,
    PredicateSpec,
    ProposedAction,
    RunSnapshot,
    RunStatus,
)


class SourceIngestInput(FrozenModel):
    source_relpath: str


class EmptyInput(FrozenModel):
    pass


def _input_schema(model: type[FrozenModel]) -> str:
    return json.dumps(model.model_json_schema(), sort_keys=True, separators=(",", ":"))


async def _execute_unused(context: object, parameters: FrozenModel) -> object:
    raise AssertionError("policy tests must not execute actions")


def _validate_unused(view: object, parameters: FrozenModel, bundle: object) -> GateDecision:
    return GateDecision(
        passed=True,
        reason_code="ok",
        message="valid",
        validator_id="unused",
        validator_version="1",
        bundle_digest="0" * 64,
        artifact_checksums=("0" * 64,),
    )


def _expand(
    capability: str, action_id: str, parameters: FrozenModel
) -> ExpectedArtifactManifest:
    return ExpectedArtifactManifest(
        action_id=action_id,
        entries=(
            ExpectedArtifact(
                canonical_relpath=f"effects/{capability.replace('.', '-')}.json",
                media_type="application/json",
                evidence_role="policy_fixture",
            ),
        ),
    )


def _definition(
    capability: str,
    *,
    input_model: type[FrozenModel] = EmptyInput,
    prerequisites: tuple[PredicateSpec, ...] = (),
    write_set: tuple[str, ...] = (),
    read_set: tuple[str, ...] = (),
    estimated_cost_usd: float = 0.0,
    expected_evidence: tuple[str, ...] = (),
    access_expander: AccessExpander | None = None,
) -> ActionDefinition:
    return ActionDefinition(
        spec=ActionSpec(
            capability=capability,
            description=f"Run {capability}.",
            input_schema=input_model.__name__,
            action_kind=ActionKind.DETERMINISTIC,
            prerequisites=prerequisites,
            expected_evidence=tuple({"name": name} for name in expected_evidence),
            validator="source_manifest",
            read_set=read_set,
            write_set=write_set,
            estimated_cost_usd=estimated_cost_usd,
        ),
        input_model=input_model,
        executor=_execute_unused,
        validator=_validate_unused,
        effect_expander=_expand,
        access_expander=access_expander,
    )


def _registry() -> ActionRegistry:
    predicates = PredicateCatalog({"always": lambda snapshot, arguments: True})
    registry = ActionRegistry(
        predicates=predicates, validators={"source_manifest": _validate_unused}
    )
    registry.register(
        _definition(
            "source.ingest",
            input_model=SourceIngestInput,
            write_set=("source",),
            estimated_cost_usd=1.0,
        )
    )
    registry.register(_definition("chapter.first", write_set=("chapters",)))
    registry.register(_definition("chapter.second", write_set=("chapters",)))
    registry.register(
        _definition(
            "release.publish",
            expected_evidence=("epubcheck", "spotcheck"),
        )
    )
    return registry


def _repair_registry() -> ActionRegistry:
    registry = ActionRegistry(
        predicates=PredicateCatalog(),
        validators={"source_manifest": _validate_unused},
        semantic_repair_mappings=(("term_drift", "repair.glossary"),),
    )
    registry.register(_definition("repair.glossary"))
    registry.register(_definition("work.other"))
    registry.validate_startup()
    return registry


def _repair_snapshot(*, repair_class: str, reason_code: str) -> RunSnapshot:
    return RunSnapshot(
        run_id="run-1",
        status=RunStatus.RUNNING,
        plan_version=1,
        actions=(
            ActionView(
                action_id="old",
                capability="work.initial",
                status=ActionStatus.REPAIR_REQUIRED,
                repair_class=repair_class,
                repair_source=(
                    "action_outcome" if repair_class == "semantic" else "integrity_guard"
                ),
                reason_code=reason_code,
            ),
        ),
        incidents=(
            IncidentView(
                incident_id="repair:old:1",
                error_code=reason_code,
                message="repair evidence",
                action_id="old",
                repair_class=repair_class,
                repair_source=(
                    "action_outcome" if repair_class == "semantic" else "integrity_guard"
                ),
                reason_code=reason_code,
            ),
        ),
        eligible_actions=(
            EligibleAction(
                capability="repair.glossary",
                description="repair",
                input_schema=_input_schema(EmptyInput),
                estimated_cost_usd=0,
            ),
            EligibleAction(
                capability="work.other",
                description="other",
                input_schema=_input_schema(EmptyInput),
                estimated_cost_usd=0,
            ),
        ),
    )


def test_policy_authorizes_only_registry_mapped_semantic_repair() -> None:
    """Catch a semantic repair authorizing an unrelated replacement capability."""
    policy = PolicyEngine(_repair_registry())
    snapshot = _repair_snapshot(repair_class="semantic", reason_code="term_drift")

    mapped = policy.authorize(
        snapshot,
        PlanPatch(
            objective="repair terminology",
            proposed_actions=(
                ProposedAction(proposal_id="repair", capability="repair.glossary"),
            ),
            rationale="use the exact mapped repair",
        ),
        next_plan_version=2,
    )
    unrelated = policy.authorize(
        snapshot,
        PlanPatch(
            objective="do unrelated work",
            proposed_actions=(
                ProposedAction(proposal_id="other", capability="work.other"),
            ),
            rationale="must not bypass the repair mapping",
        ),
        next_plan_version=2,
    )

    assert mapped.authorized
    assert mapped.actions[0].repairs_incident_ids == ("repair:old:1",)
    assert unrelated.reason_codes == ("semantic_repair_capability_mismatch",)


def test_policy_rejects_replaying_current_success_but_allows_bound_repair() -> None:
    ordinary_registry = _registry()
    ordinary = RunSnapshot(
        run_id="run-1",
        status=RunStatus.RUNNING,
        actions=(
            ActionView(
                action_id="source-v1",
                capability="source.ingest",
                status=ActionStatus.SUCCEEDED,
                outputs_current=True,
            ),
        ),
        eligible_actions=(
            EligibleAction(
                capability="source.ingest",
                description="source",
                input_schema=_input_schema(SourceIngestInput),
                estimated_cost_usd=1,
            ),
        ),
    )
    duplicate = PolicyEngine(ordinary_registry).authorize(
        ordinary,
        PlanPatch(
            objective="repeat completed source",
            proposed_actions=(
                ProposedAction(
                    proposal_id="source-v2",
                    capability="source.ingest",
                    arguments=(
                        ActionArgument(
                            name="source_relpath", value_json='"source/raw.txt"'
                        ),
                    ),
                ),
            ),
            rationale="planner must not replay current work",
        ),
        next_plan_version=1,
    )
    assert duplicate.reason_codes == ("capability_already_succeeded",)

    repair_registry = _repair_registry()
    repair = _repair_snapshot(
        repair_class="semantic", reason_code="term_drift"
    ).model_copy(
        update={
            "actions": (
                *_repair_snapshot(
                    repair_class="semantic", reason_code="term_drift"
                ).actions,
                ActionView(
                    action_id="glossary-v1",
                    capability="repair.glossary",
                    status=ActionStatus.SUCCEEDED,
                    outputs_current=True,
                ),
            )
        }
    )
    replacement = PolicyEngine(repair_registry).authorize(
        repair,
        PlanPatch(
            objective="replace current glossary",
            proposed_actions=(
                ProposedAction(proposal_id="glossary-v2", capability="repair.glossary"),
            ),
            rationale="open semantic incident grants exact replacement authority",
        ),
        next_plan_version=2,
    )
    assert replacement.authorized


def test_policy_blocks_every_plan_when_integrity_incident_is_open() -> None:
    """Catch Planner authorization while durable integrity evidence is unresolved."""
    policy = PolicyEngine(_repair_registry())
    snapshot = _repair_snapshot(
        repair_class="integrity", reason_code="artifact_identity_conflict"
    )
    decision = policy.authorize(
        snapshot,
        PlanPatch(
            objective="attempt automatic repair",
            proposed_actions=(
                ProposedAction(proposal_id="repair", capability="repair.glossary"),
            ),
            rationale="must remain blocked",
        ),
        next_plan_version=2,
    )

    assert not decision.authorized
    assert decision.reason_codes == ("integrity_incident_open",)


def test_real_chapter_parameters_drive_parallel_policy_and_scheduler_access_sets() -> None:
    """Catch production chapter batches retaining capability-wide directory locks."""
    registry = build_action_registry()
    snapshot = RunSnapshot(
        run_id="run-1",
        status=RunStatus.RUNNING,
        actions=(
            ActionView(
                action_id="glossary",
                capability="glossary.prepare",
                status=ActionStatus.SUCCEEDED,
                outputs_current=True,
            ),
        ),
        eligible_actions=(
            EligibleAction(
                capability="chapter.translate",
                description="translate",
                input_schema=_input_schema(ChapterBatchInput),
                estimated_cost_usd=1.5,
            ),
        ),
    )
    patch = PlanPatch(
        objective="translate independent chapters",
        proposed_actions=tuple(
            ProposedAction(
                proposal_id=f"chapter-{chapter}",
                capability="chapter.translate",
                arguments=(
                    ActionArgument(name="chapters", value_json=json.dumps([chapter])),
                ),
            )
            for chapter in ("001", "002")
        ),
        rationale="chapters have disjoint sources and outputs",
    )

    decision = PolicyEngine(registry).authorize(snapshot, patch, next_plan_version=1)

    assert decision.authorized, decision.reason_codes
    assert tuple(action.read_set for action in decision.actions) == (
        ("chapters/src/001.md", "glossary"),
        ("chapters/src/002.md", "glossary"),
    )
    assert tuple(action.write_set for action in decision.actions) == (
        ("chapters/translated/001.md",),
        ("chapters/translated/002.md",),
    )
    scheduled = Scheduler(max_parallel=4).select_batch(decision.actions)
    assert tuple(action.proposal_id for action in scheduled) == ("chapter-001", "chapter-002")


def test_policy_fails_closed_when_parameterized_access_expansion_is_invalid() -> None:
    def invalid_access(capability: str, parameters: FrozenModel) -> ActionAccess:
        return ActionAccess(write_set=("../secret",))

    registry = ActionRegistry(
        predicates=PredicateCatalog(),
        validators={"source_manifest": _validate_unused},
    )
    registry.register(_definition("work.invalid", access_expander=invalid_access))
    registry.validate_startup()
    snapshot = RunSnapshot(
        run_id="run-1",
        status=RunStatus.RUNNING,
        eligible_actions=(
            EligibleAction(
                capability="work.invalid",
                description="invalid dynamic access",
                input_schema=_input_schema(EmptyInput),
                estimated_cost_usd=0,
            ),
        ),
    )

    decision = PolicyEngine(registry).authorize(
        snapshot,
        PlanPatch(
            objective="reject malformed authority",
            proposed_actions=(
                ProposedAction(proposal_id="invalid", capability="work.invalid"),
            ),
            rationale="expanded access must be validated before authorization",
        ),
        next_plan_version=1,
    )

    assert not decision.authorized
    assert decision.reason_codes == ("invalid_arguments",)


def _snapshot(*, budget: float | None = 10.0) -> RunSnapshot:
    return RunSnapshot(
        run_id="run-1",
        status=RunStatus.RUNNING,
        plan_version=1,
        eligible_actions=(
            EligibleAction(
                capability="source.ingest",
                description="Ingest source.",
                input_schema=_input_schema(SourceIngestInput),
                estimated_cost_usd=1.0,
            ),
            EligibleAction(
                capability="chapter.first",
                description="Write chapters.",
                input_schema=_input_schema(EmptyInput),
                estimated_cost_usd=0.0,
            ),
            EligibleAction(
                capability="chapter.second",
                description="Write chapters.",
                input_schema=_input_schema(EmptyInput),
                estimated_cost_usd=0.0,
            ),
            EligibleAction(
                capability="release.publish",
                description="Publish release.",
                input_schema=_input_schema(EmptyInput),
                estimated_cost_usd=0.0,
            ),
        ),
        remaining_budget_usd=budget,
    )


def _patch(*proposed_actions: ProposedAction) -> PlanPatch:
    return PlanPatch(
        objective="advance the book",
        proposed_actions=proposed_actions,
        rationale="deterministic policy will verify this proposal",
    )


def _proposal(
    proposal_id: str,
    capability: str,
    *,
    arguments: tuple[ActionArgument, ...] = (),
    dependencies: tuple[str, ...] = (),
) -> ProposedAction:
    return ProposedAction(
        proposal_id=proposal_id,
        capability=capability,
        arguments=arguments,
        dependencies=dependencies,
    )


def test_policy_rejects_unknown_capability_cycle_and_write_conflict() -> None:
    """Catch a policy that partially accepts a plan containing unsafe proposals."""
    policy = PolicyEngine(_registry())
    illegal_patch = _patch(
        _proposal("unknown", "unknown.action"),
        _proposal("first", "chapter.first", dependencies=("second",)),
        _proposal("second", "chapter.second", dependencies=("first",)),
    )

    decision = policy.authorize(_snapshot(), illegal_patch, next_plan_version=2)

    assert decision.authorized is False
    assert decision.actions == ()
    assert set(decision.reason_codes) == {
        "unknown_capability",
        "dependency_cycle",
        "write_conflict",
    }


def test_policy_emits_only_canonical_validated_parameters() -> None:
    """Catch authorization that persists planner transport JSON instead of parsed input."""
    policy = PolicyEngine(_registry())
    patch = _patch(
        _proposal(
            "ingest",
            "source.ingest",
            arguments=(
                ActionArgument(name="source_relpath", value_json='"source/raw.txt"'),
            ),
        )
    )

    decision = policy.authorize(_snapshot(), patch, next_plan_version=2)

    assert decision.authorized is True
    assert decision.reason_codes == ()
    assert decision.actions[0].parameters_json == '{"source_relpath":"source/raw.txt"}'
    assert decision.actions[0].idempotency_key == "run-1:2:ingest"


@pytest.mark.parametrize("next_plan_version", (0, 1, 3))
def test_policy_rejects_stale_equal_or_skipped_plan_versions(
    next_plan_version: int,
) -> None:
    """Catch replayed or skipped plan versions that would reuse commit identities."""
    patch = _patch(
        _proposal(
            "ingest",
            "source.ingest",
            arguments=(
                ActionArgument(name="source_relpath", value_json='"source/raw.txt"'),
            ),
        )
    )

    decision = PolicyEngine(_registry()).authorize(
        _snapshot(), patch, next_plan_version=next_plan_version
    )

    assert decision.authorized is False
    assert decision.actions == ()
    assert decision.reason_codes == ("invalid_plan_version",)


@pytest.mark.parametrize(
    ("patch", "snapshot", "reason_code"),
    [
        (_patch(), _snapshot(), "invalid_horizon"),
        (
            _patch(_proposal("same", "chapter.first"), _proposal("same", "chapter.second")),
            _snapshot(),
            "duplicate_proposal_id",
        ),
        (
            _patch(_proposal("bad-args", "source.ingest")),
            _snapshot(),
            "invalid_arguments",
        ),
        (
            _patch(_proposal("dependent", "chapter.first", dependencies=("missing",))),
            _snapshot(),
            "unknown_dependency",
        ),
        (
            _patch(
                _proposal(
                    "ingest",
                    "source.ingest",
                    arguments=(
                        ActionArgument(
                            name="source_relpath", value_json='"source/raw.txt"'
                        ),
                    ),
                )
            ),
            _snapshot(budget=0.0),
            "budget_exceeded",
        ),
    ],
)
def test_policy_rejects_each_independent_invalid_plan_shape(
    patch: PlanPatch, snapshot: RunSnapshot, reason_code: str
) -> None:
    """Catch removal of each plan-shape guard before dispatch can see it."""
    decision = PolicyEngine(_registry()).authorize(snapshot, patch, next_plan_version=2)

    assert decision.authorized is False
    assert reason_code in decision.reason_codes


def test_policy_rechecks_hard_predicates_without_preconsuming_action_outputs() -> None:
    """Prerequisites are authoritative; an Action's own outputs are post-execution facts."""
    registry = _registry()
    registry = ActionRegistry(
        predicates=PredicateCatalog({"never": lambda snapshot, arguments: False}),
        validators={"source_manifest": _validate_unused},
    )
    registry.register(
        _definition(
            "source.ingest",
            input_model=SourceIngestInput,
            prerequisites=(PredicateSpec(name="never"),),
        )
    )
    registry.register(
        _definition("release.publish", expected_evidence=("epubcheck", "spotcheck"))
    )
    snapshot = _snapshot()
    policy = PolicyEngine(registry)

    hard_prerequisite = policy.authorize(
        snapshot,
        _patch(
            _proposal(
                "ingest",
                "source.ingest",
                arguments=(ActionArgument(name="source_relpath", value_json='"source/raw.txt"'),),
            )
        ),
        next_plan_version=2,
    )
    release = policy.authorize(
        snapshot,
        _patch(_proposal("release", "release.publish")),
        next_plan_version=2,
    )

    assert hard_prerequisite.reason_codes == ("hard_prerequisite_failed",)
    assert release.authorized is True


def test_policy_does_not_treat_release_expected_outputs_as_prerequisite_gates() -> None:
    """Release expected evidence is validated after execution, not required before it."""
    snapshot = _snapshot()

    decision = PolicyEngine(_registry()).authorize(
        snapshot,
        _patch(_proposal("release", "release.publish")),
        next_plan_version=2,
    )

    assert decision.authorized is True


def test_policy_rejects_a_repeated_failure_signature() -> None:
    """Catch a plan that retries the exact prior failed action without new evidence."""
    snapshot = _snapshot().model_copy(
        update={"failure_signatures": ('source.ingest:{"source_relpath":"source/raw.txt"}',)}
    )

    decision = PolicyEngine(_registry()).authorize(
        snapshot,
        _patch(
            _proposal(
                "ingest",
                "source.ingest",
                arguments=(ActionArgument(name="source_relpath", value_json='"source/raw.txt"'),),
            )
        ),
        next_plan_version=2,
    )

    assert decision.reason_codes == ("repeated_failure_signature",)
