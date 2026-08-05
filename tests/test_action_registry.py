"""Behavioral tests for the fail-closed action registry."""

from __future__ import annotations

from dataclasses import replace

import pytest

from abi.actions.contracts import ActionDefinition
from abi.actions.predicates import PredicateCatalog
from abi.actions.registry import ActionRegistry, RegistryConfigurationError
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionArgument,
    ActionKind,
    ActionSpec,
    ExpectedArtifactManifest,
    GateDecision,
    RunSnapshot,
    RunStatus,
)


class SourceIngestInput(FrozenModel):
    source_relpath: str


async def _execute_unused(context: object, parameters: FrozenModel) -> object:
    raise AssertionError("registry tests must not execute actions")


def _expand_unused(
    capability: str, action_id: str, parameters: FrozenModel
) -> ExpectedArtifactManifest:
    return ExpectedArtifactManifest(action_id=action_id)


def _validate_unused(project: object, parameters: FrozenModel) -> GateDecision:
    return GateDecision(passed=True, reason_code="ok", message="valid")


def _different_validator(project: object, parameters: FrozenModel) -> GateDecision:
    return GateDecision(passed=True, reason_code="different", message="valid")


def _definition(
    capability: str = "source.ingest", *, validator: str = "source_manifest"
) -> ActionDefinition:
    return ActionDefinition(
        spec=ActionSpec(
            capability=capability,
            description="Ingest a source document.",
            input_schema="SourceIngestInput",
            action_kind=ActionKind.DETERMINISTIC,
            validator=validator,
            write_set=("source",),
        ),
        input_model=SourceIngestInput,
        executor=_execute_unused,
        validator=_validate_unused,
        effect_expander=_expand_unused,
    )


def _snapshot() -> RunSnapshot:
    return RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)


def _registry() -> ActionRegistry:
    registry = ActionRegistry(
        predicates=PredicateCatalog(), validators={"source_manifest": _validate_unused}
    )
    registry.register(_definition())
    return registry


def test_registry_rejects_missing_validator_and_duplicate_capability() -> None:
    """Catch startup configurations that could execute an unvalidated capability."""
    missing_validator = ActionRegistry(predicates=PredicateCatalog(), validators={})

    with pytest.raises(RegistryConfigurationError, match="register validator"):
        missing_validator.register(_definition())

    registry = _registry()
    with pytest.raises(RegistryConfigurationError, match="duplicate capability"):
        registry.register(_definition())


@pytest.mark.parametrize(
    "validator",
    (
        _different_validator,
        object(),
    ),
)
def test_registry_rejects_unregistered_or_non_callable_validator_bindings(
    validator: object,
) -> None:
    """Catch an ActionDefinition that could execute a validator outside the registry."""
    registry = ActionRegistry(
        predicates=PredicateCatalog(), validators={"source_manifest": _validate_unused}
    )

    with pytest.raises(RegistryConfigurationError, match="validator binding"):
        registry.register(replace(_definition(), validator=validator))  # type: ignore[arg-type]


def test_registry_parses_arguments_with_capability_schema() -> None:
    """Catch raw planner JSON reaching an action without schema validation."""
    resolved = _registry().resolve(
        "source.ingest",
        (ActionArgument(name="source_relpath", value_json='"source/raw.txt"'),),
    )

    assert resolved.parameters.source_relpath == "source/raw.txt"
    assert resolved.parameters_json == '{"source_relpath":"source/raw.txt"}'


def test_registry_rejects_duplicate_and_non_json_arguments() -> None:
    """Catch plans that can overwrite an argument or bypass the JSON boundary."""
    registry = _registry()

    with pytest.raises(RegistryConfigurationError, match=r"source.ingest.*source_relpath"):
        registry.resolve(
            "source.ingest",
            (
                ActionArgument(name="source_relpath", value_json='"source/first.txt"'),
                ActionArgument(name="source_relpath", value_json='"source/second.txt"'),
            ),
        )

    with pytest.raises(RegistryConfigurationError, match=r"source.ingest.*source_relpath"):
        registry.resolve(
            "source.ingest",
            (ActionArgument(name="source_relpath", value_json="not-json"),),
        )


def test_registry_names_the_schema_field_that_requires_correction() -> None:
    """Catch opaque schema errors that leave the planner unable to repair its patch."""
    with pytest.raises(
        RegistryConfigurationError, match=r"source.ingest.*source_relpath"
    ):
        _registry().resolve("source.ingest", ())


def test_validate_startup_rechecks_registered_definitions() -> None:
    """Catch a registry that validates only registration-time configuration."""
    registry = _registry()
    registry._validators.clear()  # type: ignore[attr-defined]

    with pytest.raises(RegistryConfigurationError, match="register validator"):
        registry.validate_startup()


def test_eligible_evaluates_predicates_and_sorts_summaries() -> None:
    """Catch eligibility that ignores a declared hard predicate or has unstable ordering."""
    predicates = PredicateCatalog(
        {"source.absent": lambda snapshot, arguments: not snapshot.artifacts}
    )
    registry = ActionRegistry(
        predicates=predicates, validators={"source_manifest": _validate_unused}
    )
    registry.register(_definition("zeta.ingest"))
    registry.register(
        ActionDefinition(
            spec=ActionSpec(
                capability="alpha.ingest",
                description="Only ingest before source artifacts exist.",
                input_schema="SourceIngestInput",
                action_kind=ActionKind.DETERMINISTIC,
                prerequisites=(
                    {"name": "source.absent"},
                ),
                validator="source_manifest",
            ),
            input_model=SourceIngestInput,
            executor=_execute_unused,
            validator=_validate_unused,
            effect_expander=_expand_unused,
        )
    )

    eligible = registry.eligible(_snapshot())

    assert tuple(item.capability for item in eligible) == ("alpha.ingest", "zeta.ingest")


def test_eligible_evaluates_every_declared_predicate_before_rejecting() -> None:
    """Catch a short-circuit that hides a later prerequisite's deterministic result."""
    evaluations: list[str] = []
    predicates = PredicateCatalog(
        {
            "first": lambda snapshot, arguments: evaluations.append("first") or False,
            "second": lambda snapshot, arguments: evaluations.append("second") or True,
        }
    )
    registry = ActionRegistry(
        predicates=predicates, validators={"source_manifest": _validate_unused}
    )
    registry.register(
        ActionDefinition(
            spec=ActionSpec(
                capability="source.ingest",
                description="Evaluate every source prerequisite.",
                input_schema="SourceIngestInput",
                action_kind=ActionKind.DETERMINISTIC,
                prerequisites=(
                    {"name": "first"},
                    {"name": "second"},
                ),
                validator="source_manifest",
            ),
            input_model=SourceIngestInput,
            executor=_execute_unused,
            validator=_validate_unused,
            effect_expander=_expand_unused,
        )
    )

    assert registry.eligible(_snapshot()) == ()
    assert evaluations == ["first", "second"]
