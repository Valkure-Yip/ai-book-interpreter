"""Non-persisted bindings for registered action implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from abi.project.layout import BookProject
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionOutcomeEnvelope,
    ActionSpec,
    GateDecision,
    RunSnapshot,
)


@dataclass(frozen=True, slots=True)
class ActionExecutionContext:
    """Runtime inputs available to an action executor, excluding mutable authority."""

    project: BookProject
    run_id: str
    snapshot: RunSnapshot


class ActionExecutor(Protocol):
    """Execute one typed action without committing any business state."""

    async def __call__(
        self, context: ActionExecutionContext, parameters: FrozenModel
    ) -> ActionOutcomeEnvelope: ...


class ActionValidator(Protocol):
    """Check deterministic evidence emitted by a completed action."""

    def __call__(self, project: BookProject, parameters: FrozenModel) -> GateDecision: ...


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    """One capability's schema, executor, and deterministic validator binding."""

    spec: ActionSpec
    input_model: type[FrozenModel]
    executor: ActionExecutor
    validator: ActionValidator


@dataclass(frozen=True, slots=True)
class ResolvedAction:
    """Validated, canonical action parameters ready for authorization."""

    definition: ActionDefinition
    parameters: FrozenModel
    parameters_json: str
