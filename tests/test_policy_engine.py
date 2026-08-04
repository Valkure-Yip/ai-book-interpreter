"""Behavioral tests for deterministic plan authorization."""

from __future__ import annotations

import pytest

from abi.actions.contracts import ActionDefinition
from abi.actions.predicates import PredicateCatalog
from abi.actions.registry import ActionRegistry
from abi.planning.policy import PolicyEngine
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionArgument,
    ActionKind,
    ActionSpec,
    EligibleAction,
    GateDecision,
    GateEvidence,
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


async def _execute_unused(context: object, parameters: FrozenModel) -> object:
    raise AssertionError("policy tests must not execute actions")


def _validate_unused(project: object, parameters: FrozenModel) -> GateDecision:
    return GateDecision(passed=True, reason_code="ok", message="valid")


def _definition(
    capability: str,
    *,
    input_model: type[FrozenModel] = EmptyInput,
    prerequisites: tuple[PredicateSpec, ...] = (),
    write_set: tuple[str, ...] = (),
    read_set: tuple[str, ...] = (),
    estimated_cost_usd: float = 0.0,
    expected_evidence: tuple[str, ...] = (),
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


def _snapshot(*, budget: float | None = 10.0) -> RunSnapshot:
    return RunSnapshot(
        run_id="run-1",
        status=RunStatus.RUNNING,
        plan_version=1,
        eligible_actions=(
            EligibleAction(
                capability="source.ingest",
                description="Ingest source.",
                input_schema="SourceIngestInput",
                estimated_cost_usd=1.0,
            ),
            EligibleAction(
                capability="chapter.first",
                description="Write chapters.",
                input_schema="EmptyInput",
                estimated_cost_usd=0.0,
            ),
            EligibleAction(
                capability="chapter.second",
                description="Write chapters.",
                input_schema="EmptyInput",
                estimated_cost_usd=0.0,
            ),
            EligibleAction(
                capability="release.publish",
                description="Publish release.",
                input_schema="EmptyInput",
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


def test_policy_rechecks_hard_predicates_and_terminal_release_evidence() -> None:
    """Catch plans that bypass fresh prerequisites or publish before terminal gates pass."""
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
    assert release.reason_codes == ("terminal_release_policy",)


def test_policy_allows_release_only_after_all_required_gates_pass() -> None:
    """Catch a terminal policy that accepts missing or failed required release evidence."""
    snapshot = _snapshot().model_copy(
        update={
            "gate_evidence": (
                GateEvidence(
                    evidence_id="e1",
                    gate="epubcheck",
                    passed=True,
                    validator_version="1",
                ),
                GateEvidence(
                    evidence_id="e2",
                    gate="spotcheck",
                    passed=True,
                    validator_version="1",
                ),
            )
        }
    )

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
