"""Fail-closed action registration, eligibility, and parameter parsing."""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping

from pydantic import ValidationError

from abi.actions.contracts import ActionDefinition, ActionValidator, ResolvedAction
from abi.actions.predicates import PredicateCatalog, PredicateConfigurationError
from abi.project.artifact_paths import canonical_artifact_key
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionArgument,
    ActionSpec,
    EligibleAction,
    ProbeActionInput,
    RunSnapshot,
)
from abi.types.tools import ToolBinding


class RegistryConfigurationError(ValueError):
    """The registered action catalog cannot safely authorize work."""


class UnknownCapabilityError(RegistryConfigurationError):
    """A plan named no registered capability."""


class ActionRegistry:
    """The startup-validated, fail-closed set of executable capabilities."""

    def __init__(
        self,
        *,
        predicates: PredicateCatalog,
        validators: Mapping[str, ActionValidator],
        tools: Mapping[str, ToolBinding] | None = None,
        skill_refs: Collection[str] = (),
        semantic_repair_mappings: Collection[tuple[str, str]] = (),
    ) -> None:
        self._predicates = predicates
        self._validators = dict(validators)
        self._tools = dict(tools or {})
        self._skill_refs = frozenset(skill_refs)
        self._definitions: dict[str, ActionDefinition] = {}
        self._semantic_repairs: dict[str, str] = {}
        for reason_code, capability in semantic_repair_mappings:
            if reason_code in self._semantic_repairs:
                raise RegistryConfigurationError(
                    f"duplicate semantic repair reason {reason_code}; map each reason exactly once"
                )
            if re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", reason_code) is None:
                raise RegistryConfigurationError(
                    f"semantic repair reason {reason_code!r} is not a stable lowercase identifier; "
                    "rename the mapping before startup"
                )
            self._semantic_repairs[reason_code] = capability

    def register(self, definition: ActionDefinition) -> None:
        capability = definition.spec.capability
        if capability in self._definitions:
            raise RegistryConfigurationError(
                f"duplicate capability {capability}; remove one registration"
            )
        self._validate_definition(definition)
        self._definitions[capability] = definition

    def get(self, capability: str) -> ActionDefinition:
        try:
            return self._definitions[capability]
        except KeyError as exc:
            raise UnknownCapabilityError(
                f"unknown capability {capability}; choose an eligible registered capability"
            ) from exc

    def contains(self, capability: str) -> bool:
        return capability in self._definitions

    def semantic_repair_capability(self, reason_code: str) -> str:
        """Return the one startup-validated automatic repair capability."""
        try:
            return self._semantic_repairs[reason_code]
        except KeyError as exc:
            raise RegistryConfigurationError(
                f"unmapped semantic repair reason {reason_code}; register an explicit reason-to-capability "
                "mapping or route the repair as integrity"
            ) from exc

    def has_semantic_repair(self, reason_code: str) -> bool:
        return reason_code in self._semantic_repairs

    def specs(self) -> tuple[ActionSpec, ...]:
        """Return the immutable registered specs in stable capability order."""
        return tuple(
            self._definitions[capability].spec for capability in sorted(self._definitions)
        )

    def validate_startup(self) -> None:
        for definition in self._definitions.values():
            self._validate_definition(definition)
            probe = definition.spec.probe_capability
            if probe is not None and probe not in self._definitions:
                raise RegistryConfigurationError(
                    f"probe capability {probe} for {definition.spec.capability} is not registered; "
                    "register the read-only probe before startup"
                )
            if probe is not None:
                probe_spec = self._definitions[probe].spec
                if (
                    probe_spec.effects
                    or probe_spec.write_set
                    or probe_spec.may_have_side_effects
                ):
                    raise RegistryConfigurationError(
                        f"probe capability {probe} for {definition.spec.capability} must be "
                        "read-only, evidence-only, and side-effect-free; correct its "
                        "ActionSpec before startup"
                    )
                probe_definition = self._definitions[probe]
                if probe_definition.input_model is not ProbeActionInput:
                    raise RegistryConfigurationError(
                        f"probe capability {probe} for {definition.spec.capability} must use "
                        "ProbeActionInput so original action/attempt/operation identity is frozen"
                    )
            for alternative in definition.spec.alternative_capabilities:
                if alternative not in self._definitions:
                    raise RegistryConfigurationError(
                        f"alternative capability {alternative} for {definition.spec.capability} "
                        "is not registered; register it before startup"
                    )
                if alternative == definition.spec.capability:
                    raise RegistryConfigurationError(
                        f"alternative capability for {definition.spec.capability} cannot point "
                        "to itself; register a genuinely different capability"
                    )
        for reason_code, capability in self._semantic_repairs.items():
            repair_definition = self._definitions.get(capability)
            if repair_definition is None:
                raise RegistryConfigurationError(
                    f"semantic repair target {capability} for {reason_code} is not registered; "
                    "register the repair capability before startup"
                )
            if repair_definition.spec.may_have_side_effects:
                raise RegistryConfigurationError(
                    f"semantic repair target {capability} for {reason_code} must be side-effect-free; "
                    "separate uncertain external effects from automatic repair"
                )
            if any(
                item.spec.probe_capability == capability
                for item in self._definitions.values()
            ):
                raise RegistryConfigurationError(
                    f"semantic repair target {capability} for {reason_code} is a probe capability; "
                    "register a separate artifact-producing repair capability"
                )

    def eligible(self, snapshot: RunSnapshot) -> tuple[EligibleAction, ...]:
        eligible: list[EligibleAction] = []
        for capability, definition in self._definitions.items():
            if self.prerequisites_pass(definition, snapshot):
                spec = definition.spec
                eligible.append(
                    EligibleAction(
                        capability=capability,
                        description=spec.description,
                        input_schema=spec.input_schema,
                        estimated_cost_usd=spec.estimated_cost_usd,
                    )
                )
        return tuple(sorted(eligible, key=lambda action: action.capability))

    def prerequisites_pass(
        self, definition: ActionDefinition, snapshot: RunSnapshot
    ) -> bool:
        try:
            results = tuple(
                self._predicates.evaluate(snapshot, predicate)
                for predicate in definition.spec.prerequisites
            )
            return all(results)
        except PredicateConfigurationError:
            return False

    def resolve(
        self, capability: str, arguments: tuple[ActionArgument, ...]
    ) -> ResolvedAction:
        definition = self.get(capability)
        raw = self._decode_unique_arguments(capability, arguments)
        try:
            parameters = definition.input_model.model_validate(raw)
        except ValidationError as exc:
            field = ".".join(str(part) for part in exc.errors()[0]["loc"])
            raise RegistryConfigurationError(
                f"invalid arguments for {capability} field {field}; "
                "correct the field in the plan"
            ) from exc
        return ResolvedAction(
            definition=definition,
            parameters=parameters,
            parameters_json=parameters.model_dump_json(),
        )

    def resolve_json(self, capability: str, parameters_json: str) -> ResolvedAction:
        """Re-parse durable canonical parameters before they enter an executor."""
        definition = self.get(capability)
        try:
            parameters = definition.input_model.model_validate_json(parameters_json)
        except ValidationError as exc:
            raise RegistryConfigurationError(
                f"durable arguments for {capability} are invalid; repair the authorized Action "
                "from its original typed plan"
            ) from exc
        canonical_json = parameters.model_dump_json()
        if canonical_json != parameters_json:
            raise RegistryConfigurationError(
                f"durable arguments for {capability} are not canonical; re-authorize the Action "
                "through PolicyEngine before dispatch"
            )
        return ResolvedAction(
            definition=definition,
            parameters=parameters,
            parameters_json=canonical_json,
        )

    def _validate_definition(self, definition: ActionDefinition) -> None:
        spec = definition.spec
        if re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", spec.capability) is None:
            raise RegistryConfigurationError(
                f"capability {spec.capability!r} must use a portable lowercase ASCII namespace; "
                "rename the capability before startup"
            )
        for access_path in (*spec.read_set, *spec.write_set):
            try:
                canonical_artifact_key(access_path)
            except ValueError as exc:
                raise RegistryConfigurationError(
                    f"access path {access_path!r} for {spec.capability} must use a portable "
                    "lowercase ASCII namespace; correct the ActionSpec before startup"
                ) from exc
        if not issubclass(definition.input_model, FrozenModel):
            raise RegistryConfigurationError(
                f"input model for {spec.capability} must inherit FrozenModel; use a frozen schema"
            )
        try:
            definition.input_model.model_json_schema()
        except (TypeError, ValueError) as exc:
            raise RegistryConfigurationError(
                f"input schema for {spec.capability} is not serializable; correct the model"
            ) from exc
        registered_validator = self._validators.get(spec.validator)
        if registered_validator is None:
            raise RegistryConfigurationError(
                f"validator {spec.validator} for {spec.capability} is not registered; "
                "register validator before startup"
            )
        if (
            not callable(registered_validator)
            or not callable(definition.validator)
            or definition.validator is not registered_validator
        ):
            raise RegistryConfigurationError(
                f"validator binding for {spec.capability} must use registered "
                f"validator {spec.validator}; correct the action definition"
            )
        for predicate in spec.prerequisites:
            if not self._predicates.contains(predicate.name):
                raise RegistryConfigurationError(
                    f"predicate {predicate.name} for {spec.capability} is not registered; "
                    "register predicate before startup"
                )
        for tool_name in spec.tool_allowlist:
            if tool_name not in self._tools:
                raise RegistryConfigurationError(
                    f"tool {tool_name} for {spec.capability} is not registered; "
                    "register the tool before startup"
                )
        for skill_ref in spec.skill_refs:
            if skill_ref not in self._skill_refs:
                raise RegistryConfigurationError(
                    f"skill {skill_ref} for {spec.capability} is not registered; "
                    "register the skill before startup"
                )
        if spec.probe_capability is not None and spec.probe_capability == spec.capability:
            raise RegistryConfigurationError(
                f"probe capability for {spec.capability} cannot point to itself; register a "
                "separate read-only probe Action"
            )

    @staticmethod
    def _decode_unique_arguments(
        capability: str, arguments: tuple[ActionArgument, ...]
    ) -> dict[str, object]:
        raw: dict[str, object] = {}
        for argument in arguments:
            if argument.name in raw:
                raise RegistryConfigurationError(
                    f"duplicate argument for {capability} field {argument.name}; "
                    "provide the field once in the plan"
                )
            try:
                raw[argument.name] = json.loads(argument.value_json)
            except json.JSONDecodeError as exc:
                raise RegistryConfigurationError(
                    f"invalid JSON for {capability} argument {argument.name}; "
                    "encode the field as valid JSON in the plan"
                ) from exc
        return raw
