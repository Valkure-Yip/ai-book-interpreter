"""Behavioral tests for the fail-closed action registry."""

from __future__ import annotations

import json
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
    ActionStatus,
    ActionView,
    ExpectedArtifactManifest,
    GateDecision,
    IncidentView,
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


def test_semantic_repair_mapping_is_unique_registered_and_side_effect_safe() -> None:
    """Catch ambiguous or unsafe repair reasons reaching automatic replanning."""
    with pytest.raises(RegistryConfigurationError, match="duplicate semantic repair reason"):
        ActionRegistry(
            predicates=PredicateCatalog(),
            validators={"source_manifest": _validate_unused},
            semantic_repair_mappings=(
                ("term_drift", "repair.glossary"),
                ("term_drift", "repair.chapter"),
            ),
        )

    missing = ActionRegistry(
        predicates=PredicateCatalog(),
        validators={"source_manifest": _validate_unused},
        semantic_repair_mappings=(("term_drift", "repair.glossary"),),
    )
    missing.register(_definition())
    with pytest.raises(
        RegistryConfigurationError, match=r"repair\.glossary.*not registered"
    ):
        missing.validate_startup()

    unsafe = ActionRegistry(
        predicates=PredicateCatalog(),
        validators={"source_manifest": _validate_unused},
        semantic_repair_mappings=(("term_drift", "repair.glossary"),),
    )
    unsafe.register(_definition())
    unsafe.register(
        replace(
            _definition("repair.glossary"),
            spec=_definition("repair.glossary").spec.model_copy(
                update={"may_have_side_effects": True}
            ),
        )
    )
    with pytest.raises(RegistryConfigurationError, match="side-effect-free"):
        unsafe.validate_startup()


def test_semantic_repair_lookup_fails_closed_when_unmapped() -> None:
    """Catch an unknown validator or outcome reason defaulting to semantic repair."""
    registry = ActionRegistry(
        predicates=PredicateCatalog(),
        validators={"source_manifest": _validate_unused},
        semantic_repair_mappings=(("term_drift", "repair.glossary"),),
    )
    registry.register(_definition())
    registry.register(_definition("repair.glossary"))
    registry.validate_startup()

    assert registry.semantic_repair_capability("term_drift") == "repair.glossary"
    with pytest.raises(RegistryConfigurationError, match="unmapped semantic repair reason"):
        registry.semantic_repair_capability("unknown_reason")


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


def test_eligible_exposes_executable_argument_contract_and_fixed_arguments() -> None:
    """Catch Planner context that names a schema without exposing usable fields."""
    registry = ActionRegistry(
        predicates=PredicateCatalog(), validators={"source_manifest": _validate_unused}
    )
    fixed = (
        ActionArgument(name="source_relpath", value_json='"source/source_text_raw.txt"'),
    )
    registry.register(replace(_definition(), fixed_arguments=fixed))

    (eligible,) = registry.eligible(_snapshot())

    assert json.loads(eligible.input_schema) == SourceIngestInput.model_json_schema()
    assert eligible.input_schema == json.dumps(
        SourceIngestInput.model_json_schema(),
        sort_keys=True,
        separators=(",", ":"),
    )
    assert eligible.fixed_arguments == fixed


def test_eligible_actions_expose_the_deterministic_semantic_repair_route() -> None:
    registry = ActionRegistry(
        predicates=PredicateCatalog(),
        validators={"source_manifest": _validate_unused},
        semantic_repair_mappings=(("chapter_control_not_passed", "chapter.control"),),
    )
    registry.register(_definition("chapter.control"))
    registry.register(_definition("chapter.translate"))
    snapshot = RunSnapshot(
        run_id="run-1",
        status=RunStatus.RUNNING,
        incidents=(
            IncidentView(
                incident_id="repair:control:1",
                error_code="chapter_control_not_passed",
                message="Control closure did not pass.",
                action_id="control",
                repair_class="semantic",
                repair_source="validator",
                reason_code="chapter_control_not_passed",
            ),
        ),
    )

    eligible = {item.capability: item for item in registry.eligible(snapshot)}

    assert eligible["chapter.control"].repairs_reason_codes == (
        "chapter_control_not_passed",
    )
    assert eligible["chapter.translate"].repairs_reason_codes == ()


def test_eligible_hides_current_success_except_for_bound_semantic_repair() -> None:
    registry = ActionRegistry(
        predicates=PredicateCatalog(),
        validators={"source_manifest": _validate_unused},
        semantic_repair_mappings=(("metadata_wrong", "source.ingest"),),
    )
    registry.register(_definition())
    succeeded = ActionView(
        action_id="ingest-1",
        capability="source.ingest",
        status=ActionStatus.SUCCEEDED,
        outputs_current=True,
    )

    assert registry.eligible(
        RunSnapshot(
            run_id="run-1",
            status=RunStatus.RUNNING,
            actions=(succeeded,),
        )
    ) == ()

    repair_snapshot = RunSnapshot(
        run_id="run-1",
        status=RunStatus.RUNNING,
        actions=(succeeded,),
        incidents=(
            IncidentView(
                incident_id="repair:metadata:1",
                error_code="metadata_wrong",
                message="replace current metadata",
                repair_class="semantic",
                repair_source="validator",
                reason_code="metadata_wrong",
            ),
        ),
    )
    (eligible,) = registry.eligible(repair_snapshot)
    assert eligible.capability == "source.ingest"
    assert eligible.repairs_reason_codes == ("metadata_wrong",)


def test_registry_injects_fixed_arguments_and_rejects_planner_override() -> None:
    """Catch Controller-owned source identity remaining mutable Planner authority."""
    registry = ActionRegistry(
        predicates=PredicateCatalog(), validators={"source_manifest": _validate_unused}
    )
    registry.register(
        replace(
            _definition(),
            fixed_arguments=(
                ActionArgument(
                    name="source_relpath", value_json='"source/source_text_raw.txt"'
                ),
            ),
        )
    )

    resolved = registry.resolve("source.ingest", ())

    assert resolved.parameters.source_relpath == "source/source_text_raw.txt"
    with pytest.raises(RegistryConfigurationError, match=r"controller-owned.*source_relpath"):
        registry.resolve(
            "source.ingest",
            (
                ActionArgument(
                    name="source_relpath", value_json='"source/source_text.txt"'
                ),
            ),
        )


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
