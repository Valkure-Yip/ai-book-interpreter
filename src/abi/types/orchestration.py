"""Frozen contracts for the constrained dynamic orchestration control plane."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from abi.types._base import FrozenModel
from abi.types.artifact_paths import canonical_artifact_key


def sha256_canonical_json(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_model_json(value: FrozenModel) -> str:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def canonical_failure_signature(
    capability: str, canonical_parameters_json: str, error_code: str
) -> str:
    return hashlib.sha256(
        f"{capability}\n{canonical_parameters_json}\n{error_code}".encode()
    ).hexdigest()


def _require_canonical_json(value: str, *, label: str) -> object:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if json.dumps(decoded, sort_keys=True, separators=(",", ":")) != value:
        raise ValueError(f"{label} must use canonical compact sorted-key JSON")
    return decoded


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED_BUDGET = "PAUSED_BUDGET"
    PAUSED_HITL = "PAUSED_HITL"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ActionKind(StrEnum):
    DETERMINISTIC = "deterministic"
    AGENT = "agent"
    COMPOSITE = "composite"


class ActionStatus(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRY_WAIT = "RETRY_WAIT"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    PERMANENT_FAILED = "PERMANENT_FAILED"
    INDETERMINATE = "INDETERMINATE"
    PAUSED = "PAUSED"


class ActionArgument(FrozenModel):
    name: str
    value_json: str


class RetryPolicySpec(FrozenModel):
    max_attempts: int = Field(default=3, ge=1)
    retryable_codes: tuple[str, ...] = ()
    base_delay_s: float = Field(default=1.0, ge=0)
    max_delay_s: float = Field(default=30.0, ge=0)


class PredicateSpec(FrozenModel):
    name: str
    arguments: tuple[ActionArgument, ...] = ()


class EffectSpec(FrozenModel):
    name: str
    artifact_pattern: str | None = None


class EvidenceSpec(FrozenModel):
    name: str
    required: bool = True


class ActionSpec(FrozenModel):
    capability: str
    description: str
    input_schema: str
    action_kind: ActionKind
    prerequisites: tuple[PredicateSpec, ...] = ()
    effects: tuple[EffectSpec, ...] = ()
    expected_evidence: tuple[EvidenceSpec, ...] = ()
    tool_allowlist: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    read_set: tuple[str, ...] = ()
    write_set: tuple[str, ...] = ()
    retry_policy: RetryPolicySpec = Field(default_factory=RetryPolicySpec)
    validator: str
    resource_class: str = "default"
    estimated_cost_usd: float = Field(default=0.0, ge=0)
    may_have_side_effects: bool = False
    probe_capability: str | None = None
    alternative_capabilities: tuple[str, ...] = ()


class ProposedAction(FrozenModel):
    proposal_id: str
    capability: str
    arguments: tuple[ActionArgument, ...] = ()
    dependencies: tuple[str, ...] = ()
    expected_evidence: tuple[str, ...] = ()
    priority: int = 0


class PlanPatch(FrozenModel):
    objective: str
    proposed_actions: tuple[ProposedAction, ...]
    superseded_action_ids: tuple[str, ...] = ()
    rationale: str


class ArtifactMetadata(FrozenModel):
    name: str = Field(min_length=1)
    value_json: str

    @field_validator("value_json")
    @classmethod
    def _canonical_value(cls, value: str) -> str:
        _require_canonical_json(value, label="artifact metadata value_json")
        return value


class ArtifactBundleEntry(FrozenModel):
    staged_relpath: str
    canonical_relpath: str
    media_type: str = Field(min_length=1)
    evidence_role: str = Field(min_length=1)
    metadata: tuple[ArtifactMetadata, ...] = ()

    @field_validator("canonical_relpath")
    @classmethod
    def _canonical_key(cls, value: str) -> str:
        return canonical_artifact_key(value)

    @field_validator("metadata")
    @classmethod
    def _ordered_metadata(
        cls, value: tuple[ArtifactMetadata, ...]
    ) -> tuple[ArtifactMetadata, ...]:
        names = tuple(item.name for item in value)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("artifact metadata must be uniquely ordered by name")
        return value


class ArtifactBundle(FrozenModel):
    action_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    entries: tuple[ArtifactBundleEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_entries(self) -> Self:
        prefix = f"state/staging/{self.action_id}/{self.attempt}/"
        keys: list[tuple[str, str]] = []
        staged: set[str] = set()
        canonical: set[str] = set()
        for entry in self.entries:
            if not entry.staged_relpath.startswith(prefix):
                raise ValueError("staged path must be beneath the exact action/attempt namespace")
            suffix = entry.staged_relpath.removeprefix(prefix)
            if canonical_artifact_key(suffix) != entry.canonical_relpath:
                raise ValueError("staged leaf must use the deterministic canonical-path mapping")
            if entry.staged_relpath in staged or entry.canonical_relpath in canonical:
                raise ValueError("bundle paths must be unique")
            staged.add(entry.staged_relpath)
            canonical.add(entry.canonical_relpath)
            keys.append((entry.canonical_relpath, entry.staged_relpath))
        if keys != sorted(keys):
            raise ValueError("bundle entries must already be in canonical order")
        return self


def canonical_bundle_json(bundle: ArtifactBundle) -> str:
    return canonical_model_json(bundle)


class ExpectedArtifact(FrozenModel):
    canonical_relpath: str
    media_type: str = Field(min_length=1)
    evidence_role: str = Field(min_length=1)
    metadata: tuple[ArtifactMetadata, ...] = ()

    @field_validator("canonical_relpath")
    @classmethod
    def _canonical_key(cls, value: str) -> str:
        return canonical_artifact_key(value)

    @field_validator("metadata")
    @classmethod
    def _ordered_metadata(
        cls, value: tuple[ArtifactMetadata, ...]
    ) -> tuple[ArtifactMetadata, ...]:
        names = tuple(item.name for item in value)
        if names != tuple(sorted(names)) or len(names) != len(set(names)):
            raise ValueError("expected metadata must be uniquely ordered by name")
        return value


class ExpectedArtifactManifest(FrozenModel):
    action_id: str = Field(min_length=1)
    entries: tuple[ExpectedArtifact, ...] = ()

    @field_validator("entries")
    @classmethod
    def _ordered_entries(
        cls, value: tuple[ExpectedArtifact, ...]
    ) -> tuple[ExpectedArtifact, ...]:
        paths = tuple(item.canonical_relpath for item in value)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("expected artifacts must be uniquely ordered by canonical path")
        return value


def canonical_manifest_json(manifest: ExpectedArtifactManifest) -> str:
    return canonical_model_json(manifest)


class ArtifactRef(FrozenModel):
    artifact_id: str
    relpath: str
    sha256: str
    producer_action_id: str


class GateEvidence(FrozenModel):
    evidence_id: str
    gate: str
    passed: bool
    validator_version: str
    artifact_checksums: tuple[str, ...] = ()


RepairClass = Literal["semantic", "integrity"]
RepairSource = Literal["action_outcome", "validator", "integrity_guard"]


class IncidentView(FrozenModel):
    incident_id: str
    error_code: str
    subject: str | None = None
    message: str
    action_id: str | None = None
    repair_class: RepairClass | None = None
    repair_source: RepairSource | None = None
    reason_code: str | None = None

    @model_validator(mode="after")
    def _repair_fields_together(self) -> Self:
        values = (self.repair_class, self.repair_source, self.reason_code)
        if any(item is not None for item in values) and not all(values):
            raise ValueError("repair class/source/reason must be all present or all absent")
        return self


class PlanRejectionView(FrozenModel):
    plan_version: int = Field(ge=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)

    @field_validator("reason_codes")
    @classmethod
    def _validate_reason_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(value) > 64 or re.fullmatch(r"[a-z0-9_.-]+", value) is None for value in values):
            raise ValueError("reason codes must be stable lowercase ASCII identifiers")
        return tuple(dict.fromkeys(values))


class ActionView(FrozenModel):
    action_id: str
    capability: str
    status: ActionStatus
    failure_signature: str | None = None
    repair_class: RepairClass | None = None
    repair_source: RepairSource | None = None
    reason_code: str | None = None

    @model_validator(mode="after")
    def _repair_fields_together(self) -> Self:
        values = (self.repair_class, self.repair_source, self.reason_code)
        if self.status is ActionStatus.REPAIR_REQUIRED and not all(values):
            raise ValueError("REPAIR_REQUIRED actions need class/source/reason")
        if self.status is not ActionStatus.REPAIR_REQUIRED and any(item is not None for item in values):
            raise ValueError("non-repair actions may not carry repair classification")
        return self


class EligibleAction(FrozenModel):
    capability: str
    description: str
    input_schema: str
    estimated_cost_usd: float = Field(ge=0)


class RunSnapshot(FrozenModel):
    run_id: str
    status: RunStatus
    plan_version: int = Field(default=0, ge=0)
    actions: tuple[ActionView, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    gate_evidence: tuple[GateEvidence, ...] = ()
    incidents: tuple[IncidentView, ...] = ()
    plan_rejections: tuple[PlanRejectionView, ...] = ()
    eligible_actions: tuple[EligibleAction, ...] = ()
    remaining_budget_usd: float | None = Field(default=None, ge=0)
    failure_signatures: tuple[str, ...] = ()


class PlanningContext(FrozenModel):
    policy_snapshot: RunSnapshot
    planner_snapshot: RunSnapshot


class AuthorizedAction(FrozenModel):
    action_id: str
    proposal_id: str
    plan_version: int = Field(ge=1)
    capability: str
    parameters_json: str
    dependencies: tuple[str, ...] = ()
    priority: int = 0
    read_set: tuple[str, ...] = ()
    write_set: tuple[str, ...] = ()
    idempotency_key: str
    expected_artifact_manifest: ExpectedArtifactManifest
    expected_artifact_manifest_digest: str
    expected_evidence_refs: tuple[str, ...] = ()
    retry_policy: RetryPolicySpec
    retry_policy_fingerprint: str

    @model_validator(mode="after")
    def _validate_frozen_facts(self) -> Self:
        if self.expected_artifact_manifest.action_id != self.action_id:
            raise ValueError("expected manifest must belong to the authorized action")
        manifest_json = canonical_manifest_json(self.expected_artifact_manifest)
        if sha256_canonical_json(manifest_json) != self.expected_artifact_manifest_digest:
            raise ValueError("expected manifest digest does not match canonical manifest JSON")
        policy_json = canonical_model_json(self.retry_policy)
        if sha256_canonical_json(policy_json) != self.retry_policy_fingerprint:
            raise ValueError("retry policy fingerprint does not match canonical policy JSON")
        return self


class AuthorizationDecision(FrozenModel):
    authorized: bool
    reason_codes: tuple[str, ...] = ()
    actions: tuple[AuthorizedAction, ...] = ()


class GateDecision(FrozenModel):
    passed: bool
    reason_code: str
    message: str
    validator_id: str
    validator_version: str
    bundle_digest: str
    artifact_checksums: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()


class Succeeded(FrozenModel):
    kind: Literal["succeeded"] = "succeeded"
    artifact_bundle: ArtifactBundle
    evidence_refs: tuple[str, ...] = ()


class RetryableFailure(FrozenModel):
    kind: Literal["retryable_failure"] = "retryable_failure"
    error_code: str
    message: str
    retry_after_s: float | None = None


class RepairRequired(FrozenModel):
    kind: Literal["repair_required"] = "repair_required"
    repair_class: RepairClass
    repair_source: RepairSource
    reason_code: str = Field(min_length=1)
    defect_codes: tuple[str, ...]
    message: str


class PermanentFailure(FrozenModel):
    kind: Literal["permanent_failure"] = "permanent_failure"
    error_code: str
    message: str


class Indeterminate(FrozenModel):
    kind: Literal["indeterminate"] = "indeterminate"
    operation_key: str
    error_code: str = Field(min_length=1)
    failure_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    message: str


class ProbeResolution(FrozenModel):
    kind: Literal["probe_resolution"] = "probe_resolution"
    operation_key: str
    disposition: Literal["succeeded", "absent", "unknown"]
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    message: str


class ProbeActionInput(FrozenModel):
    """Frozen binding from one evidence-only probe to its original attempt."""

    original_action_id: str = Field(min_length=1)
    original_attempt: int = Field(ge=1)
    operation_key: str = Field(min_length=1)
    probe_capability: str = Field(min_length=1)


class PendingHitlActionReview(FrozenModel):
    tool_name: str
    arguments_json: str
    description: str | None = None
    allowed_decisions: tuple[Literal["approve", "reject"], ...] = Field(min_length=1)


class PendingHitlInterrupt(FrozenModel):
    interrupt_id: str = Field(min_length=1)
    action_reviews: tuple[PendingHitlActionReview, ...] = Field(min_length=1)


class Paused(FrozenModel):
    kind: Literal["paused"] = "paused"
    reason: Literal["budget", "hitl"]
    message: str
    pending_hitl_interrupts: tuple[PendingHitlInterrupt, ...] = ()


ActionOutcome = Annotated[
    Succeeded | RetryableFailure | RepairRequired | PermanentFailure | Indeterminate
    | ProbeResolution | Paused,
    Field(discriminator="kind"),
]


class ActionOutcomeEnvelope(FrozenModel):
    outcome: ActionOutcome
    action_id: str
    attempt: int = Field(ge=1)

    @model_validator(mode="after")
    def _bundle_identity(self) -> Self:
        if isinstance(self.outcome, Succeeded):
            bundle = self.outcome.artifact_bundle
            if bundle.action_id != self.action_id or bundle.attempt != self.attempt:
                raise ValueError("success bundle identity must match the outcome envelope")
        return self


class AttemptOutcomeReceiptPayload(FrozenModel):
    action_id: str
    attempt: int = Field(ge=1)
    canonical_outcome_json: str
    outcome_digest: str
    canonical_bundle_json: str | None = None
    bundle_digest: str | None = None
    evidence_refs: tuple[str, ...] = ()
    error_code: str | None = None
    failure_signature: str | None = None

    @model_validator(mode="after")
    def _validate_receipt(self) -> Self:
        _require_canonical_json(self.canonical_outcome_json, label="outcome receipt JSON")
        if sha256_canonical_json(self.canonical_outcome_json) != self.outcome_digest:
            raise ValueError("outcome receipt digest mismatch")
        envelope = ActionOutcomeEnvelope.model_validate_json(self.canonical_outcome_json)
        if canonical_model_json(envelope) != self.canonical_outcome_json:
            raise ValueError("outcome receipt JSON is not canonical")
        if envelope.action_id != self.action_id or envelope.attempt != self.attempt:
            raise ValueError("outcome receipt envelope identity mismatch")
        outcome = envelope.outcome
        has_bundle = self.canonical_bundle_json is not None or self.bundle_digest is not None
        if isinstance(outcome, Succeeded):
            if not has_bundle or self.canonical_bundle_json is None or self.bundle_digest is None:
                raise ValueError("ordinary success receipt requires canonical bundle facts")
            parsed = ArtifactBundle.model_validate_json(self.canonical_bundle_json)
            if canonical_bundle_json(parsed) != self.canonical_bundle_json:
                raise ValueError("bundle receipt JSON is not canonical")
            if sha256_canonical_json(self.canonical_bundle_json) != self.bundle_digest:
                raise ValueError("bundle receipt digest mismatch")
            if parsed.action_id != self.action_id or parsed.attempt != self.attempt:
                raise ValueError("bundle receipt identity mismatch")
            if parsed != outcome.artifact_bundle:
                raise ValueError("bundle receipt differs from embedded outcome bundle")
        elif has_bundle:
            raise ValueError("only ordinary success may carry bundle receipt facts")
        embedded_evidence = tuple(getattr(outcome, "evidence_refs", ()))
        if self.evidence_refs != embedded_evidence:
            raise ValueError("outcome receipt evidence refs differ from embedded outcome")
        embedded_error = getattr(outcome, "error_code", None)
        if self.error_code != embedded_error:
            raise ValueError("outcome receipt error code differs from embedded outcome")
        embedded_signature = getattr(outcome, "failure_signature", None)
        if self.failure_signature != embedded_signature:
            raise ValueError("outcome receipt failure signature differs from embedded outcome")
        return self


class GateArtifactIdentity(FrozenModel):
    staged_relpath: str
    canonical_relpath: str
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("canonical_relpath")
    @classmethod
    def _canonical_key(cls, value: str) -> str:
        return canonical_artifact_key(value)


class GateReceiptPayload(FrozenModel):
    action_id: str
    attempt: int = Field(ge=1)
    validator_id: str
    validator_version: str
    canonical_gate_decision_json: str
    gate_decision_digest: str
    bundle_digest: str
    artifacts: tuple[GateArtifactIdentity, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_gate(self) -> Self:
        _require_canonical_json(
            self.canonical_gate_decision_json, label="gate receipt JSON"
        )
        if sha256_canonical_json(self.canonical_gate_decision_json) != self.gate_decision_digest:
            raise ValueError("gate receipt digest mismatch")
        decision = GateDecision.model_validate_json(self.canonical_gate_decision_json)
        if canonical_model_json(decision) != self.canonical_gate_decision_json:
            raise ValueError("gate receipt JSON is not canonical")
        if decision.passed is not True:
            raise ValueError("gate receipt requires a PASS decision")
        if decision.validator_id != self.validator_id or decision.validator_version != self.validator_version:
            raise ValueError("gate validator identity mismatch")
        if decision.bundle_digest != self.bundle_digest:
            raise ValueError("gate bundle digest mismatch")
        if decision.artifact_checksums != tuple(item.checksum for item in self.artifacts):
            raise ValueError("gate artifact checksums differ from ordered artifact identities")
        if decision.evidence_refs != self.evidence_refs:
            raise ValueError("gate evidence refs differ from the canonical decision")
        ordered = tuple((item.canonical_relpath, item.staged_relpath) for item in self.artifacts)
        if ordered != tuple(sorted(ordered)) or len(ordered) != len(set(ordered)):
            raise ValueError("gate artifact identities must be unique and canonically ordered")
        prefix = f"state/staging/{self.action_id}/{self.attempt}/"
        if any(
            not item.staged_relpath.startswith(prefix)
            or item.staged_relpath.removeprefix(prefix) != item.canonical_relpath
            for item in self.artifacts
        ):
            raise ValueError("gate staged paths must use the exact attempt namespace")
        return self


class ToolCallRecord(FrozenModel):
    name: str
    arguments_json: str


class AgentRunResult(FrozenModel):
    outcome: ActionOutcome
    llm_calls: int
    tool_calls: int
    cost_usd: float
    stopped_reason: Literal["completed", "iteration_limit", "paused", "error"]
    tool_log: tuple[ToolCallRecord, ...] = ()


class RunResult(FrozenModel):
    run_id: str
    status: RunStatus
    cost_usd: float = Field(default=0.0, ge=0)
    blocked_reason: str | None = None
