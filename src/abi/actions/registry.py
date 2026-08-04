"""Fail-closed action registration, eligibility, and parameter parsing."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping

from pydantic import ValidationError

from abi.actions.contracts import ActionDefinition, ActionValidator, ResolvedAction
from abi.actions.predicates import PredicateCatalog, PredicateConfigurationError
from abi.types._base import FrozenModel
from abi.types.orchestration import ActionArgument, EligibleAction, RunSnapshot
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
    ) -> None:
        self._predicates = predicates
        self._validators = dict(validators)
        self._tools = dict(tools or {})
        self._skill_refs = frozenset(skill_refs)
        self._definitions: dict[str, ActionDefinition] = {}

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

    def validate_startup(self) -> None:
        for definition in self._definitions.values():
            self._validate_definition(definition)

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

    def _validate_definition(self, definition: ActionDefinition) -> None:
        spec = definition.spec
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
