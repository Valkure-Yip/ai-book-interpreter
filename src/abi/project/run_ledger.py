"""Async SQLite repository that owns all durable orchestration business facts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self, cast
from uuid import uuid4

import aiosqlite
from pydantic import Field, ValidationError

from abi.project.artifact_paths import canonical_artifact_key
from abi.project.ledger_schema import SCHEMA_SQL
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionOutcomeEnvelope,
    ActionStatus,
    ActionView,
    ArtifactMetadata,
    ArtifactRef,
    AttemptOutcomeReceiptPayload,
    AuthorizedAction,
    ExpectedArtifactManifest,
    GateArtifactIdentity,
    GateDecision,
    GateEvidence,
    GateReceiptPayload,
    IncidentView,
    Indeterminate,
    Paused,
    PermanentFailure,
    PlanPatch,
    PlanRejectionView,
    ProbeActionInput,
    ProbeResolution,
    RepairClass,
    RepairRequired,
    RepairSource,
    RetryableFailure,
    RetryPolicySpec,
    RunSnapshot,
    RunStatus,
    Succeeded,
    canonical_manifest_json,
    canonical_model_json,
    sha256_canonical_json,
)


class LedgerError(RuntimeError):
    """Base exception for a rejected durable-ledger operation."""


class LedgerTransitionError(LedgerError):
    """Raised when an attempted run or action transition is not legal."""


class LedgerClaimConflict(LedgerTransitionError):
    """Raised when another controller already claimed the same durable attempt."""


class LedgerConflictError(LedgerError):
    """Raised when a replay disagrees with a previously committed business fact."""


class LedgerNotFoundError(LedgerError):
    """Raised when an operation refers to no durable run or action."""


class _ValidatorFailureReplayConflict(RuntimeError):
    """Internal signal used to roll back before independent compensation."""


class _ProbeBindingConflict(RuntimeError):
    """Internal signal used to roll back before independent compensation."""


class _ProbeResolutionConflict(RuntimeError):
    """Internal signal that preserves the first resolution before compensation."""

    def __init__(self, *, run_id: str, action_id: str, attempt: int) -> None:
        self.run_id = run_id
        self.action_id = action_id
        self.attempt = attempt
        super().__init__("probe resolution conflicts with the immutable first fact")


class RunSeed(FrozenModel):
    """Immutable inputs for creating a durable run."""

    run_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    budget_usd: float | None = Field(default=None, ge=0)


class RunRecord(FrozenModel):
    """A typed, immutable row from the ``runs`` table."""

    run_id: str
    status: RunStatus
    budget_usd: float | None = None
    created_at: datetime
    updated_at: datetime


class PlanVersionRecord(FrozenModel):
    """An append-only planner patch version."""

    run_id: str
    version: int = Field(ge=1)
    patch: PlanPatch
    created_at: datetime


class ActionRecord(FrozenModel):
    """An authorized action and its repository-owned state."""

    action_id: str
    run_id: str
    plan_version: int = Field(ge=1)
    capability: str
    parameters_json: str
    dependencies: tuple[str, ...] = ()
    priority: int = 0
    read_set: tuple[str, ...] = ()
    write_set: tuple[str, ...] = ()
    status: ActionStatus
    idempotency_key: str
    expected_artifact_manifest: ExpectedArtifactManifest
    expected_manifest_digest: str
    expected_evidence_refs: tuple[str, ...] = ()
    retry_policy: RetryPolicySpec
    retry_policy_fingerprint: str
    failure_signature: str | None = None
    repair_class: RepairClass | None = None
    repair_source: RepairSource | None = None
    reason_code: str | None = None
    committed_at: datetime | None = None


class ActionAttemptRecord(FrozenModel):
    """A single immutable execution attempt, updated only with its terminal fact."""

    action_id: str
    attempt: int = Field(ge=1)
    status: ActionStatus
    parameters_json: str
    expected_artifact_manifest: ExpectedArtifactManifest
    expected_manifest_digest: str
    expected_evidence_refs: tuple[str, ...]
    retry_policy: RetryPolicySpec
    retry_policy_fingerprint: str
    retry_of_attempt: int | None = None
    staging_relpath: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_signature: str | None = None
    repair_class: RepairClass | None = None
    repair_source: RepairSource | None = None
    reason_code: str | None = None


class ArtifactCommit(FrozenModel):
    """One canonical artifact fact validated before an action becomes successful."""

    artifact_id: str
    relpath: str
    sha256: str
    producer_action_id: str
    media_type: str


class SuccessCommit(FrozenModel):
    """Validated facts that atomically make one action successful."""

    action_id: str
    attempt: int = Field(default=1, ge=1)
    artifacts: tuple[ArtifactCommit, ...] = ()
    gate_evidence: tuple[GateEvidence, ...] = ()
    cost_usd: float = Field(default=0.0, ge=0)


class CommittedAction(FrozenModel):
    """Result of a successful idempotent action commit."""

    action_id: str
    committed_at: datetime
    artifacts: tuple[ArtifactCommit, ...] = ()
    gate_evidence: tuple[GateEvidence, ...] = ()
    cost_usd: float = Field(ge=0)


class IncidentRecord(FrozenModel):
    """An unresolved or resolved incident persisted independently of run status."""

    incident_id: str
    run_id: str
    error_code: str
    subject: str | None = None
    message: str
    action_id: str | None = None
    repair_class: Literal["semantic", "integrity"] | None = None
    repair_source: Literal["action_outcome", "validator", "integrity_guard"] | None = None
    reason_code: str | None = None
    status: str
    created_at: datetime
    resolved_at: datetime | None = None


class OutboxEventRecord(FrozenModel):
    """One ordered, not-yet-delivered rebuildable projection event."""

    sequence: int = Field(ge=1)
    event_id: str
    run_id: str
    event_name: str
    aggregate_id: str
    payload_json: str
    idempotency_key: str
    created_at: datetime
    delivered_at: datetime | None = None


PromotionStatus = Literal["PENDING", "COMMITTED", "CONFLICT"]


class PromotionIntent(FrozenModel):
    """A durable bridge between a staged file and its canonical destination."""

    intent_id: str
    action_id: str
    attempt: int = Field(ge=1)
    staged_relpath: str
    canonical_relpath: str
    checksum: str
    media_type: str
    evidence_role: str
    metadata_json: str
    ordinal: int = Field(ge=0)
    bundle_digest: str
    status: PromotionStatus
    created_at: datetime
    committed_at: datetime | None = None


class AttemptOutcomeReceiptRecord(AttemptOutcomeReceiptPayload):
    recorded_at: datetime


class GateReceiptRecord(GateReceiptPayload):
    recorded_at: datetime


class ValidatorFailureReceiptRecord(FrozenModel):
    """Immutable raw failed-validator fact; it can never authorize promotion."""

    action_id: str
    attempt: int = Field(ge=1)
    validator_id: str
    validator_version: str
    canonical_gate_decision_json: str
    gate_decision_digest: str
    bundle_digest: str
    artifact_checksums: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    recorded_at: datetime


class RepairFactRecord(FrozenModel):
    action_id: str
    attempt: int = Field(ge=1)
    repair_class: Literal["semantic", "integrity"]
    repair_source: Literal["action_outcome", "validator", "integrity_guard"]
    reason_code: str
    defect_codes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    message: str
    outcome_digest: str
    recorded_at: datetime


class ProbeResolutionRequest(FrozenModel):
    """Caller-bound facts required to resolve one indeterminate operation."""

    original_action_id: str = Field(min_length=1)
    original_attempt: int = Field(ge=1)
    probe_action_id: str = Field(min_length=1)
    probe_attempt: int = Field(ge=1)
    operation_key: str = Field(min_length=1)
    original_idempotency_key: str = Field(min_length=1)
    retry_policy_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProbeResolutionRecord(FrozenModel):
    """Immutable durable fact produced by ``resolve_indeterminate``."""

    original_action_id: str
    original_attempt: int = Field(ge=1)
    probe_action_id: str
    probe_attempt: int = Field(ge=1)
    operation_key: str
    disposition: Literal["succeeded", "absent", "unknown"]
    evidence_refs: tuple[str, ...]
    message: str
    original_idempotency_key: str
    retry_policy: RetryPolicySpec
    retry_policy_fingerprint: str
    error_code: str
    failure_signature: str
    resolution_digest: str
    resolved_at: datetime


class _GateIdentityList(FrozenModel):
    items: tuple[GateArtifactIdentity, ...]


class _MetadataList(FrozenModel):
    items: tuple[ArtifactMetadata, ...]


Clock = Callable[[], datetime]
LedgerHook = Callable[[str, object], None]


class RunLedger:
    """The sole mutable business authority for a dynamic orchestration run."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        *,
        clock: Clock | None = None,
        test_hook: LedgerHook | None = None,
    ) -> None:
        self._db = db
        self._clock = clock or (lambda: datetime.now(UTC))
        self._test_hook = test_hook
        self._transaction_lock = asyncio.Lock()

    @classmethod
    @asynccontextmanager
    async def open(
        cls,
        path: Path,
        *,
        clock: Clock | None = None,
        test_hook: LedgerHook | None = None,
    ) -> AsyncIterator[Self]:
        """Open and initialize a WAL-backed ledger at ``path``."""
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        try:
            await db.executescript(SCHEMA_SQL)
            await db.commit()
            yield cls(db, clock=clock, test_hook=test_hook)
        finally:
            await db.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run one explicitly serialized SQLite business transaction."""
        async with self._transaction_lock:
            await self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                await self._db.rollback()
                raise
            else:
                await self._db.commit()

    async def create_run(self, seed: RunSeed) -> str:
        now = self._now()
        try:
            async with self.transaction() as db:
                await db.execute(
                    "INSERT INTO runs (run_id, status, budget_usd, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (seed.run_id, RunStatus.RUNNING.value, seed.budget_usd, now, now),
                )
        except aiosqlite.IntegrityError as exc:
            raise LedgerConflictError(
                f"run {seed.run_id} already exists; resume the existing run instead of creating it again"
            ) from exc
        return seed.run_id

    async def get_run(self, run_id: str) -> RunRecord:
        row = await self._fetch_one("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if row is None:
            raise LedgerNotFoundError(f"run {run_id} was not found; create or select a valid run")
        return self._run_from_row(row)

    async def append_plan(self, run_id: str, patch: PlanPatch) -> PlanVersionRecord:
        now = self._now()
        async with self.transaction() as db:
            await self._require_running_run(db, run_id)
            cursor = await db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM plan_versions WHERE run_id = ?",
                (run_id,),
            )
            row = await cursor.fetchone()
            assert row is not None
            version = int(row["version"])
            await db.execute(
                "INSERT INTO plan_versions (run_id, version, objective, rationale, patch_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, version, patch.objective, patch.rationale, patch.model_dump_json(), now),
            )
            await self._insert_outbox(
                db,
                run_id=run_id,
                event_name="plan.proposed",
                aggregate_id=f"plan:{run_id}:{version}",
                payload_json=json.dumps({"plan_version": version}, sort_keys=True),
                idempotency_key=f"plan.proposed:plan:{run_id}:{version}",
                now=now,
            )
        return PlanVersionRecord(run_id=run_id, version=version, patch=patch, created_at=_parse_time(now))

    async def record_plan_rejection(
        self, run_id: str, *, plan_version: int, reason_codes: tuple[str, ...]
    ) -> PlanRejectionView:
        """Append deterministic policy feedback for the next bounded Planner context."""
        if plan_version < 1:
            raise LedgerTransitionError(
                "plan rejection needs a positive plan version; record feedback for an appended plan"
            )
        if not reason_codes:
            raise LedgerTransitionError(
                "plan rejection needs at least one reason; record the deterministic policy reason code"
            )
        rejection = PlanRejectionView(plan_version=plan_version, reason_codes=reason_codes)
        now = self._now()
        async with self.transaction() as db:
            await self._require_run(db, run_id)
            cursor = await db.execute(
                "SELECT 1 FROM plan_versions WHERE run_id = ? AND version = ?",
                (run_id, plan_version),
            )
            if await cursor.fetchone() is None:
                raise LedgerTransitionError(
                    f"run {run_id} has no durable plan {plan_version}; append the plan before recording its rejection"
                )
            await db.execute(
                "INSERT INTO plan_rejections (rejection_id, run_id, plan_version, reason_codes_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(uuid4()), run_id, plan_version, _dump_tuple(reason_codes), now),
            )
            await self._insert_outbox(
                db,
                run_id=run_id,
                event_name="plan.rejected",
                aggregate_id=f"plan:{run_id}:{plan_version}",
                payload_json=json.dumps(
                    {"plan_version": plan_version, "reason_codes": reason_codes},
                    sort_keys=True,
                ),
                idempotency_key=f"plan.rejected:plan:{run_id}:{plan_version}",
                now=now,
            )
        return rejection

    async def pending_plan(self, run_id: str) -> PlanVersionRecord | None:
        """Return the latest appended plan only while it has no durable disposition."""
        await self.get_run(run_id)
        row = await self._fetch_one(
            "SELECT * FROM plan_versions WHERE run_id = ? ORDER BY version DESC LIMIT 1",
            (run_id,),
        )
        if row is None:
            return None
        disposition = await self._fetch_one(
            "SELECT 1 FROM actions WHERE run_id = ? AND plan_version = ? "
            "UNION ALL SELECT 1 FROM plan_rejections WHERE run_id = ? AND plan_version = ? LIMIT 1",
            (run_id, row["version"], run_id, row["version"]),
        )
        if disposition is not None:
            return None
        return PlanVersionRecord(
            run_id=run_id,
            version=row["version"],
            patch=PlanPatch.model_validate_json(row["patch_json"]),
            created_at=_parse_time(row["created_at"]),
        )

    async def authorize_actions(
        self, run_id: str, actions: Sequence[AuthorizedAction]
    ) -> tuple[ActionRecord, ...]:
        if not actions:
            return ()
        async with self.transaction() as db:
            await self._require_running_run(db, run_id)
            latest = await self._latest_plan_version(db, run_id)
            if latest is None:
                raise LedgerTransitionError(
                    f"run {run_id} has no plan; append an immutable plan before authorizing actions"
                )
            await self._insert_outbox(
                db,
                run_id=run_id,
                event_name="plan.authorized",
                aggregate_id=f"plan:{run_id}:{latest}",
                payload_json=json.dumps({"plan_version": latest}, sort_keys=True),
                idempotency_key=f"plan.authorized:plan:{run_id}:{latest}",
                now=self._now(),
            )
            for action in actions:
                if action.plan_version != latest:
                    raise LedgerTransitionError(
                        f"action {action.action_id} targets plan {action.plan_version}, not {latest}; "
                        "re-authorize it against the latest plan"
                    )
                await db.execute(
                    "INSERT INTO actions (action_id, run_id, plan_version, capability, parameters_json, "
                    "dependencies_json, priority, read_set_json, write_set_json, status, idempotency_key, "
                    "expected_manifest_json, expected_manifest_digest, expected_evidence_refs_json, "
                    "retry_policy_json, retry_policy_fingerprint) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        action.action_id,
                        run_id,
                        action.plan_version,
                        action.capability,
                        action.parameters_json,
                        _dump_tuple(action.dependencies),
                        action.priority,
                        _dump_tuple(action.read_set),
                        _dump_tuple(action.write_set),
                        ActionStatus.AUTHORIZED.value,
                        action.idempotency_key,
                        canonical_manifest_json(action.expected_artifact_manifest),
                        action.expected_artifact_manifest_digest,
                        _dump_tuple(action.expected_evidence_refs),
                        canonical_model_json(action.retry_policy),
                        action.retry_policy_fingerprint,
                    ),
                )
                await self._insert_outbox(
                    db,
                    run_id=run_id,
                    event_name="action.authorized",
                    aggregate_id=action.action_id,
                    payload_json=json.dumps(
                        {
                            "action_id": action.action_id,
                            "capability": action.capability,
                            "plan_version": action.plan_version,
                        },
                        sort_keys=True,
                    ),
                    idempotency_key=f"action.authorized:{action.action_id}",
                    now=self._now(),
                )
        return tuple([await self.get_action(action.action_id) for action in actions])

    async def authorize_probe_action(
        self,
        run_id: str,
        *,
        binding: ProbeActionInput,
        patch: PlanPatch,
        action: AuthorizedAction,
        expected_previous_plan_version: int,
    ) -> tuple[ActionRecord, bool]:
        """Atomically bind, plan, authorize, and reserve one probe attempt."""
        now = self._now()
        created = False
        binding_parameters_json = binding.model_dump_json()
        durable_probe_action_id = action.action_id
        try:
            async with self.transaction() as db:
                cursor = await db.execute(
                    "SELECT * FROM probe_bindings WHERE original_action_id = ? "
                    "AND original_attempt = ?",
                    (binding.original_action_id, binding.original_attempt),
                )
                prior = await cursor.fetchone()
                if prior is not None:
                    exact = (
                        prior["operation_key"] == binding.operation_key
                        and prior["probe_capability"] == binding.probe_capability
                    )
                    existing = await self._require_action(
                        db, prior["probe_action_id"]
                    )
                    exact = exact and (
                        existing["capability"] == binding.probe_capability
                        and existing["parameters_json"]
                        == binding_parameters_json
                    )
                    if not exact:
                        raise _ProbeBindingConflict
                    durable_probe_action_id = prior["probe_action_id"]
                else:
                    await self._require_running_run(db, run_id)
                    original = await self._require_action(
                        db, binding.original_action_id
                    )
                    original_attempt = await self._attempt_row(
                        db, binding.original_action_id, binding.original_attempt
                    )
                    if (
                        original["run_id"] != run_id
                        or _action_status(original["status"], "actions.status")
                        is not ActionStatus.INDETERMINATE
                        or _action_status(
                            original_attempt["status"], "action_attempts.status"
                        )
                        is not ActionStatus.INDETERMINATE
                    ):
                        raise _ProbeBindingConflict
                    cursor = await db.execute(
                        "SELECT canonical_outcome_json FROM attempt_outcome_receipts "
                        "WHERE action_id = ? AND attempt = ?",
                        (binding.original_action_id, binding.original_attempt),
                    )
                    receipt = await cursor.fetchone()
                    if receipt is None:
                        raise _ProbeBindingConflict
                    envelope = ActionOutcomeEnvelope.model_validate_json(
                        receipt["canonical_outcome_json"]
                    )
                    if (
                        not isinstance(envelope.outcome, Indeterminate)
                        or envelope.outcome.operation_key != binding.operation_key
                    ):
                        raise _ProbeBindingConflict
                    latest = await self._latest_plan_version(db, run_id)
                    current_version = latest or 0
                    next_version = current_version + 1
                    if (
                        current_version != expected_previous_plan_version
                        or action.plan_version != next_version
                        or len(patch.proposed_actions) != 1
                        or action.capability != binding.probe_capability
                        or action.parameters_json != binding_parameters_json
                    ):
                        raise LedgerTransitionError(
                            "probe authorization facts are stale; rebuild the policy decision "
                            f"from the latest durable snapshot (current={current_version}, "
                            f"expected={expected_previous_plan_version}, action={action.plan_version}, "
                            f"next={next_version}, count={len(patch.proposed_actions)}, "
                            f"capability_match={action.capability == binding.probe_capability}, "
                            f"parameters_match={action.parameters_json == binding_parameters_json})"
                        )
                    await db.execute(
                        "INSERT INTO plan_versions (run_id, version, objective, rationale, "
                        "patch_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            run_id,
                            next_version,
                            patch.objective,
                            patch.rationale,
                            patch.model_dump_json(),
                            now,
                        ),
                    )
                    await self._insert_outbox(
                        db,
                        run_id=run_id,
                        event_name="plan.proposed",
                        aggregate_id=f"plan:{run_id}:{next_version}",
                        payload_json=json.dumps(
                            {"plan_version": next_version}, sort_keys=True
                        ),
                        idempotency_key=f"plan.proposed:plan:{run_id}:{next_version}",
                        now=now,
                    )
                    await self._insert_outbox(
                        db,
                        run_id=run_id,
                        event_name="plan.authorized",
                        aggregate_id=f"plan:{run_id}:{next_version}",
                        payload_json=json.dumps(
                            {"plan_version": next_version}, sort_keys=True
                        ),
                        idempotency_key=f"plan.authorized:plan:{run_id}:{next_version}",
                        now=now,
                    )
                    await db.execute(
                        "INSERT INTO actions (action_id, run_id, plan_version, capability, "
                        "parameters_json, dependencies_json, priority, read_set_json, write_set_json, "
                        "status, idempotency_key, expected_manifest_json, expected_manifest_digest, "
                        "expected_evidence_refs_json, retry_policy_json, retry_policy_fingerprint) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            action.action_id,
                            run_id,
                            action.plan_version,
                            action.capability,
                            action.parameters_json,
                            _dump_tuple(action.dependencies),
                            action.priority,
                            _dump_tuple(action.read_set),
                            _dump_tuple(action.write_set),
                            ActionStatus.AUTHORIZED.value,
                            action.idempotency_key,
                            canonical_manifest_json(action.expected_artifact_manifest),
                            action.expected_artifact_manifest_digest,
                            _dump_tuple(action.expected_evidence_refs),
                            canonical_model_json(action.retry_policy),
                            action.retry_policy_fingerprint,
                        ),
                    )
                    await db.execute(
                        "INSERT INTO action_attempts (action_id, attempt, status, parameters_json, "
                        "expected_manifest_json, expected_manifest_digest, expected_evidence_refs_json, "
                        "retry_policy_json, retry_policy_fingerprint, retry_of_attempt, staging_relpath) "
                        "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                        (
                            action.action_id,
                            ActionStatus.AUTHORIZED.value,
                            action.parameters_json,
                            canonical_manifest_json(action.expected_artifact_manifest),
                            action.expected_artifact_manifest_digest,
                            _dump_tuple(action.expected_evidence_refs),
                            canonical_model_json(action.retry_policy),
                            action.retry_policy_fingerprint,
                            f"state/staging/{action.action_id}/1",
                        ),
                    )
                    await self._insert_outbox(
                        db,
                        run_id=run_id,
                        event_name="action.authorized",
                        aggregate_id=action.action_id,
                        payload_json=json.dumps(
                            {
                                "action_id": action.action_id,
                                "capability": action.capability,
                                "plan_version": action.plan_version,
                            },
                            sort_keys=True,
                        ),
                        idempotency_key=f"action.authorized:{action.action_id}",
                        now=now,
                    )
                    await db.execute(
                        "INSERT INTO probe_bindings (original_action_id, original_attempt, "
                        "operation_key, probe_capability, probe_action_id, plan_version, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            binding.original_action_id,
                            binding.original_attempt,
                            binding.operation_key,
                            binding.probe_capability,
                            action.action_id,
                            action.plan_version,
                            now,
                        ),
                    )
                    created = True
        except _ProbeBindingConflict as exc:
            await self.mark_bundle_conflict(
                binding.original_action_id,
                binding.original_attempt,
                reason_code="probe_binding_conflict",
                message=(
                    "A probe binding disagrees with the original indeterminate operation; "
                    "preserve both facts and inspect before resuming."
                ),
            )
            raise LedgerConflictError(
                "probe binding conflicts with durable facts"
            ) from exc
        return await self.get_action(durable_probe_action_id), created

    async def get_action(self, action_id: str) -> ActionRecord:
        row = await self._fetch_one("SELECT * FROM actions WHERE action_id = ?", (action_id,))
        if row is None:
            raise LedgerNotFoundError(
                f"action {action_id} was not found; authorize the action before dispatching it"
            )
        return self._action_from_row(row)

    async def start_attempt(self, action_id: str, *, attempt: int = 1) -> ActionAttemptRecord:
        """Claim exactly one authorized attempt and snapshot all execution facts first."""
        if attempt < 1:
            raise LedgerTransitionError("attempt must be positive")
        now = self._now()
        async with self.transaction() as db:
            action = await self._require_action(db, action_id)
            await self._require_running_run(db, action["run_id"])
            status = _action_status(action["status"], "actions.status")
            if status is not ActionStatus.AUTHORIZED:
                if status is ActionStatus.RUNNING:
                    raise LedgerClaimConflict(
                        f"action {action_id} attempt {attempt} was already claimed"
                    )
                raise LedgerTransitionError(
                    f"action {action_id} is {status.value}; authorize or schedule a retry before starting an attempt"
                )
            row = await self._fetch_attempt_row(db, action_id, attempt)
            if row is None:
                if attempt != 1:
                    raise LedgerTransitionError(
                        f"attempt {attempt} is not authorized; create_next_attempt before dispatch"
                    )
                await db.execute(
                    "INSERT INTO action_attempts (action_id, attempt, status, parameters_json, "
                    "expected_manifest_json, expected_manifest_digest, expected_evidence_refs_json, "
                    "retry_policy_json, retry_policy_fingerprint, retry_of_attempt, staging_relpath, "
                    "started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    (
                        action_id,
                        attempt,
                        ActionStatus.RUNNING.value,
                        action["parameters_json"],
                        action["expected_manifest_json"],
                        action["expected_manifest_digest"],
                        action["expected_evidence_refs_json"],
                        action["retry_policy_json"],
                        action["retry_policy_fingerprint"],
                        f"state/staging/{action_id}/{attempt}",
                        now,
                    ),
                )
            else:
                if _action_status(row["status"], "action_attempts.status") is not ActionStatus.AUTHORIZED:
                    if _action_status(
                        row["status"], "action_attempts.status"
                    ) is ActionStatus.RUNNING:
                        raise LedgerClaimConflict(
                            f"attempt {attempt} for {action_id} was already claimed"
                        )
                    raise LedgerTransitionError(
                        f"attempt {attempt} for {action_id} is {row['status']}; never re-enter a claimed attempt"
                    )
                await db.execute(
                    "UPDATE action_attempts SET status = ?, started_at = ? "
                    "WHERE action_id = ? AND attempt = ?",
                    (ActionStatus.RUNNING.value, now, action_id, attempt),
                )
            await db.execute(
                "UPDATE actions SET status = ?, failure_signature = NULL WHERE action_id = ?",
                (ActionStatus.RUNNING.value, action_id),
            )
            await self._insert_outbox(
                db,
                run_id=action["run_id"],
                event_name="action.started",
                aggregate_id=action_id,
                payload_json=json.dumps(
                    {
                        "action_id": action_id,
                        "attempt": attempt,
                        "capability": action["capability"],
                        "plan_version": action["plan_version"],
                    },
                    sort_keys=True,
                ),
                idempotency_key=f"action.started:{action_id}:{attempt}",
                now=now,
            )
        return await self.get_attempt(action_id, attempt)

    async def get_attempt(self, action_id: str, attempt: int) -> ActionAttemptRecord:
        """Load one typed attempt row without exposing SQLite row shapes to callers."""
        row = await self._fetch_one(
            "SELECT * FROM action_attempts WHERE action_id = ? AND attempt = ?",
            (action_id, attempt),
        )
        if row is None:
            raise LedgerNotFoundError(
                f"attempt {attempt} for {action_id} was not found; start the action before reading its attempt"
            )
        return self._attempt_from_row(row)

    async def record_attempt_outcome(
        self, payload: AttemptOutcomeReceiptPayload
    ) -> AttemptOutcomeReceiptRecord:
        """Persist the immutable executor handoff before any controller hook."""
        now = self._now()
        async with self.transaction() as db:
            await self._require_action(db, payload.action_id)
            attempt = await self._attempt_row(db, payload.action_id, payload.attempt)
            if _action_status(attempt["status"], "action_attempts.status") is not ActionStatus.RUNNING:
                cursor = await db.execute(
                    "SELECT * FROM attempt_outcome_receipts WHERE action_id = ? AND attempt = ?",
                    (payload.action_id, payload.attempt),
                )
                prior = await cursor.fetchone()
                if prior is not None and self._outcome_receipt_from_row(prior).model_dump(
                    exclude={"recorded_at"}
                ) == payload.model_dump():
                    return self._outcome_receipt_from_row(prior)
                raise LedgerTransitionError(
                    f"attempt {payload.attempt} is not RUNNING; preserve its durable outcome"
                )
            cursor = await db.execute(
                "SELECT * FROM attempt_outcome_receipts WHERE action_id = ? AND attempt = ?",
                (payload.action_id, payload.attempt),
            )
            prior = await cursor.fetchone()
            if prior is not None:
                record = self._outcome_receipt_from_row(prior)
                if record.model_dump(exclude={"recorded_at"}) != payload.model_dump():
                    raise LedgerConflictError(
                        "attempt outcome receipt conflicts with its durable handoff; block and inspect evidence"
                    )
                return record
            if payload.canonical_bundle_json is not None:
                bundle = json.loads(payload.canonical_bundle_json)
                expected = json.loads(attempt["expected_manifest_json"])
                actual_effects = tuple(
                    (
                        item["canonical_relpath"],
                        item["media_type"],
                        item["evidence_role"],
                        item.get("metadata", []),
                    )
                    for item in bundle["entries"]
                )
                expected_effects = tuple(
                    (
                        item["canonical_relpath"],
                        item["media_type"],
                        item["evidence_role"],
                        item.get("metadata", []),
                    )
                    for item in expected["entries"]
                )
                if bundle["action_id"] != payload.action_id or bundle["attempt"] != payload.attempt:
                    raise LedgerConflictError("outcome bundle identity conflicts with its attempt")
                if actual_effects != expected_effects:
                    raise LedgerConflictError("outcome bundle does not equal the durable expected manifest")
            await db.execute(
                "INSERT INTO attempt_outcome_receipts (action_id, attempt, canonical_outcome_json, "
                "outcome_digest, canonical_bundle_json, bundle_digest, evidence_refs_json, error_code, "
                "failure_signature, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payload.action_id,
                    payload.attempt,
                    payload.canonical_outcome_json,
                    payload.outcome_digest,
                    payload.canonical_bundle_json,
                    payload.bundle_digest,
                    _dump_tuple(payload.evidence_refs),
                    payload.error_code,
                    payload.failure_signature,
                    now,
                ),
            )
        return await self.get_attempt_outcome(payload.action_id, payload.attempt)

    async def route_retry_from_receipt(
        self, action_id: str, *, attempt: int
    ) -> ActionAttemptRecord:
        """Apply frozen retry policy only after its immutable outcome receipt exists."""
        now = self._now()
        async with self.transaction() as db:
            action = await self._require_action(db, action_id)
            attempt_row = await self._attempt_row(db, action_id, attempt)
            cursor = await db.execute(
                "SELECT * FROM attempt_outcome_receipts WHERE action_id = ? AND attempt = ?",
                (action_id, attempt),
            )
            row = await cursor.fetchone()
            if row is None:
                raise LedgerTransitionError("retry routing requires a durable outcome receipt")
            envelope = ActionOutcomeEnvelope.model_validate_json(row["canonical_outcome_json"])
            if not isinstance(envelope.outcome, RetryableFailure):
                raise LedgerTransitionError("only a retryable-failure receipt can enter RETRY_WAIT")
            policy = RetryPolicySpec.model_validate_json(attempt_row["retry_policy_json"])
            if (
                envelope.outcome.error_code not in policy.retryable_codes
                or attempt >= policy.max_attempts
            ):
                raise LedgerTransitionError(
                    f"{envelope.outcome.error_code} is not eligible for another attempt under the frozen retry policy"
                )
            current = _action_status(attempt_row["status"], "action_attempts.status")
            if current is ActionStatus.RETRY_WAIT:
                return self._attempt_from_row(attempt_row)
            if current is not ActionStatus.RUNNING:
                raise LedgerTransitionError("retry routing requires a RUNNING attempt")
            await db.execute(
                "UPDATE action_attempts SET status = ?, finished_at = ? WHERE action_id = ? AND attempt = ?",
                (ActionStatus.RETRY_WAIT.value, now, action_id, attempt),
            )
            await db.execute(
                "UPDATE actions SET status = ? WHERE action_id = ?",
                (ActionStatus.RETRY_WAIT.value, action_id),
            )
            await self._insert_outbox(
                db,
                run_id=action["run_id"],
                event_name="action.outcome",
                aggregate_id=action_id,
                payload_json=json.dumps(
                    {
                        "action_id": action_id,
                        "attempt": attempt,
                        "classification": envelope.outcome.kind,
                    },
                    sort_keys=True,
                ),
                idempotency_key=f"action.outcome:{action_id}:{attempt}",
                now=now,
            )
        return await self.get_attempt(action_id, attempt)

    async def get_attempt_outcome(
        self, action_id: str, attempt: int
    ) -> AttemptOutcomeReceiptRecord:
        row = await self._fetch_one(
            "SELECT * FROM attempt_outcome_receipts WHERE action_id = ? AND attempt = ?",
            (action_id, attempt),
        )
        if row is None:
            raise LedgerNotFoundError("attempt outcome receipt is absent; reconcile staging or block")
        return self._outcome_receipt_from_row(row)

    async def get_probe_resolution(
        self, original_action_id: str, original_attempt: int
    ) -> ProbeResolutionRecord:
        """Load the immutable external-operation resolution for one original attempt."""
        row = await self._fetch_one(
            "SELECT * FROM probe_resolutions WHERE original_action_id = ? "
            "AND original_attempt = ?",
            (original_action_id, original_attempt),
        )
        if row is None:
            raise LedgerNotFoundError("probe resolution is absent")
        return self._probe_resolution_from_row(row)

    async def get_probe_resolution_for_probe(
        self, probe_action_id: str, probe_attempt: int
    ) -> ProbeResolutionRecord:
        """Load the immutable resolution committed by one evidence-only probe."""
        row = await self._fetch_one(
            "SELECT * FROM probe_resolutions WHERE probe_action_id = ? "
            "AND probe_attempt = ?",
            (probe_action_id, probe_attempt),
        )
        if row is None:
            raise LedgerNotFoundError("probe resolution is absent")
        return self._probe_resolution_from_row(row)

    async def create_next_attempt(
        self, action_id: str, *, previous_attempt: int
    ) -> ActionAttemptRecord:
        """Idempotently reserve exactly previous_attempt+1 after durable RETRY_WAIT."""
        next_attempt = previous_attempt + 1
        async with self.transaction() as db:
            action = await self._require_action(db, action_id)
            previous = await self._attempt_row(db, action_id, previous_attempt)
            cursor = await db.execute(
                "SELECT * FROM action_attempts WHERE action_id = ? AND retry_of_attempt = ?",
                (action_id, previous_attempt),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                exact_successor = (
                    int(existing["attempt"]) == next_attempt
                    and existing["parameters_json"] == previous["parameters_json"]
                    and existing["expected_manifest_json"]
                    == previous["expected_manifest_json"]
                    and existing["expected_manifest_digest"]
                    == previous["expected_manifest_digest"]
                    and existing["expected_evidence_refs_json"]
                    == previous["expected_evidence_refs_json"]
                    and existing["retry_policy_json"] == previous["retry_policy_json"]
                    and existing["retry_policy_fingerprint"]
                    == previous["retry_policy_fingerprint"]
                    and int(existing["retry_of_attempt"]) == previous_attempt
                    and existing["staging_relpath"]
                    == f"state/staging/{action_id}/{next_attempt}"
                )
                if not exact_successor:
                    raise LedgerConflictError(
                        "retry successor conflicts with its predecessor frozen facts"
                    )
                return self._attempt_from_row(existing)
            if _action_status(previous["status"], "action_attempts.status") is not ActionStatus.RETRY_WAIT:
                raise LedgerTransitionError("only a durable RETRY_WAIT attempt can create a successor")
            if _action_status(action["status"], "actions.status") is not ActionStatus.RETRY_WAIT:
                raise LedgerTransitionError("Action must be RETRY_WAIT before successor reservation")
            await db.execute(
                "INSERT INTO action_attempts (action_id, attempt, status, parameters_json, "
                "expected_manifest_json, expected_manifest_digest, expected_evidence_refs_json, "
                "retry_policy_json, retry_policy_fingerprint, retry_of_attempt, staging_relpath) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    action_id,
                    next_attempt,
                    ActionStatus.AUTHORIZED.value,
                    previous["parameters_json"],
                    previous["expected_manifest_json"],
                    previous["expected_manifest_digest"],
                    previous["expected_evidence_refs_json"],
                    previous["retry_policy_json"],
                    previous["retry_policy_fingerprint"],
                    previous_attempt,
                    f"state/staging/{action_id}/{next_attempt}",
                ),
            )
            await db.execute(
                "UPDATE actions SET status = ? WHERE action_id = ?",
                (ActionStatus.AUTHORIZED.value, action_id),
            )
        return await self.get_attempt(action_id, next_attempt)

    async def create_gate_receipt_and_bundle_intents(
        self, payload: GateReceiptPayload
    ) -> tuple[GateReceiptRecord, tuple[PromotionIntent, ...]]:
        """Persist PASS plus the complete ordered intent set before canonical mutation."""
        now = self._now()
        replay_conflict = False
        try:
            async with self.transaction() as db:
                await self._require_action(db, payload.action_id)
                attempt = await self._attempt_row(db, payload.action_id, payload.attempt)
                cursor = await db.execute(
                "SELECT * FROM attempt_outcome_receipts WHERE action_id = ? AND attempt = ?",
                (payload.action_id, payload.attempt),
            )
                outcome = await cursor.fetchone()
                if outcome is None or outcome["bundle_digest"] != payload.bundle_digest:
                    raise LedgerConflictError("gate receipt is not bound to the durable outcome receipt")
                cursor = await db.execute(
                "SELECT * FROM gate_receipts WHERE action_id = ? AND attempt = ?",
                (payload.action_id, payload.attempt),
            )
                prior = await cursor.fetchone()
                if prior is not None:
                    record = self._gate_receipt_from_row(prior)
                    intents = await self._intent_rows_for_attempt(db, payload.action_id, payload.attempt)
                    if (
                        record.model_dump(exclude={"recorded_at"}) != payload.model_dump()
                        or not _intent_rows_match_payload(intents, payload, attempt)
                    ):
                        replay_conflict = True
                        raise LedgerConflictError("gate receipt or complete intent set conflicts")
                    return record, tuple(self._promotion_intent_from_row(row) for row in intents)
                expected = ExpectedArtifactManifest.model_validate_json(attempt["expected_manifest_json"])
                if len(expected.entries) != len(payload.artifacts):
                    raise LedgerConflictError("gate artifact count does not equal durable manifest")
                receipt = self._outcome_receipt_from_row(outcome)
                envelope = ActionOutcomeEnvelope.model_validate_json(receipt.canonical_outcome_json)
                if not hasattr(envelope.outcome, "artifact_bundle"):
                    raise LedgerConflictError("gate receipt requires a successful outcome bundle")
                bundle = envelope.outcome.artifact_bundle
                if len(bundle.entries) != len(expected.entries):
                    raise LedgerConflictError("outcome bundle does not equal the durable manifest")
                for identity, bundle_entry, artifact in zip(
                    payload.artifacts, bundle.entries, expected.entries, strict=True
                ):
                    if (
                        identity.staged_relpath != bundle_entry.staged_relpath
                        or identity.canonical_relpath != bundle_entry.canonical_relpath
                        or bundle_entry.canonical_relpath != artifact.canonical_relpath
                        or bundle_entry.media_type != artifact.media_type
                        or bundle_entry.evidence_role != artifact.evidence_role
                        or bundle_entry.metadata != artifact.metadata
                    ):
                        raise LedgerConflictError("gate artifact order/identity differs from outcome and manifest")
                await db.execute(
                "INSERT INTO gate_receipts (action_id, attempt, validator_id, validator_version, "
                "canonical_gate_decision_json, gate_decision_digest, bundle_digest, artifacts_json, "
                "evidence_refs_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payload.action_id,
                    payload.attempt,
                    payload.validator_id,
                    payload.validator_version,
                    payload.canonical_gate_decision_json,
                    payload.gate_decision_digest,
                    payload.bundle_digest,
                    canonical_model_json(_GateIdentityList(items=payload.artifacts)),
                    _dump_tuple(payload.evidence_refs),
                    now,
                ),
            )
                for ordinal, (identity, artifact) in enumerate(
                    zip(payload.artifacts, expected.entries, strict=True)
                ):
                    await db.execute(
                    "INSERT INTO promotion_intents (intent_id, action_id, attempt, staged_relpath, "
                    "canonical_relpath, checksum, media_type, evidence_role, metadata_json, ordinal, "
                    "bundle_digest, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"intent:{payload.action_id}:{payload.attempt}:{ordinal}",
                        payload.action_id,
                        payload.attempt,
                        identity.staged_relpath,
                        canonical_artifact_key(identity.canonical_relpath),
                        identity.checksum,
                        artifact.media_type,
                        artifact.evidence_role,
                        canonical_model_json(_MetadataList(items=artifact.metadata)),
                        ordinal,
                        payload.bundle_digest,
                        "PENDING",
                        now,
                    ),
                    )
        except LedgerConflictError:
            if replay_conflict:
                await self.mark_bundle_conflict(
                    payload.action_id,
                    payload.attempt,
                    reason_code="partial_intent_set",
                    message="gate/intents replay conflicts",
                )
            raise
        except aiosqlite.IntegrityError as exc:
            await self.mark_bundle_conflict(
                payload.action_id,
                payload.attempt,
                reason_code="artifact_checksum_conflict",
                message="canonical artifact already has a promotion intent; inspect and choose the canonical artifact",
            )
            raise LedgerConflictError(
                "bundle promotion conflicts with a canonical artifact; inspect and choose the canonical artifact"
            ) from exc
        return await self.get_gate_receipt_and_intents(payload.action_id, payload.attempt)

    async def get_gate_receipt_and_intents(
        self, action_id: str, attempt: int
    ) -> tuple[GateReceiptRecord, tuple[PromotionIntent, ...]]:
        row = await self._fetch_one(
            "SELECT * FROM gate_receipts WHERE action_id = ? AND attempt = ?", (action_id, attempt)
        )
        if row is None:
            raise LedgerNotFoundError("gate receipt is absent")
        intents = await self._fetch_all(
            "SELECT * FROM promotion_intents WHERE action_id = ? AND attempt = ? ORDER BY ordinal",
            (action_id, attempt),
        )
        return self._gate_receipt_from_row(row), tuple(
            self._promotion_intent_from_row(item) for item in intents
        )

    async def get_validator_failure_receipt(
        self, action_id: str, attempt: int
    ) -> ValidatorFailureReceiptRecord:
        row = await self._fetch_one(
            "SELECT * FROM validator_failure_receipts WHERE action_id = ? AND attempt = ?",
            (action_id, attempt),
        )
        if row is None:
            raise LedgerNotFoundError("validator failure receipt is absent")
        return self._validator_failure_receipt_from_row(row)

    async def record_repair_required(
        self,
        *,
        action_id: str,
        attempt: int,
        repair_class: RepairClass,
        repair_source: RepairSource,
        reason_code: str,
        defect_codes: tuple[str, ...],
        evidence_refs: tuple[str, ...],
        message: str,
        semantic_reason_mapped: bool,
        validator_decision: GateDecision | None = None,
    ) -> RepairFactRecord:
        """Record one receipt-bound semantic repair or integrity block transaction."""
        self._invoke_hook(
            "before_repair_fact_commit",
            {"action_id": action_id, "attempt": attempt},
        )
        now = self._now()
        try:
            async with self.transaction() as db:
                action = await self._require_action(db, action_id)
                await self._attempt_row(db, action_id, attempt)
                cursor = await db.execute(
                    "SELECT * FROM attempt_outcome_receipts WHERE action_id = ? AND attempt = ?",
                    (action_id, attempt),
                )
                receipt = await cursor.fetchone()
                if receipt is None:
                    raise LedgerTransitionError("repair fact requires the durable outcome receipt")
                envelope = ActionOutcomeEnvelope.model_validate_json(
                    receipt["canonical_outcome_json"]
                )
                outcome = envelope.outcome
                raw_fact_digest = receipt["outcome_digest"]
                exact_receipt_fact = (
                    isinstance(outcome, RepairRequired)
                    and validator_decision is None
                    and repair_class == outcome.repair_class
                    and repair_source == outcome.repair_source
                    and reason_code == outcome.reason_code
                    and defect_codes == outcome.defect_codes
                    and message == outcome.message
                )

                if validator_decision is not None:
                    decision_json = canonical_model_json(validator_decision)
                    decision_digest = sha256_canonical_json(decision_json)
                    cursor = await db.execute(
                        "SELECT * FROM validator_failure_receipts "
                        "WHERE action_id = ? AND attempt = ?",
                        (action_id, attempt),
                    )
                    prior_failure = await cursor.fetchone()
                    if prior_failure is not None:
                        if prior_failure["canonical_gate_decision_json"] != decision_json:
                            raise _ValidatorFailureReplayConflict
                    else:
                        await db.execute(
                            "INSERT INTO validator_failure_receipts (action_id, attempt, "
                            "validator_id, validator_version, canonical_gate_decision_json, "
                            "gate_decision_digest, bundle_digest, artifact_checksums_json, "
                            "evidence_refs_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                action_id,
                                attempt,
                                validator_decision.validator_id,
                                validator_decision.validator_version,
                                decision_json,
                                decision_digest,
                                validator_decision.bundle_digest,
                                _dump_tuple(validator_decision.artifact_checksums),
                                _dump_tuple(validator_decision.evidence_refs),
                                now,
                            ),
                        )
                    raw_fact_digest = decision_digest
                    bundle_entry_count = -1
                    if receipt["canonical_bundle_json"] is not None:
                        raw_bundle = json.loads(receipt["canonical_bundle_json"])
                        entries = raw_bundle.get("entries") if isinstance(raw_bundle, dict) else None
                        if isinstance(entries, list):
                            bundle_entry_count = len(entries)
                    exact_receipt_fact = (
                        isinstance(outcome, Succeeded)
                        and not validator_decision.passed
                        and repair_class == "semantic"
                        and repair_source == "validator"
                        and reason_code == validator_decision.reason_code
                        and defect_codes == (validator_decision.reason_code,)
                        and evidence_refs == validator_decision.evidence_refs
                        and message == validator_decision.message
                        and validator_decision.bundle_digest == receipt["bundle_digest"]
                        and validator_decision.evidence_refs
                        == tuple(json.loads(receipt["evidence_refs_json"]))
                        and len(validator_decision.artifact_checksums)
                        == bundle_entry_count
                    )

                valid_semantic = (
                    exact_receipt_fact
                    and repair_class == "semantic"
                    and semantic_reason_mapped
                    and repair_source in {"action_outcome", "validator"}
                )
                valid_integrity = exact_receipt_fact and repair_class == "integrity"
                if valid_semantic or valid_integrity:
                    effective_class: RepairClass = repair_class
                    effective_source: RepairSource = repair_source
                    effective_reason = reason_code
                else:
                    effective_class = "integrity"
                    effective_source = "integrity_guard"
                    effective_reason = "repair_class_unknown"
                cursor = await db.execute(
                    "SELECT * FROM repair_facts WHERE action_id = ? AND attempt = ?",
                    (action_id, attempt),
                )
                prior = await cursor.fetchone()
                if prior is not None:
                    return self._repair_fact_from_row(prior)
                await db.execute(
                    "INSERT INTO repair_facts (action_id, attempt, repair_class, repair_source, reason_code, "
                    "defect_codes_json, evidence_refs_json, message, outcome_digest, recorded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        action_id, attempt, effective_class, effective_source, effective_reason,
                        _dump_tuple(defect_codes), _dump_tuple(evidence_refs), message,
                        raw_fact_digest, now,
                    ),
                )
                await db.execute(
                    "UPDATE action_attempts SET status = ?, repair_class = ?, repair_source = ?, "
                    "reason_code = ?, finished_at = ? WHERE action_id = ? AND attempt = ?",
                    (ActionStatus.REPAIR_REQUIRED.value, effective_class, effective_source, effective_reason, now, action_id, attempt),
                )
                await db.execute(
                    "UPDATE actions SET status = ?, repair_class = ?, repair_source = ?, reason_code = ? "
                    "WHERE action_id = ?",
                    (ActionStatus.REPAIR_REQUIRED.value, effective_class, effective_source, effective_reason, action_id),
                )
                await self._insert_outbox(
                    db,
                    run_id=action["run_id"],
                    event_name="action.outcome",
                    aggregate_id=action_id,
                    payload_json=json.dumps(
                        {
                            "action_id": action_id,
                            "attempt": attempt,
                            "classification": "repair_required",
                            "repair_class": effective_class,
                            "repair_source": effective_source,
                            "reason_code": effective_reason,
                        },
                        sort_keys=True,
                    ),
                    idempotency_key=f"action.outcome:{action_id}:{attempt}",
                    now=now,
                )
                incident_id = f"repair:{action_id}:{attempt}"
                await db.execute(
                    "INSERT INTO incidents (incident_id, run_id, action_id, error_code, subject, message, "
                    "repair_class, repair_source, reason_code, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)",
                    (incident_id, action["run_id"], action_id, effective_reason, incident_id, message,
                     effective_class, effective_source, effective_reason, now),
                )
                await self._insert_outbox(
                    db,
                    run_id=action["run_id"],
                    event_name="incident.created",
                    aggregate_id=action_id,
                    payload_json=json.dumps(
                        {
                            "incident_id": incident_id,
                            "action_id": action_id,
                            "error_code": effective_reason,
                            "repair_class": effective_class,
                            "repair_source": effective_source,
                            "reason_code": effective_reason,
                        },
                        sort_keys=True,
                    ),
                    idempotency_key=f"incident.created:{incident_id}",
                    now=now,
                )
                await self._insert_outbox(
                    db,
                    run_id=action["run_id"],
                    event_name="run.replanned",
                    aggregate_id=action["run_id"],
                    payload_json=json.dumps(
                        {"action_id": action_id, "attempt": attempt}, sort_keys=True
                    ),
                    idempotency_key=f"run.replanned:{action_id}:{attempt}",
                    now=now,
                )
                if effective_class == "integrity":
                    await db.execute(
                        "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                        (RunStatus.BLOCKED.value, now, action["run_id"]),
                    )
        except _ValidatorFailureReplayConflict:
            await self.mark_bundle_conflict(
                action_id,
                attempt,
                reason_code="gate_binding_conflict",
                message=(
                    "A different failed-validator decision was replayed for the same attempt; "
                    "preserve the first receipt and inspect the conflicting evidence."
                ),
            )
        self._invoke_hook(
            "after_repair_fact_commit",
            {"action_id": action_id, "attempt": attempt},
        )
        row = await self._fetch_one(
            "SELECT * FROM repair_facts WHERE action_id = ? AND attempt = ?", (action_id, attempt)
        )
        assert row is not None
        return self._repair_fact_from_row(row)

    async def mark_bundle_conflict(
        self,
        action_id: str,
        attempt: int,
        *,
        reason_code: str,
        message: str,
    ) -> ActionAttemptRecord:
        """Atomically compensate the complete attempt and block its run."""
        async with self.transaction() as db:
            action = await self._require_action(db, action_id)
            attempt_row = await self._attempt_row(db, action_id, attempt)
            await self._mark_bundle_conflict_tx(
                db, action, attempt_row, reason_code, message
            )
        return await self.get_attempt(action_id, attempt)

    async def resolve_indeterminate(
        self, request: ProbeResolutionRequest
    ) -> ProbeResolutionRecord:
        """Atomically commit probe evidence and exactly one original-attempt route."""
        try:
            return await self._resolve_indeterminate_once(request)
        except _ProbeResolutionConflict as exc:
            await self._record_probe_resolution_conflict(exc)
            raise LedgerConflictError(
                "probe resolution conflicts with the immutable first fact"
            ) from exc
        except (LedgerTransitionError, ValidationError) as exc:
            original = await self.get_action(request.original_action_id)
            conflict = _ProbeResolutionConflict(
                run_id=original.run_id,
                action_id=request.original_action_id,
                attempt=request.original_attempt,
            )
            await self._record_probe_resolution_conflict(conflict)
            raise LedgerConflictError(
                "probe resolution conflicts with durable binding or policy facts"
            ) from exc

    async def _resolve_indeterminate_once(
        self, request: ProbeResolutionRequest
    ) -> ProbeResolutionRecord:
        """Run one serialized resolution transaction before any conflict compensation."""
        now = self._now()
        async with self.transaction() as db:
            cursor = await db.execute(
                "SELECT pr.*, a.run_id AS original_run_id "
                "FROM probe_resolutions AS pr "
                "JOIN actions AS a ON a.action_id = pr.original_action_id "
                "WHERE (pr.original_action_id = ? AND pr.original_attempt = ?) "
                "OR (pr.probe_action_id = ? AND pr.probe_attempt = ?)",
                (
                    request.original_action_id,
                    request.original_attempt,
                    request.probe_action_id,
                    request.probe_attempt,
                ),
            )
            prior_identity = await cursor.fetchone()
            if prior_identity is not None and (
                prior_identity["original_action_id"] != request.original_action_id
                or int(prior_identity["original_attempt"])
                != request.original_attempt
                or prior_identity["probe_action_id"] != request.probe_action_id
                or int(prior_identity["probe_attempt"]) != request.probe_attempt
                or prior_identity["operation_key"] != request.operation_key
                or prior_identity["original_idempotency_key"]
                != request.original_idempotency_key
                or prior_identity["retry_policy_fingerprint"]
                != request.retry_policy_fingerprint
            ):
                raise _ProbeResolutionConflict(
                    run_id=prior_identity["original_run_id"],
                    action_id=prior_identity["original_action_id"],
                    attempt=int(prior_identity["original_attempt"]),
                )
            probe = await self._require_action(db, request.probe_action_id)
            probe_attempt_row = await self._attempt_row(
                db, request.probe_action_id, request.probe_attempt
            )
            original = await self._require_action(db, request.original_action_id)
            original_attempt_row = await self._attempt_row(
                db, request.original_action_id, request.original_attempt
            )
            if probe["run_id"] != original["run_id"]:
                raise LedgerTransitionError("probe and original Action belong to different runs")
            cursor = await db.execute(
                "SELECT * FROM attempt_outcome_receipts WHERE action_id = ? AND attempt = ?",
                (request.probe_action_id, request.probe_attempt),
            )
            probe_receipt = await cursor.fetchone()
            if probe_receipt is None:
                raise LedgerTransitionError("probe resolution requires its durable outcome receipt")
            probe_envelope = ActionOutcomeEnvelope.model_validate_json(
                probe_receipt["canonical_outcome_json"]
            )
            if not isinstance(probe_envelope.outcome, ProbeResolution):
                raise LedgerTransitionError("probe resolution requires ProbeResolution")
            binding = ProbeActionInput.model_validate_json(probe["parameters_json"])
            cursor = await db.execute(
                "SELECT * FROM attempt_outcome_receipts WHERE action_id = ? AND attempt = ?",
                (request.original_action_id, request.original_attempt),
            )
            original_receipt = await cursor.fetchone()
            if original_receipt is None:
                raise LedgerTransitionError("probe binding requires original outcome receipt")
            original_envelope = ActionOutcomeEnvelope.model_validate_json(
                original_receipt["canonical_outcome_json"]
            )
            cursor = await db.execute(
                "SELECT * FROM probe_bindings WHERE original_action_id = ? "
                "AND original_attempt = ?",
                (request.original_action_id, request.original_attempt),
            )
            durable_binding = await cursor.fetchone()
            policy = RetryPolicySpec.model_validate_json(
                original_attempt_row["retry_policy_json"]
            )
            resolution_json = canonical_model_json(probe_envelope.outcome)
            resolution_digest = sha256_canonical_json(resolution_json)
            exact_binding = (
                isinstance(original_envelope.outcome, Indeterminate)
                and durable_binding is not None
                and binding.original_action_id == request.original_action_id
                and binding.original_attempt == request.original_attempt
                and binding.operation_key == request.operation_key
                and probe_envelope.outcome.operation_key == request.operation_key
                and original_envelope.outcome.operation_key == request.operation_key
                and binding.probe_capability == probe["capability"]
                and durable_binding["operation_key"] == request.operation_key
                and durable_binding["probe_capability"] == probe["capability"]
                and durable_binding["probe_action_id"] == request.probe_action_id
                and original["idempotency_key"] == request.original_idempotency_key
                and original["retry_policy_json"] == original_attempt_row["retry_policy_json"]
                and original["retry_policy_fingerprint"]
                == original_attempt_row["retry_policy_fingerprint"]
                and original_attempt_row["retry_policy_fingerprint"]
                == request.retry_policy_fingerprint
                and original_receipt["error_code"]
                == original_envelope.outcome.error_code
                and original_receipt["failure_signature"]
                == original_envelope.outcome.failure_signature
                and tuple(json.loads(probe_receipt["evidence_refs_json"]))
                == probe_envelope.outcome.evidence_refs
            )
            if not exact_binding:
                if prior_identity is not None:
                    raise _ProbeResolutionConflict(
                        run_id=prior_identity["original_run_id"],
                        action_id=prior_identity["original_action_id"],
                        attempt=int(prior_identity["original_attempt"]),
                    )
                raise LedgerTransitionError("probe resolution conflicts with original binding")
            assert isinstance(original_envelope.outcome, Indeterminate)
            durable_values = {
                "original_action_id": request.original_action_id,
                "original_attempt": request.original_attempt,
                "probe_action_id": request.probe_action_id,
                "probe_attempt": request.probe_attempt,
                "operation_key": request.operation_key,
                "disposition": probe_envelope.outcome.disposition,
                "evidence_refs_json": _dump_tuple(probe_envelope.outcome.evidence_refs),
                "message": probe_envelope.outcome.message,
                "original_idempotency_key": request.original_idempotency_key,
                "retry_policy_json": original_attempt_row["retry_policy_json"],
                "retry_policy_fingerprint": request.retry_policy_fingerprint,
                "error_code": original_envelope.outcome.error_code,
                "failure_signature": original_envelope.outcome.failure_signature,
                "resolution_digest": resolution_digest,
            }
            cursor = await db.execute(
                "SELECT * FROM probe_resolutions WHERE original_action_id = ? "
                "AND original_attempt = ?",
                (request.original_action_id, request.original_attempt),
            )
            prior = await cursor.fetchone()
            if prior is not None:
                if not all(prior[key] == value for key, value in durable_values.items()):
                    raise _ProbeResolutionConflict(
                        run_id=original["run_id"],
                        action_id=prior["original_action_id"],
                        attempt=int(prior["original_attempt"]),
                    )
                return self._probe_resolution_from_row(prior)
            probe_status = _action_status(probe["status"], "actions.status")
            attempt_status = _action_status(
                probe_attempt_row["status"], "action_attempts.status"
            )
            if probe_status is not ActionStatus.RUNNING or attempt_status is not ActionStatus.RUNNING:
                raise LedgerTransitionError("probe resolution requires its RUNNING attempt")
            if (
                _action_status(original["status"], "actions.status")
                is not ActionStatus.INDETERMINATE
                or _action_status(
                    original_attempt_row["status"], "action_attempts.status"
                )
                is not ActionStatus.INDETERMINATE
            ):
                raise _ProbeResolutionConflict(
                    run_id=original["run_id"],
                    action_id=request.original_action_id,
                    attempt=request.original_attempt,
                )
            await db.execute(
                "INSERT INTO probe_resolutions (original_action_id, original_attempt, "
                "probe_action_id, probe_attempt, operation_key, disposition, "
                "evidence_refs_json, message, original_idempotency_key, retry_policy_json, "
                "retry_policy_fingerprint, error_code, failure_signature, resolution_digest, "
                "resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *durable_values.values(),
                    now,
                ),
            )
            self._invoke_hook("after_probe_resolution_insert", request)
            await db.execute(
                "UPDATE action_attempts SET status = ?, finished_at = ? "
                "WHERE action_id = ? AND attempt = ?",
                (
                    ActionStatus.SUCCEEDED.value,
                    now,
                    request.probe_action_id,
                    request.probe_attempt,
                ),
            )
            await db.execute(
                "UPDATE actions SET status = ?, committed_at = ? WHERE action_id = ?",
                (ActionStatus.SUCCEEDED.value, now, request.probe_action_id),
            )
            self._invoke_hook("after_probe_success", request)
            await self._insert_outbox(
                db,
                run_id=probe["run_id"],
                event_name="action.outcome",
                aggregate_id=request.probe_action_id,
                payload_json=json.dumps(
                    {
                        "action_id": request.probe_action_id,
                        "attempt": request.probe_attempt,
                        "classification": "probe_resolution",
                        "disposition": probe_envelope.outcome.disposition,
                    },
                    sort_keys=True,
                ),
                idempotency_key=(
                    f"action.outcome:{request.probe_action_id}:{request.probe_attempt}"
                ),
                now=now,
            )
            await self._insert_outbox(
                db,
                run_id=probe["run_id"],
                event_name="action.committed",
                aggregate_id=request.probe_action_id,
                payload_json=json.dumps(
                    {
                        "action_id": request.probe_action_id,
                        "attempt": request.probe_attempt,
                        "evidence_only": True,
                    },
                    sort_keys=True,
                ),
                idempotency_key=f"action.committed:{request.probe_action_id}",
                now=now,
            )
            disposition = probe_envelope.outcome.disposition
            if disposition == "succeeded":
                await db.execute(
                    "UPDATE action_attempts SET status = ?, finished_at = ? "
                    "WHERE action_id = ? AND attempt = ?",
                    (
                        ActionStatus.SUCCEEDED.value,
                        now,
                        request.original_action_id,
                        request.original_attempt,
                    ),
                )
                await db.execute(
                    "UPDATE actions SET status = ?, committed_at = ? WHERE action_id = ?",
                    (ActionStatus.SUCCEEDED.value, now, request.original_action_id),
                )
            elif disposition == "absent" and (
                original_envelope.outcome.error_code in policy.retryable_codes
                and request.original_attempt < policy.max_attempts
            ):
                await db.execute(
                    "UPDATE action_attempts SET status = ?, finished_at = ? "
                    "WHERE action_id = ? AND attempt = ?",
                    (
                        ActionStatus.RETRY_WAIT.value,
                        now,
                        request.original_action_id,
                        request.original_attempt,
                    ),
                )
                await db.execute(
                    "UPDATE actions SET status = ? WHERE action_id = ?",
                    (ActionStatus.RETRY_WAIT.value, request.original_action_id),
                )
            else:
                reason_code = (
                    "probe_resolution_unknown"
                    if disposition == "unknown"
                    else "probe_retry_exhausted"
                    if request.original_attempt >= policy.max_attempts
                    else "probe_retry_not_allowed"
                )
                subject = (
                    f"probe-resolution:{request.original_action_id}:"
                    f"{request.original_attempt}"
                )
                await self._insert_incident(
                    db,
                    run_id=probe["run_id"],
                    error_code=reason_code,
                    message=(
                        "Probe evidence cannot safely authorize automatic retry; "
                        "inspect durable provider and retry-policy facts."
                    ),
                    action_id=request.original_action_id,
                    subject=subject,
                    repair_class="integrity",
                    repair_source="integrity_guard",
                    reason_code=reason_code,
                    now=now,
                )
                await db.execute(
                    "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                    (RunStatus.BLOCKED.value, now, probe["run_id"]),
                )
            self._invoke_hook("after_original_resolution", request)
            await self._insert_outbox(
                db,
                run_id=probe["run_id"],
                event_name="action.resolved",
                aggregate_id=request.original_action_id,
                payload_json=json.dumps(
                    {
                        "action_id": request.original_action_id,
                        "attempt": request.original_attempt,
                        "probe_action_id": request.probe_action_id,
                        "probe_attempt": request.probe_attempt,
                        "disposition": disposition,
                    },
                    sort_keys=True,
                ),
                idempotency_key=(
                    f"action.resolved:{request.original_action_id}:"
                    f"{request.original_attempt}"
                ),
                now=now,
            )
            self._invoke_hook("after_resolution_outbox", request)
        row = await self._fetch_one(
            "SELECT * FROM probe_resolutions WHERE original_action_id = ? "
            "AND original_attempt = ?",
            (request.original_action_id, request.original_attempt),
        )
        assert row is not None
        return self._probe_resolution_from_row(row)

    async def _record_probe_resolution_conflict(
        self, conflict: _ProbeResolutionConflict
    ) -> None:
        """Block on a conflicting replay without changing the immutable first route."""
        now = self._now()
        async with self.transaction() as db:
            await self._insert_incident(
                db,
                run_id=conflict.run_id,
                error_code="probe_resolution_conflict",
                message=(
                    "A probe resolution replay conflicts with the immutable first fact; "
                    "preserve both sources and inspect external evidence."
                ),
                action_id=conflict.action_id,
                subject=(
                    f"probe-resolution:{conflict.action_id}:{conflict.attempt}:conflict"
                ),
                repair_class="integrity",
                repair_source="integrity_guard",
                reason_code="probe_resolution_conflict",
                now=now,
            )
            await db.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (RunStatus.BLOCKED.value, now, conflict.run_id),
            )

    def _invoke_hook(self, point: str, detail: object) -> None:
        if self._test_hook is not None:
            self._test_hook(point, detail)

    async def attempt_status(self, action_id: str, attempt: int) -> ActionStatus:
        return (await self.get_attempt(action_id, attempt)).status

    async def attempt_numbers(self, action_id: str) -> tuple[int, ...]:
        rows = await self._fetch_all(
            "SELECT attempt FROM action_attempts WHERE action_id = ? ORDER BY attempt", (action_id,)
        )
        return tuple(int(row["attempt"]) for row in rows)

    async def finish_attempt(
        self,
        action_id: str,
        *,
        attempt: int,
        status: ActionStatus,
        failure_signature: str | None = None,
    ) -> ActionAttemptRecord:
        if status not in {
            ActionStatus.RETRY_WAIT,
            ActionStatus.REPAIR_REQUIRED,
            ActionStatus.PERMANENT_FAILED,
            ActionStatus.INDETERMINATE,
            ActionStatus.PAUSED,
        }:
            raise LedgerTransitionError(
                f"{status.value} is not a finish outcome; use commit_success for success or choose a terminal failure state"
            )
        now = self._now()
        async with self.transaction() as db:
            action = await self._require_action(db, action_id)
            if _action_status(action["status"], "actions.status") is not ActionStatus.RUNNING:
                raise LedgerTransitionError(
                    f"action {action_id} is not running; start its attempt before recording an outcome"
                )
            row = await self._attempt_row(db, action_id, attempt)
            if _action_status(row["status"], "action_attempts.status") is not ActionStatus.RUNNING:
                raise LedgerTransitionError(
                    f"attempt {attempt} for {action_id} is already finished; record a new retry attempt instead"
                )
            cursor = await db.execute(
                "SELECT * FROM attempt_outcome_receipts WHERE action_id = ? AND attempt = ?",
                (action_id, attempt),
            )
            receipt = await cursor.fetchone()
            if receipt is None:
                raise LedgerTransitionError("finish attempt requires its durable outcome receipt")
            envelope = ActionOutcomeEnvelope.model_validate_json(receipt["canonical_outcome_json"])
            expected_status = _finish_status_for_outcome(envelope.outcome)
            if status is not expected_status:
                raise LedgerTransitionError(
                    f"{envelope.outcome.kind} receipt cannot finish as {status.value}"
                )
            if status is ActionStatus.REPAIR_REQUIRED:
                cursor = await db.execute(
                    "SELECT 1 FROM repair_facts WHERE action_id = ? AND attempt = ?",
                    (action_id, attempt),
                )
                if await cursor.fetchone() is None:
                    raise LedgerTransitionError("repair-required finish requires its durable repair fact")
            if (
                isinstance(envelope.outcome, Indeterminate)
                and failure_signature != envelope.outcome.failure_signature
            ):
                raise LedgerTransitionError(
                    "indeterminate finish must preserve the receipt failure signature"
                )
            await db.execute(
                "UPDATE action_attempts SET status = ?, failure_signature = ?, finished_at = ? "
                "WHERE action_id = ? AND attempt = ?",
                (status.value, failure_signature, now, action_id, attempt),
            )
            await db.execute(
                "UPDATE actions SET status = ?, failure_signature = ? WHERE action_id = ?",
                (status.value, failure_signature, action_id),
            )
            await self._insert_outbox(
                db,
                run_id=action["run_id"],
                event_name="action.outcome",
                aggregate_id=action_id,
                payload_json=json.dumps(
                    {
                        "action_id": action_id,
                        "attempt": attempt,
                        "capability": action["capability"],
                        "classification": status.value.lower(),
                    },
                    sort_keys=True,
                ),
                idempotency_key=f"action.outcome:{action_id}:{attempt}",
                now=now,
            )
        return await self.get_attempt(action_id, attempt)

    async def commit_success(self, commit: SuccessCommit) -> CommittedAction:
        commit = _canonical_success_commit(commit)
        now = self._now()
        conflict = False
        try:
            async with self.transaction() as db:
                action = await self._require_action(db, commit.action_id)
                signature = _commit_signature(commit, idempotency_key=action["idempotency_key"])
                prior_signature = action["commit_signature"]
                if _action_status(action["status"], "actions.status") is ActionStatus.SUCCEEDED:
                    if prior_signature == signature:
                        return await self._committed_action(db, commit.action_id)
                    await self._insert_incident(
                        db,
                        run_id=action["run_id"],
                        error_code="action_commit_conflict",
                        message=(
                            f"action {commit.action_id} was already committed with different checksums; "
                            "inspect the canonical artifact and create a repair action"
                        ),
                        action_id=commit.action_id,
                        now=now,
                    )
                    conflict = True
                else:
                    _validate_success_fact_set(commit)
                    if _action_status(action["status"], "actions.status") is not ActionStatus.RUNNING:
                        raise LedgerTransitionError(
                            f"action {commit.action_id} is {action['status']}; start an attempt before committing success"
                        )
                    attempt = await self._attempt_row(db, commit.action_id, commit.attempt)
                    if _action_status(attempt["status"], "action_attempts.status") is not ActionStatus.RUNNING:
                        raise LedgerTransitionError(
                            f"attempt {commit.attempt} for {commit.action_id} is already finished; do not commit it again"
                        )
                    await self._verify_committed_bundle_tx(db, commit)
                    await db.execute(
                        "UPDATE actions SET status = ?, committed_at = ?, commit_signature = ? WHERE action_id = ?",
                        (ActionStatus.SUCCEEDED.value, now, signature, commit.action_id),
                    )
                    await db.execute(
                        "UPDATE action_attempts SET status = ?, finished_at = ? WHERE action_id = ? AND attempt = ?",
                        (ActionStatus.SUCCEEDED.value, now, commit.action_id, commit.attempt),
                    )
                    for artifact in commit.artifacts:
                        await db.execute(
                            "INSERT INTO artifacts (artifact_id, action_id, attempt, canonical_relpath, sha256, "
                            "media_type, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                artifact.artifact_id,
                                commit.action_id,
                                commit.attempt,
                                artifact.relpath,
                                artifact.sha256,
                                artifact.media_type,
                                now,
                            ),
                        )
                    for evidence in commit.gate_evidence:
                        await db.execute(
                            "INSERT INTO gate_evidence (evidence_id, action_id, gate, passed, validator_version, "
                            "artifact_checksums_json, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                evidence.evidence_id,
                                commit.action_id,
                                evidence.gate,
                                int(evidence.passed),
                                evidence.validator_version,
                                _dump_tuple(evidence.artifact_checksums),
                                now,
                            ),
                        )
                    await db.execute(
                        "INSERT INTO budget_entries (entry_id, run_id, action_id, attempt, amount_usd, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            f"action:{commit.action_id}:attempt:{commit.attempt}",
                            action["run_id"],
                            commit.action_id,
                            commit.attempt,
                            commit.cost_usd,
                            now,
                        ),
                    )
                    await self._insert_outbox(
                        db,
                        run_id=action["run_id"],
                        event_name="action.outcome",
                        aggregate_id=commit.action_id,
                        payload_json=json.dumps(
                            {
                                "action_id": commit.action_id,
                                "attempt": commit.attempt,
                                "capability": action["capability"],
                                "classification": "succeeded",
                            },
                            sort_keys=True,
                        ),
                        idempotency_key=(
                            f"action.outcome:{commit.action_id}:{commit.attempt}"
                        ),
                        now=now,
                    )
                    await self._insert_outbox(
                        db,
                        run_id=action["run_id"],
                        event_name="action.validated",
                        aggregate_id=commit.action_id,
                        payload_json=json.dumps(
                            {
                                "action_id": commit.action_id,
                                "attempt": commit.attempt,
                                "gates": tuple(
                                    item.gate for item in commit.gate_evidence
                                ),
                            },
                            sort_keys=True,
                        ),
                        idempotency_key=(
                            f"action.validated:{commit.action_id}:{commit.attempt}"
                        ),
                        now=now,
                    )
                    await self._insert_outbox(
                        db,
                        run_id=action["run_id"],
                        event_name="action.committed",
                        aggregate_id=commit.action_id,
                        payload_json=json.dumps(
                            {"action_id": commit.action_id, "attempt": commit.attempt},
                            sort_keys=True,
                        ),
                        idempotency_key=f"action.committed:{commit.action_id}",
                        now=now,
                    )
        except aiosqlite.IntegrityError as exc:
            raise LedgerConflictError(
                f"action {commit.action_id} conflicts with a durable artifact fact; "
                "inspect and choose the canonical artifact before retrying"
            ) from exc
        if conflict:
            raise LedgerConflictError(
                f"action {commit.action_id} conflicts with its prior commit; inspect the canonical artifact "
                "and create a repair action"
            )
        return await self._committed_action(self._db, commit.action_id)

    async def verify_committed_bundle(
        self, action_id: str, attempt: int
    ) -> tuple[PromotionIntent, ...]:
        """Verify the full receipt/gate/manifest/intent identity set is committed."""
        async with self.transaction() as db:
            rows = await self._verified_intent_rows_tx(db, action_id, attempt)
        return tuple(self._promotion_intent_from_row(row) for row in rows)

    async def _verify_committed_bundle_tx(
        self, db: aiosqlite.Connection, commit: SuccessCommit
    ) -> None:
        rows = await self._verified_intent_rows_tx(db, commit.action_id, commit.attempt)
        intents = tuple(self._promotion_intent_from_row(row) for row in rows)
        expected_artifacts = tuple(
            (
                intent.canonical_relpath,
                intent.checksum,
                intent.media_type,
            )
            for intent in intents
        )
        actual_artifacts = tuple(
            (artifact.relpath, artifact.sha256, artifact.media_type)
            for artifact in commit.artifacts
        )
        if actual_artifacts != expected_artifacts or any(
            artifact.producer_action_id != commit.action_id for artifact in commit.artifacts
        ):
            raise LedgerTransitionError(
                "success commit artifacts do not equal the complete committed intent set"
            )
        cursor = await db.execute(
            "SELECT * FROM gate_receipts WHERE action_id = ? AND attempt = ?",
            (commit.action_id, commit.attempt),
        )
        gate_row = await cursor.fetchone()
        assert gate_row is not None
        gate = self._gate_receipt_from_row(gate_row)
        if len(commit.gate_evidence) != 1:
            raise LedgerTransitionError("success commit requires exactly its durable gate receipt")
        evidence = commit.gate_evidence[0]
        if (
            not evidence.passed
            or evidence.gate != gate.validator_id
            or evidence.validator_version != gate.validator_version
            or evidence.artifact_checksums != tuple(item.checksum for item in gate.artifacts)
        ):
            raise LedgerTransitionError(
                "success commit gate evidence does not equal the durable PASS receipt"
            )

    async def _verified_intent_rows_tx(
        self, db: aiosqlite.Connection, action_id: str, attempt: int
    ) -> list[aiosqlite.Row]:
        attempt_row = await self._attempt_row(db, action_id, attempt)
        cursor = await db.execute(
            "SELECT * FROM attempt_outcome_receipts WHERE action_id = ? AND attempt = ?",
            (action_id, attempt),
        )
        outcome_row = await cursor.fetchone()
        if outcome_row is None:
            raise LedgerTransitionError("success requires its durable outcome receipt")
        outcome_receipt = self._outcome_receipt_from_row(outcome_row)
        envelope = ActionOutcomeEnvelope.model_validate_json(
            outcome_receipt.canonical_outcome_json
        )
        if not isinstance(envelope.outcome, Succeeded):
            raise LedgerTransitionError("success requires a succeeded outcome receipt")
        cursor = await db.execute(
            "SELECT * FROM gate_receipts WHERE action_id = ? AND attempt = ?",
            (action_id, attempt),
        )
        gate_row = await cursor.fetchone()
        if gate_row is None:
            raise LedgerTransitionError("success requires its durable gate receipt")
        gate = self._gate_receipt_from_row(gate_row)
        rows = await self._intent_rows_for_attempt(db, action_id, attempt)
        if not _intent_rows_match_payload(rows, gate, attempt_row):
            raise LedgerConflictError(
                "promotion intent set conflicts with receipt, outcome bundle, or durable manifest"
            )
        if any(_promotion_status(row["status"], row["intent_id"]) != "COMMITTED" for row in rows):
            raise LedgerTransitionError(
                "all exact bundle intents must be COMMITTED before success"
            )
        bundle = envelope.outcome.artifact_bundle
        if gate.bundle_digest != outcome_receipt.bundle_digest or tuple(
            (entry.staged_relpath, entry.canonical_relpath)
            for entry in bundle.entries
        ) != tuple(
            (identity.staged_relpath, identity.canonical_relpath)
            for identity in gate.artifacts
        ):
            raise LedgerConflictError("gate intent set is not bound to the outcome bundle")
        return rows

    async def record_incident(
        self,
        run_id: str,
        *,
        error_code: str,
        message: str,
        action_id: str | None = None,
        repair_class: RepairClass | None = None,
        repair_source: RepairSource | None = None,
        reason_code: str | None = None,
    ) -> IncidentRecord:
        classification = (repair_class, repair_source, reason_code)
        if any(value is not None for value in classification) and not all(
            value is not None for value in classification
        ):
            raise ValueError(
                "repair_class, repair_source, and reason_code must be supplied together"
            )
        if repair_class == "semantic" and repair_source not in {
            "action_outcome",
            "validator",
        }:
            raise ValueError("semantic incidents require action_outcome or validator source")
        if repair_class == "integrity" and repair_source != "integrity_guard":
            raise ValueError("integrity incidents require integrity_guard source")
        now = self._now()
        async with self.transaction() as db:
            await self._require_run(db, run_id)
            if action_id is not None:
                action = await self._require_action(db, action_id)
                if action["run_id"] != run_id:
                    raise LedgerTransitionError(
                        f"action {action_id} belongs to another run; choose an action from run {run_id}"
                    )
            return await self._insert_incident(
                db,
                run_id=run_id,
                error_code=error_code,
                message=message,
                action_id=action_id,
                repair_class=repair_class,
                repair_source=repair_source,
                reason_code=reason_code,
                now=now,
            )

    async def get_promotion_intent(self, intent_id: str) -> PromotionIntent:
        """Load one typed promotion intent."""
        row = await self._fetch_one("SELECT * FROM promotion_intents WHERE intent_id = ?", (intent_id,))
        if row is None:
            raise LedgerNotFoundError(
                f"promotion intent {intent_id} was not found; prepare the staged artifact before promoting it"
            )
        return self._promotion_intent_from_row(row)

    async def promotion_state(self, intent_id: str) -> PromotionStatus:
        """Return the durable state of one promotion intent."""
        return (await self.get_promotion_intent(intent_id)).status

    async def promotion_intents(self, run_id: str | None = None) -> tuple[PromotionIntent, ...]:
        """List promotion intents, optionally scoped to one durable run."""
        if run_id is None:
            rows = await self._fetch_all(
                "SELECT * FROM promotion_intents ORDER BY created_at, intent_id", ()
            )
        else:
            await self.get_run(run_id)
            rows = await self._fetch_all(
                "SELECT pi.* FROM promotion_intents pi "
                "JOIN actions a ON a.action_id = pi.action_id "
                "WHERE a.run_id = ? ORDER BY pi.created_at, pi.intent_id",
                (run_id,),
            )
        return tuple(self._promotion_intent_from_row(row) for row in rows)

    async def commit_promotion_intent(self, intent_id: str) -> PromotionIntent:
        """Confirm a canonical install only after its filesystem facts are verified."""
        now = self._now()
        async with self.transaction() as db:
            cursor = await db.execute("SELECT * FROM promotion_intents WHERE intent_id = ?", (intent_id,))
            row = await cursor.fetchone()
            if row is None:
                raise LedgerNotFoundError(
                    f"promotion intent {intent_id} was not found; prepare the staged artifact before committing it"
                )
            status = _promotion_status(row["status"], intent_id)
            if status == "CONFLICT":
                raise LedgerTransitionError(
                    f"promotion intent {intent_id} is CONFLICT; repair or replace the intent "
                    "instead of committing it"
                )
            if status == "COMMITTED":
                return self._promotion_intent_from_row(row)
            await db.execute(
                "UPDATE promotion_intents SET status = ?, committed_at = ? WHERE intent_id = ?",
                ("COMMITTED", now, intent_id),
            )
        return await self.get_promotion_intent(intent_id)

    async def conflict_promotion_intent(
        self, intent_id: str, *, error_code: str, message: str
    ) -> PromotionIntent:
        """Atomically compensate a pending or committed promotion and record its incident."""
        async with self.transaction() as db:
            cursor = await db.execute(
                "SELECT pi.*, a.run_id FROM promotion_intents pi "
                "JOIN actions a ON a.action_id = pi.action_id WHERE pi.intent_id = ?",
                (intent_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise LedgerNotFoundError(
                    f"promotion intent {intent_id} was not found; cannot compensate its promotion"
                )
            _promotion_status(row["status"], intent_id)
            action = await self._require_action(db, row["action_id"])
            attempt = await self._attempt_row(db, row["action_id"], row["attempt"])
            await self._mark_bundle_conflict_tx(db, action, attempt, error_code, message)
            cursor = await db.execute(
                "SELECT * FROM promotion_intents WHERE intent_id = ?", (intent_id,)
            )
            compensated = await cursor.fetchone()
            assert compensated is not None
            return self._promotion_intent_from_row(compensated)

    async def record_promotion_incident(
        self, intent_id: str, *, error_code: str, message: str
    ) -> IncidentRecord:
        """Record one idempotent reconciliation incident against an intent's action."""
        now = self._now()
        async with self.transaction() as db:
            cursor = await db.execute(
                "SELECT pi.action_id, a.run_id FROM promotion_intents pi "
                "JOIN actions a ON a.action_id = pi.action_id WHERE pi.intent_id = ?",
                (intent_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise LedgerNotFoundError(
                    f"promotion intent {intent_id} was not found; cannot record its reconciliation incident"
                )
            cursor = await db.execute(
                "SELECT * FROM incidents WHERE action_id = ? AND error_code = ? AND subject = ? "
                "AND status = 'OPEN'",
                (row["action_id"], error_code, intent_id),
            )
            prior = await cursor.fetchone()
            if prior is not None:
                return self._incident_from_row(prior)
            return await self._insert_incident(
                db,
                run_id=row["run_id"],
                error_code=error_code,
                message=message,
                action_id=row["action_id"],
                subject=intent_id,
                now=now,
            )

    async def set_run_status(self, run_id: str, status: RunStatus) -> RunRecord:
        now = self._now()
        async with self.transaction() as db:
            row = await self._require_run(db, run_id)
            current = _run_status(row["status"], "runs.status")
            if status is current:
                return self._run_from_row(row)
            if status is RunStatus.COMPLETED:
                if current is not RunStatus.RUNNING or not await self._ready_to_complete(db, run_id):
                    raise LedgerTransitionError(
                        f"run {run_id} cannot complete from {current.value}; resume or unblock work and commit "
                        "all authorized actions first"
                    )
            elif status not in _RUN_TRANSITIONS[current]:
                raise LedgerTransitionError(
                    f"run {run_id} cannot transition from {current.value} to {status.value}; "
                    "resume or unblock the run through a legal state transition"
                )
            await db.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?", (status.value, now, run_id)
            )
            event_name = {
                RunStatus.PAUSED_BUDGET: "run.paused",
                RunStatus.PAUSED_HITL: "run.paused",
                RunStatus.BLOCKED: "run.blocked",
                RunStatus.COMPLETED: "run.completed",
                RunStatus.RUNNING: "run.resumed",
                RunStatus.CANCELLED: "run.cancelled",
            }[status]
            cursor = await db.execute(
                "SELECT COUNT(*) AS count FROM event_outbox "
                "WHERE run_id = ? AND event_name LIKE 'run.%'",
                (run_id,),
            )
            transition_row = await cursor.fetchone()
            assert transition_row is not None
            transition = int(transition_row["count"]) + 1
            await self._insert_outbox(
                db,
                run_id=run_id,
                event_name=event_name,
                aggregate_id=run_id,
                payload_json=json.dumps(
                    {
                        "previous_status": current.value,
                        "status": status.value,
                        "transition": transition,
                    },
                    sort_keys=True,
                ),
                idempotency_key=f"{event_name}:{run_id}:{transition}",
                now=now,
            )
        return await self.get_run(run_id)

    async def record_event(
        self,
        *,
        run_id: str,
        event_name: str,
        aggregate_id: str,
        payload_json: str,
        idempotency_key: str,
    ) -> OutboxEventRecord:
        """Append one idempotent business event to the durable outbox."""
        if not event_name or not aggregate_id or not idempotency_key:
            raise LedgerTransitionError(
                "outbox identity fields must be non-empty; derive stable event, aggregate, and "
                "idempotency identities before recording the event"
            )
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise LedgerTransitionError(
                "outbox payload must be valid JSON; serialize the typed event payload first"
            ) from exc
        if not isinstance(payload, dict):
            raise LedgerTransitionError(
                "outbox payload must be a JSON object; serialize a typed event model first"
            )
        payload["run_id"] = run_id
        canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        now = self._now()
        async with self.transaction() as db:
            await self._require_run(db, run_id)
            cursor = await db.execute(
                "SELECT rowid AS sequence, * FROM event_outbox WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            prior = await cursor.fetchone()
            if prior is not None:
                if (
                    prior["event_name"] != event_name
                    or prior["aggregate_id"] != aggregate_id
                    or prior["payload_json"] != canonical_payload
                ):
                    raise LedgerConflictError(
                        f"outbox event {idempotency_key} disagrees with its durable fact; inspect "
                        "the controller event identity before retrying"
                    )
                return self._outbox_event_from_row(prior)
            event_id = idempotency_key
            await db.execute(
                "INSERT INTO event_outbox (event_id, run_id, event_name, aggregate_id, payload_json, "
                "idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    run_id,
                    event_name,
                    aggregate_id,
                    canonical_payload,
                    idempotency_key,
                    now,
                ),
            )
            cursor = await db.execute(
                "SELECT rowid AS sequence, * FROM event_outbox WHERE event_id = ?",
                (event_id,),
            )
            row = await cursor.fetchone()
            assert row is not None
            return self._outbox_event_from_row(row)

    async def undelivered_events(self, run_id: str | None = None) -> tuple[OutboxEventRecord, ...]:
        """Return pending projection events in SQLite insertion order."""
        where = "delivered_at IS NULL AND run_id IS NOT NULL"
        parameters: tuple[object, ...] = ()
        if run_id is not None:
            await self.get_run(run_id)
            where += " AND run_id = ?"
            parameters = (run_id,)
        rows = await self._fetch_all(
            f"SELECT rowid AS sequence, * FROM event_outbox WHERE {where} ORDER BY rowid",
            parameters,
        )
        return tuple(self._outbox_event_from_row(row) for row in rows)

    async def mark_event_delivered(
        self, event_id: str, *, run_id: str | None = None
    ) -> OutboxEventRecord:
        """Idempotently mark an event delivered after its projection is durable."""
        now = self._now()
        async with self.transaction() as db:
            cursor = await db.execute(
                "SELECT rowid AS sequence, * FROM event_outbox WHERE event_id = ?",
                (event_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise LedgerNotFoundError(
                    f"outbox event {event_id} was not found; reload undelivered events before marking it"
                )
            if run_id is not None and row["run_id"] != run_id:
                raise LedgerTransitionError(
                    f"outbox event {event_id} belongs to another run; flush only the requested run"
                )
            if row["delivered_at"] is None:
                await db.execute(
                    "UPDATE event_outbox SET delivered_at = ? WHERE event_id = ?",
                    (now, event_id),
                )
            cursor = await db.execute(
                "SELECT rowid AS sequence, * FROM event_outbox WHERE event_id = ?",
                (event_id,),
            )
            delivered = await cursor.fetchone()
            assert delivered is not None
            return self._outbox_event_from_row(delivered)

    async def load_snapshot(self, run_id: str, *, rejection_limit: int | None = 20) -> RunSnapshot:
        if rejection_limit is not None and rejection_limit < 0:
            raise ValueError("rejection_limit must be non-negative; configure a valid snapshot sample count")
        run = await self.get_run(run_id)
        actions = await self._fetch_all(
            "SELECT * FROM actions WHERE run_id = ? ORDER BY action_id", (run_id,)
        )
        artifacts = await self._fetch_all(
            "SELECT a.* FROM artifacts a JOIN actions ac ON ac.action_id = a.action_id "
            "WHERE ac.run_id = ? ORDER BY a.artifact_id",
            (run_id,),
        )
        evidence = await self._fetch_all(
            "SELECT ge.* FROM gate_evidence ge JOIN actions ac ON ac.action_id = ge.action_id "
            "WHERE ac.run_id = ? ORDER BY ge.evidence_id",
            (run_id,),
        )
        incidents = await self._fetch_all(
            "SELECT * FROM incidents WHERE run_id = ? AND status = 'OPEN' ORDER BY created_at, incident_id",
            (run_id,),
        )
        rejection_sql = (
            "SELECT plan_version, reason_codes_json FROM plan_rejections WHERE run_id = ? "
            "ORDER BY created_at DESC, rejection_id DESC"
        )
        rejection_params: tuple[object, ...] = (run_id,)
        if rejection_limit is not None:
            rejection_sql += " LIMIT ?"
            rejection_params = (run_id, rejection_limit)
        rejections = await self._fetch_all(rejection_sql, rejection_params)
        spent_row = await self._fetch_one(
            "SELECT COALESCE(SUM(amount_usd), 0) AS spent FROM budget_entries WHERE run_id = ?", (run_id,)
        )
        assert spent_row is not None
        remaining = None if run.budget_usd is None else max(0.0, run.budget_usd - float(spent_row["spent"]))
        return RunSnapshot(
            run_id=run_id,
            status=run.status,
            plan_version=await self._latest_plan_version(self._db, run_id) or 0,
            actions=tuple(
                ActionView(
                    action_id=row["action_id"],
                    capability=row["capability"],
                    status=_action_status(row["status"], "actions.status"),
                    failure_signature=row["failure_signature"],
                    repair_class=row["repair_class"],
                    repair_source=row["repair_source"],
                    reason_code=row["reason_code"],
                )
                for row in actions
            ),
            artifacts=tuple(
                ArtifactRef(
                    artifact_id=row["artifact_id"],
                    relpath=row["canonical_relpath"],
                    sha256=row["sha256"],
                    producer_action_id=row["action_id"],
                )
                for row in artifacts
            ),
            gate_evidence=tuple(
                GateEvidence(
                    evidence_id=row["evidence_id"],
                    gate=row["gate"],
                    passed=bool(row["passed"]),
                    validator_version=row["validator_version"],
                    artifact_checksums=tuple(json.loads(row["artifact_checksums_json"])),
                )
                for row in evidence
            ),
            incidents=tuple(
                IncidentView(
                    incident_id=row["incident_id"],
                    error_code=row["error_code"],
                    subject=row["subject"],
                    message=row["message"],
                    action_id=row["action_id"],
                    repair_class=row["repair_class"],
                    repair_source=row["repair_source"],
                    reason_code=row["reason_code"],
                )
                for row in incidents
            ),
            plan_rejections=tuple(
                self._plan_rejection_from_row(row)
                for row in rejections
            ),
            remaining_budget_usd=remaining,
            failure_signatures=tuple(
                row["failure_signature"]
                for row in actions
                if row["failure_signature"] is not None
            ),
        )

    async def rebuild_status_projection(self, run_id: str, path: Path | None = None) -> RunSnapshot:
        """Rebuild a non-authoritative status projection, optionally writing it to disk."""
        snapshot = await self.load_snapshot(run_id)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return snapshot

    async def count_committed_actions(self, action_id: str) -> int:
        return await self._count("SELECT COUNT(*) FROM actions WHERE action_id = ? AND status = ?", (action_id, ActionStatus.SUCCEEDED.value))

    async def count_outbox_events(self, event_name: str, aggregate_id: str) -> int:
        return await self._count("SELECT COUNT(*) FROM event_outbox WHERE event_name = ? AND aggregate_id = ?", (event_name, aggregate_id))

    async def count_attempts(self, *, capability: str | None = None) -> int:
        if capability is None:
            return await self._count("SELECT COUNT(*) FROM action_attempts", ())
        return await self._count(
            "SELECT COUNT(*) FROM action_attempts aa JOIN actions a ON a.action_id = aa.action_id "
            "WHERE a.capability = ?", (capability,)
        )

    async def count_artifacts_for(self, action_id: str) -> int:
        return await self._count("SELECT COUNT(*) FROM artifacts WHERE action_id = ?", (action_id,))

    async def action_status(self, action_id: str) -> ActionStatus:
        return (await self.get_action(action_id)).status

    async def list_actions(self, run_id: str) -> tuple[ActionRecord, ...]:
        """Load durable Actions without exposing SQLite rows to the controller."""
        await self.get_run(run_id)
        rows = await self._fetch_all(
            "SELECT * FROM actions WHERE run_id = ? ORDER BY action_id", (run_id,)
        )
        return tuple(self._action_from_row(row) for row in rows)

    async def running_attempts(self, run_id: str) -> tuple[ActionAttemptRecord, ...]:
        """Load attempts left RUNNING across a controller crash boundary."""
        await self.get_run(run_id)
        rows = await self._fetch_all(
            "SELECT aa.* FROM action_attempts aa "
            "JOIN actions a ON a.action_id = aa.action_id "
            "WHERE a.run_id = ? AND a.status = ? AND aa.status = ? "
            "ORDER BY aa.action_id, aa.attempt",
            (run_id, ActionStatus.RUNNING.value, ActionStatus.RUNNING.value),
        )
        return tuple(self._attempt_from_row(row) for row in rows)

    async def event_names(self) -> tuple[str, ...]:
        rows = await self._fetch_all("SELECT event_name FROM event_outbox ORDER BY rowid", ())
        return tuple(row["event_name"] for row in rows)

    async def latest_event_sequence(self, run_id: str) -> int:
        """Return the monotonic projection watermark for one run."""
        await self.get_run(run_id)
        row = await self._fetch_one(
            "SELECT COALESCE(MAX(rowid), 0) FROM event_outbox WHERE run_id = ?",
            (run_id,),
        )
        assert row is not None
        return int(row[0])

    async def has_open_incident(self, error_code: str) -> bool:
        return await self._count("SELECT COUNT(*) FROM incidents WHERE error_code = ? AND status = 'OPEN'", (error_code,)) > 0

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("ledger clock must return an aware UTC datetime; inject timezone.utc in tests")
        return value.astimezone(UTC).isoformat()

    async def _fetch_one(self, sql: str, parameters: tuple[object, ...]) -> aiosqlite.Row | None:
        cursor = await self._db.execute(sql, parameters)
        return await cursor.fetchone()

    async def _fetch_all(self, sql: str, parameters: tuple[object, ...]) -> list[aiosqlite.Row]:
        cursor = await self._db.execute(sql, parameters)
        return list(await cursor.fetchall())

    @staticmethod
    async def _intent_rows_for_attempt(
        db: aiosqlite.Connection, action_id: str, attempt: int
    ) -> list[aiosqlite.Row]:
        cursor = await db.execute(
            "SELECT * FROM promotion_intents WHERE action_id = ? AND attempt = ? ORDER BY ordinal",
            (action_id, attempt),
        )
        return list(await cursor.fetchall())

    async def _mark_bundle_conflict_tx(
        self,
        db: aiosqlite.Connection,
        action: aiosqlite.Row,
        attempt: aiosqlite.Row,
        reason_code: str,
        message: str,
    ) -> None:
        now = self._now()
        await db.execute(
            "UPDATE promotion_intents SET status = 'CONFLICT' WHERE action_id = ? AND attempt = ?",
            (action["action_id"], attempt["attempt"]),
        )
        await db.execute(
            "UPDATE action_attempts SET status = ?, repair_class = 'integrity', "
            "repair_source = 'integrity_guard', reason_code = ?, finished_at = ? "
            "WHERE action_id = ? AND attempt = ?",
            (ActionStatus.REPAIR_REQUIRED.value, reason_code, now, action["action_id"], attempt["attempt"]),
        )
        await db.execute(
            "UPDATE actions SET status = ?, repair_class = 'integrity', "
            "repair_source = 'integrity_guard', reason_code = ? WHERE action_id = ?",
            (ActionStatus.REPAIR_REQUIRED.value, reason_code, action["action_id"]),
        )
        await db.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
            (RunStatus.BLOCKED.value, now, action["run_id"]),
        )
        subject = f"attempt:{attempt['attempt']}"
        cursor = await db.execute(
            "SELECT incident_id FROM incidents WHERE action_id = ? AND error_code = ? "
            "AND subject = ? AND status = 'OPEN'",
            (action["action_id"], reason_code, subject),
        )
        prior = await cursor.fetchone()
        if prior is None:
            await self._insert_incident(
                db,
                run_id=action["run_id"],
                error_code=reason_code,
                message=message,
                action_id=action["action_id"],
                subject=subject,
                repair_class="integrity",
                repair_source="integrity_guard",
                reason_code=reason_code,
                now=now,
            )

    async def _count(self, sql: str, parameters: tuple[object, ...]) -> int:
        row = await self._fetch_one(sql, parameters)
        assert row is not None
        return int(row[0])

    async def _require_run(self, db: aiosqlite.Connection, run_id: str) -> aiosqlite.Row:
        cursor = await db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        row = await cursor.fetchone()
        if row is None:
            raise LedgerNotFoundError(f"run {run_id} was not found; create or select a valid run")
        return row

    async def _require_running_run(
        self, db: aiosqlite.Connection, run_id: str
    ) -> aiosqlite.Row:
        row = await self._require_run(db, run_id)
        if _run_status(row["status"], "runs.status") is not RunStatus.RUNNING:
            raise LedgerTransitionError(
                f"run {run_id} is {row['status']}; resume the run before planning or dispatching work"
            )
        return row

    async def _require_action(self, db: aiosqlite.Connection, action_id: str) -> aiosqlite.Row:
        cursor = await db.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,))
        row = await cursor.fetchone()
        if row is None:
            raise LedgerNotFoundError(
                f"action {action_id} was not found; authorize the action before dispatching it"
            )
        return row

    async def _attempt_row(self, db: aiosqlite.Connection, action_id: str, attempt: int) -> aiosqlite.Row:
        row = await self._fetch_attempt_row(db, action_id, attempt)
        if row is None:
            raise LedgerNotFoundError(
                f"attempt {attempt} for {action_id} was not found; start the action before finishing it"
            )
        return row

    @staticmethod
    async def _fetch_attempt_row(
        db: aiosqlite.Connection, action_id: str, attempt: int
    ) -> aiosqlite.Row | None:
        cursor = await db.execute(
            "SELECT * FROM action_attempts WHERE action_id = ? AND attempt = ?",
            (action_id, attempt),
        )
        return await cursor.fetchone()

    async def _latest_plan_version(self, db: aiosqlite.Connection, run_id: str) -> int | None:
        cursor = await db.execute("SELECT MAX(version) AS version FROM plan_versions WHERE run_id = ?", (run_id,))
        row = await cursor.fetchone()
        assert row is not None
        return None if row["version"] is None else int(row["version"])

    async def _ready_to_complete(self, db: aiosqlite.Connection, run_id: str) -> bool:
        cursor = await db.execute(
            "SELECT SUM(status = ?) AS successful, "
            "SUM(status IN (?, ?, ?, ?, ?)) AS unfinished FROM actions WHERE run_id = ?",
            (
                ActionStatus.SUCCEEDED.value,
                ActionStatus.AUTHORIZED.value,
                ActionStatus.RUNNING.value,
                ActionStatus.RETRY_WAIT.value,
                ActionStatus.INDETERMINATE.value,
                ActionStatus.PAUSED.value,
                run_id,
            ),
        )
        row = await cursor.fetchone()
        assert row is not None
        return int(row["successful"] or 0) > 0 and int(row["unfinished"] or 0) == 0

    async def _committed_action(self, db: aiosqlite.Connection, action_id: str) -> CommittedAction:
        action = await self._require_action(db, action_id)
        artifact_rows = await self._rows_for_action(db, "artifacts", action_id, "artifact_id")
        evidence_rows = await self._rows_for_action(db, "gate_evidence", action_id, "evidence_id")
        return CommittedAction(
            action_id=action_id,
            committed_at=_parse_time(action["committed_at"]),
            artifacts=tuple(
                ArtifactCommit(
                    artifact_id=row["artifact_id"],
                    relpath=row["canonical_relpath"],
                    sha256=row["sha256"],
                    producer_action_id=action_id,
                    media_type=row["media_type"],
                )
                for row in artifact_rows
            ),
            gate_evidence=tuple(
                GateEvidence(
                    evidence_id=row["evidence_id"], gate=row["gate"], passed=bool(row["passed"]),
                    validator_version=row["validator_version"], artifact_checksums=tuple(json.loads(row["artifact_checksums_json"]))
                )
                for row in evidence_rows
            ),
            cost_usd=await self._action_cost(db, action_id),
        )

    @staticmethod
    async def _rows_for_action(
        db: aiosqlite.Connection, table: str, action_id: str, order_column: str
    ) -> list[aiosqlite.Row]:
        cursor = await db.execute(
            f"SELECT * FROM {table} WHERE action_id = ? ORDER BY {order_column}", (action_id,)
        )
        return list(await cursor.fetchall())

    @staticmethod
    async def _action_cost(db: aiosqlite.Connection, action_id: str) -> float:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) AS cost FROM budget_entries WHERE action_id = ?", (action_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        return float(row["cost"])

    async def _insert_outbox(
        self,
        db: aiosqlite.Connection,
        *,
        run_id: str,
        event_name: str,
        aggregate_id: str,
        payload_json: str,
        idempotency_key: str,
        now: str,
    ) -> None:
        payload = json.loads(payload_json)
        payload["run_id"] = run_id
        canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        cursor = await db.execute(
            "SELECT * FROM event_outbox WHERE idempotency_key = ?",
            (idempotency_key,),
        )
        prior = await cursor.fetchone()
        if prior is not None:
            if (
                prior["event_name"] != event_name
                or prior["run_id"] != run_id
                or prior["aggregate_id"] != aggregate_id
                or prior["payload_json"] != canonical_payload
            ):
                raise LedgerConflictError(
                    f"outbox event {idempotency_key} disagrees with its durable fact; inspect "
                    "the controller event identity before retrying"
                )
            return
        await db.execute(
            "INSERT INTO event_outbox (event_id, run_id, event_name, aggregate_id, payload_json, "
            "idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                idempotency_key,
                run_id,
                event_name,
                aggregate_id,
                canonical_payload,
                idempotency_key,
                now,
            ),
        )

    async def _insert_incident(
        self,
        db: aiosqlite.Connection,
        *,
        run_id: str,
        error_code: str,
        message: str,
        action_id: str | None,
        subject: str | None = None,
        repair_class: RepairClass | None = None,
        repair_source: RepairSource | None = None,
        reason_code: str | None = None,
        now: str,
    ) -> IncidentRecord:
        record = IncidentRecord(
            incident_id=str(uuid4()), run_id=run_id, error_code=error_code, subject=subject, message=message,
            action_id=action_id, repair_class=repair_class, repair_source=repair_source,
            reason_code=reason_code, status="OPEN", created_at=_parse_time(now)
        )
        await db.execute(
            "INSERT INTO incidents (incident_id, run_id, action_id, error_code, subject, message, "
            "repair_class, repair_source, reason_code, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.incident_id, run_id, action_id, error_code, subject, message,
             repair_class, repair_source, reason_code, record.status, now),
        )
        incident_payload: dict[str, object] = {
            "incident_id": record.incident_id,
            "action_id": action_id,
            "error_code": error_code,
        }
        if repair_class is not None:
            incident_payload.update(
                repair_class=repair_class,
                repair_source=repair_source,
                reason_code=reason_code,
            )
        await self._insert_outbox(
            db,
            run_id=run_id,
            event_name="incident.created",
            aggregate_id=action_id or run_id,
            payload_json=json.dumps(incident_payload, sort_keys=True),
            idempotency_key=f"incident.created:{record.incident_id}",
            now=now,
        )
        return record

    @staticmethod
    def _run_from_row(row: aiosqlite.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"], status=_run_status(row["status"], "runs.status"), budget_usd=row["budget_usd"],
            created_at=_parse_time(row["created_at"]), updated_at=_parse_time(row["updated_at"])
        )

    @staticmethod
    def _action_from_row(row: aiosqlite.Row) -> ActionRecord:
        return ActionRecord(
            action_id=row["action_id"], run_id=row["run_id"], plan_version=row["plan_version"],
            capability=row["capability"], parameters_json=row["parameters_json"],
            dependencies=tuple(json.loads(row["dependencies_json"])), priority=row["priority"],
            read_set=tuple(json.loads(row["read_set_json"])), write_set=tuple(json.loads(row["write_set_json"])),
            status=_action_status(row["status"], "actions.status"), idempotency_key=row["idempotency_key"],
            expected_artifact_manifest=ExpectedArtifactManifest.model_validate_json(
                row["expected_manifest_json"]
            ),
            expected_manifest_digest=row["expected_manifest_digest"],
            expected_evidence_refs=tuple(json.loads(row["expected_evidence_refs_json"])),
            retry_policy=RetryPolicySpec.model_validate_json(row["retry_policy_json"]),
            retry_policy_fingerprint=row["retry_policy_fingerprint"],
            failure_signature=row["failure_signature"],
            repair_class=row["repair_class"],
            repair_source=row["repair_source"],
            reason_code=row["reason_code"],
            committed_at=None if row["committed_at"] is None else _parse_time(row["committed_at"]),
        )

    @staticmethod
    def _promotion_intent_from_row(row: aiosqlite.Row) -> PromotionIntent:
        status = _promotion_status(row["status"], row["intent_id"])
        return PromotionIntent(
            intent_id=row["intent_id"],
            action_id=row["action_id"],
            attempt=row["attempt"],
            staged_relpath=row["staged_relpath"],
            canonical_relpath=row["canonical_relpath"],
            checksum=row["checksum"],
            media_type=row["media_type"],
            evidence_role=row["evidence_role"],
            metadata_json=row["metadata_json"],
            ordinal=row["ordinal"],
            bundle_digest=row["bundle_digest"],
            status=status,
            created_at=_parse_time(row["created_at"]),
            committed_at=None if row["committed_at"] is None else _parse_time(row["committed_at"]),
        )

    @staticmethod
    def _incident_from_row(row: aiosqlite.Row) -> IncidentRecord:
        return IncidentRecord(
            incident_id=row["incident_id"],
            run_id=row["run_id"],
            error_code=row["error_code"],
            subject=row["subject"],
            message=row["message"],
            action_id=row["action_id"],
            repair_class=row["repair_class"],
            repair_source=row["repair_source"],
            reason_code=row["reason_code"],
            status=row["status"],
            created_at=_parse_time(row["created_at"]),
            resolved_at=None if row["resolved_at"] is None else _parse_time(row["resolved_at"]),
        )

    @staticmethod
    def _outbox_event_from_row(row: aiosqlite.Row) -> OutboxEventRecord:
        return OutboxEventRecord(
            sequence=row["sequence"],
            event_id=row["event_id"],
            run_id=row["run_id"],
            event_name=row["event_name"],
            aggregate_id=row["aggregate_id"],
            payload_json=row["payload_json"],
            idempotency_key=row["idempotency_key"],
            created_at=_parse_time(row["created_at"]),
            delivered_at=(
                None
                if row["delivered_at"] is None
                else _parse_time(row["delivered_at"])
            ),
        )

    @staticmethod
    def _plan_rejection_from_row(row: aiosqlite.Row) -> PlanRejectionView:
        """Parse stored code-only rejection feedback before it reaches a Planner prompt."""
        try:
            raw_codes = json.loads(row["reason_codes_json"])
        except json.JSONDecodeError as exc:
            raise LedgerError(
                "durable plan rejection contains invalid JSON; repair the ledger row before replanning"
            ) from exc
        if not isinstance(raw_codes, list) or not all(isinstance(code, str) for code in raw_codes):
            raise LedgerError(
                "durable plan rejection must contain a JSON list of stable reason codes; repair the ledger row"
            )
        try:
            return PlanRejectionView(plan_version=row["plan_version"], reason_codes=tuple(raw_codes))
        except ValidationError as exc:
            raise LedgerError(
                "durable plan rejection has invalid reason codes; repair the ledger row before replanning"
            ) from exc

    @staticmethod
    def _attempt_from_row(row: aiosqlite.Row) -> ActionAttemptRecord:
        return ActionAttemptRecord(
            action_id=row["action_id"],
            attempt=row["attempt"],
            status=_action_status(row["status"], "action_attempts.status"),
            parameters_json=row["parameters_json"],
            expected_artifact_manifest=ExpectedArtifactManifest.model_validate_json(
                row["expected_manifest_json"]
            ),
            expected_manifest_digest=row["expected_manifest_digest"],
            expected_evidence_refs=tuple(json.loads(row["expected_evidence_refs_json"])),
            retry_policy=RetryPolicySpec.model_validate_json(row["retry_policy_json"]),
            retry_policy_fingerprint=row["retry_policy_fingerprint"],
            retry_of_attempt=row["retry_of_attempt"],
            staging_relpath=row["staging_relpath"],
            started_at=None if row["started_at"] is None else _parse_time(row["started_at"]),
            finished_at=None if row["finished_at"] is None else _parse_time(row["finished_at"]),
            failure_signature=row["failure_signature"],
            repair_class=row["repair_class"],
            repair_source=row["repair_source"],
            reason_code=row["reason_code"],
        )

    @staticmethod
    def _outcome_receipt_from_row(row: aiosqlite.Row) -> AttemptOutcomeReceiptRecord:
        return AttemptOutcomeReceiptRecord(
            action_id=row["action_id"],
            attempt=row["attempt"],
            canonical_outcome_json=row["canonical_outcome_json"],
            outcome_digest=row["outcome_digest"],
            canonical_bundle_json=row["canonical_bundle_json"],
            bundle_digest=row["bundle_digest"],
            evidence_refs=tuple(json.loads(row["evidence_refs_json"])),
            error_code=row["error_code"],
            failure_signature=row["failure_signature"],
            recorded_at=_parse_time(row["recorded_at"]),
        )

    @staticmethod
    def _gate_receipt_from_row(row: aiosqlite.Row) -> GateReceiptRecord:
        identities = _GateIdentityList.model_validate_json(row["artifacts_json"])
        return GateReceiptRecord(
            action_id=row["action_id"],
            attempt=row["attempt"],
            validator_id=row["validator_id"],
            validator_version=row["validator_version"],
            canonical_gate_decision_json=row["canonical_gate_decision_json"],
            gate_decision_digest=row["gate_decision_digest"],
            bundle_digest=row["bundle_digest"],
            artifacts=identities.items,
            evidence_refs=tuple(json.loads(row["evidence_refs_json"])),
            recorded_at=_parse_time(row["recorded_at"]),
        )

    @staticmethod
    def _validator_failure_receipt_from_row(
        row: aiosqlite.Row,
    ) -> ValidatorFailureReceiptRecord:
        return ValidatorFailureReceiptRecord(
            action_id=row["action_id"],
            attempt=row["attempt"],
            validator_id=row["validator_id"],
            validator_version=row["validator_version"],
            canonical_gate_decision_json=row["canonical_gate_decision_json"],
            gate_decision_digest=row["gate_decision_digest"],
            bundle_digest=row["bundle_digest"],
            artifact_checksums=tuple(json.loads(row["artifact_checksums_json"])),
            evidence_refs=tuple(json.loads(row["evidence_refs_json"])),
            recorded_at=_parse_time(row["recorded_at"]),
        )

    @staticmethod
    def _repair_fact_from_row(row: aiosqlite.Row) -> RepairFactRecord:
        return RepairFactRecord(
            action_id=row["action_id"],
            attempt=row["attempt"],
            repair_class=row["repair_class"],
            repair_source=row["repair_source"],
            reason_code=row["reason_code"],
            defect_codes=tuple(json.loads(row["defect_codes_json"])),
            evidence_refs=tuple(json.loads(row["evidence_refs_json"])),
            message=row["message"],
            outcome_digest=row["outcome_digest"],
            recorded_at=_parse_time(row["recorded_at"]),
        )

    @staticmethod
    def _probe_resolution_from_row(row: aiosqlite.Row) -> ProbeResolutionRecord:
        return ProbeResolutionRecord(
            original_action_id=row["original_action_id"],
            original_attempt=row["original_attempt"],
            probe_action_id=row["probe_action_id"],
            probe_attempt=row["probe_attempt"],
            operation_key=row["operation_key"],
            disposition=row["disposition"],
            evidence_refs=tuple(json.loads(row["evidence_refs_json"])),
            message=row["message"],
            original_idempotency_key=row["original_idempotency_key"],
            retry_policy=RetryPolicySpec.model_validate_json(
                row["retry_policy_json"]
            ),
            retry_policy_fingerprint=row["retry_policy_fingerprint"],
            error_code=row["error_code"],
            failure_signature=row["failure_signature"],
            resolution_digest=row["resolution_digest"],
            resolved_at=_parse_time(row["resolved_at"]),
        )


_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.RUNNING: frozenset({RunStatus.PAUSED_BUDGET, RunStatus.PAUSED_HITL, RunStatus.BLOCKED, RunStatus.CANCELLED}),
    RunStatus.PAUSED_BUDGET: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.PAUSED_HITL: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.BLOCKED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def _dump_tuple(values: tuple[str, ...]) -> str:
    return json.dumps(values, separators=(",", ":"))


def _validate_success_fact_set(commit: SuccessCommit) -> None:
    if not commit.artifacts:
        raise LedgerTransitionError(
            "success commits require artifacts; supply at least one committed artifact before marking success"
        )
    if not commit.gate_evidence:
        raise LedgerTransitionError(
            "success commits require gate evidence; supply at least one passing gate evidence record"
        )
    if len({artifact.artifact_id for artifact in commit.artifacts}) != len(commit.artifacts):
        raise LedgerTransitionError(
            "success commit repeats an artifact ID; repair the artifact manifest and retry the commit"
        )
    if any(artifact.producer_action_id != commit.action_id for artifact in commit.artifacts):
        raise LedgerTransitionError(
            "success commit contains another action's artifact; repair the artifact manifest and retry the commit"
        )
    if len({evidence.evidence_id for evidence in commit.gate_evidence}) != len(commit.gate_evidence):
        raise LedgerTransitionError(
            "success commit repeats a gate evidence ID; repair the evidence bundle and retry the commit"
        )
    artifact_checksums = tuple(sorted(artifact.sha256 for artifact in commit.artifacts))
    for evidence in commit.gate_evidence:
        if not evidence.passed:
            raise LedgerTransitionError(
                f"gate {evidence.gate} did not pass; repair the failed gate and re-run its validator"
            )
        if tuple(sorted(evidence.artifact_checksums)) != artifact_checksums:
            raise LedgerTransitionError(
                f"gate {evidence.gate} checksums do not match the committed artifacts; "
                "re-run the validator against the exact artifact set"
            )


def _canonical_success_commit(commit: SuccessCommit) -> SuccessCommit:
    """Validate canonical keys before any transaction and return only those keys."""
    validated = commit.model_copy(
        update={
            "artifacts": tuple(
                artifact.model_copy(
                    update={"relpath": canonical_artifact_key(artifact.relpath)}
                )
                for artifact in commit.artifacts
            )
        }
    )
    if len({artifact.relpath for artifact in validated.artifacts}) != len(validated.artifacts):
        raise LedgerTransitionError(
            "success commit repeats a canonical artifact path; repair the artifact manifest and retry the commit"
        )
    return validated


def _commit_signature(commit: SuccessCommit, *, idempotency_key: str) -> str:
    return json.dumps(
        {"commit": commit.model_dump(mode="json"), "idempotency_key": idempotency_key},
        sort_keys=True,
        separators=(",", ":"),
    )


def _run_status(value: str, field: str) -> RunStatus:
    try:
        return RunStatus(value)
    except ValueError as exc:
        raise LedgerError(
            f"invalid persisted {field} value {value!r}; repair or recreate the ledger before resuming"
        ) from exc


def _action_status(value: str, field: str) -> ActionStatus:
    try:
        return ActionStatus(value)
    except ValueError as exc:
        raise LedgerError(
            f"invalid persisted {field} value {value!r}; repair or recreate the ledger before resuming"
        ) from exc


def _promotion_status(value: str, intent_id: str) -> PromotionStatus:
    if value not in {"PENDING", "COMMITTED", "CONFLICT"}:
        raise LedgerError(
            f"promotion intent {intent_id} has corrupted status {value!r}; "
            "repair the ledger before reconciling artifacts"
        )
    return cast(PromotionStatus, value)


def _intent_rows_match_payload(
    rows: Sequence[aiosqlite.Row],
    payload: GateReceiptPayload,
    attempt_row: aiosqlite.Row,
) -> bool:
    expected = ExpectedArtifactManifest.model_validate_json(attempt_row["expected_manifest_json"])
    if len(rows) != len(payload.artifacts) or len(rows) != len(expected.entries):
        return False
    for ordinal, (row, identity, artifact) in enumerate(
        zip(rows, payload.artifacts, expected.entries, strict=True)
    ):
        if (
            row["action_id"] != payload.action_id
            or int(row["attempt"]) != payload.attempt
            or row["staged_relpath"] != identity.staged_relpath
            or row["canonical_relpath"] != identity.canonical_relpath
            or row["checksum"] != identity.checksum
            or row["media_type"] != artifact.media_type
            or row["evidence_role"] != artifact.evidence_role
            or row["metadata_json"] != canonical_model_json(
                _MetadataList(items=artifact.metadata)
            )
            or int(row["ordinal"]) != ordinal
            or row["bundle_digest"] != payload.bundle_digest
        ):
            return False
    return True


def _finish_status_for_outcome(outcome: object) -> ActionStatus:
    if isinstance(outcome, RetryableFailure):
        return ActionStatus.RETRY_WAIT
    if isinstance(outcome, RepairRequired):
        return ActionStatus.REPAIR_REQUIRED
    if isinstance(outcome, PermanentFailure):
        return ActionStatus.PERMANENT_FAILED
    if isinstance(outcome, Indeterminate):
        return ActionStatus.INDETERMINATE
    if isinstance(outcome, Paused):
        return ActionStatus.PAUSED
    raise LedgerTransitionError("outcome receipt must use its dedicated success or probe route")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)
