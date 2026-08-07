"""Non-persisted bindings for registered action implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from abi.actions.evidence import StagingEvidenceView
from abi.project.layout import BookProject
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionArgument,
    ActionOutcomeEnvelope,
    ActionSpec,
    ArtifactBundle,
    ExpectedArtifactManifest,
    GateDecision,
    RunSnapshot,
)
from abi.types.tools import GateRuntimeMetadata


@dataclass(frozen=True, slots=True)
class ActionExecutionContext:
    """Runtime inputs available to an action executor, excluding mutable authority."""

    project: BookProject
    run_id: str
    snapshot: RunSnapshot
    action_id: str = ""
    attempt: int = 1
    source_lang: str = "source"
    target_lang: str = "target"
    source_target: str = "source-target"
    publication_mode: str = "public_domain"
    book_slug: str = "book"
    profile: str | None = None
    repair_context: tuple[str, ...] = ()
    runtime_metadata: GateRuntimeMetadata | None = None

    def __post_init__(self) -> None:
        if self.runtime_metadata is None:
            return
        if self.target_lang != self.runtime_metadata.target_language:
            raise ValueError(
                "Action target_lang must match GateRuntimeMetadata.target_language; "
                "construct both values from the controller run configuration"
            )
        if self.publication_mode != self.runtime_metadata.publication_mode:
            raise ValueError(
                "Action publication_mode must match GateRuntimeMetadata.publication_mode; "
                "construct both values from the controller run configuration"
            )


class ActionExecutor(Protocol):
    """Execute one typed action without committing any business state."""

    async def __call__(
        self, context: ActionExecutionContext, parameters: FrozenModel
    ) -> ActionOutcomeEnvelope: ...


class ActionValidator(Protocol):
    """Check deterministic evidence emitted by a completed action."""

    def __call__(
        self,
        evidence_view: StagingEvidenceView,
        parameters: FrozenModel,
        bundle: ArtifactBundle,
    ) -> GateDecision: ...


class EffectExpander(Protocol):
    def __call__(
        self, capability: str, action_id: str, parameters: FrozenModel
    ) -> ExpectedArtifactManifest: ...


@dataclass(frozen=True, slots=True)
class ActionAccess:
    """Parameter-expanded read/write resources for one resolved Action instance."""

    read_set: tuple[str, ...] = ()
    write_set: tuple[str, ...] = ()


class AccessExpander(Protocol):
    def __call__(self, capability: str, parameters: FrozenModel) -> ActionAccess: ...


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    """One capability's schema, executor, and deterministic validator binding."""

    spec: ActionSpec
    input_model: type[FrozenModel]
    executor: ActionExecutor
    validator: ActionValidator
    effect_expander: EffectExpander
    access_expander: AccessExpander | None = None
    fixed_arguments: tuple[ActionArgument, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedAction:
    """Validated, canonical action parameters ready for authorization."""

    definition: ActionDefinition
    parameters: FrozenModel
    parameters_json: str
