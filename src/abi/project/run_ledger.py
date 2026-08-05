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
    ActionStatus,
    ActionView,
    ArtifactRef,
    AuthorizedAction,
    GateEvidence,
    IncidentView,
    PlanPatch,
    PlanRejectionView,
    RunSnapshot,
    RunStatus,
)


class LedgerError(RuntimeError):
    """Base exception for a rejected durable-ledger operation."""


class LedgerTransitionError(LedgerError):
    """Raised when an attempted run or action transition is not legal."""


class LedgerConflictError(LedgerError):
    """Raised when a replay disagrees with a previously committed business fact."""


class LedgerNotFoundError(LedgerError):
    """Raised when an operation refers to no durable run or action."""


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
    failure_signature: str | None = None
    committed_at: datetime | None = None


class ActionAttemptRecord(FrozenModel):
    """A single immutable execution attempt, updated only with its terminal fact."""

    action_id: str
    attempt: int = Field(ge=1)
    status: ActionStatus
    started_at: datetime
    finished_at: datetime | None = None
    failure_signature: str | None = None


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
    status: str
    created_at: datetime
    resolved_at: datetime | None = None


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
    status: PromotionStatus
    created_at: datetime
    committed_at: datetime | None = None


Clock = Callable[[], datetime]


class RunLedger:
    """The sole mutable business authority for a dynamic orchestration run."""

    def __init__(self, db: aiosqlite.Connection, *, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or (lambda: datetime.now(UTC))
        self._transaction_lock = asyncio.Lock()

    @classmethod
    @asynccontextmanager
    async def open(cls, path: Path, *, clock: Clock | None = None) -> AsyncIterator[Self]:
        """Open and initialize a WAL-backed ledger at ``path``."""
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        try:
            await db.executescript(SCHEMA_SQL)
            await db.commit()
            yield cls(db, clock=clock)
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
        return rejection

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
            for action in actions:
                if action.plan_version != latest:
                    raise LedgerTransitionError(
                        f"action {action.action_id} targets plan {action.plan_version}, not {latest}; "
                        "re-authorize it against the latest plan"
                    )
                await db.execute(
                    "INSERT INTO actions (action_id, run_id, plan_version, capability, parameters_json, "
                    "dependencies_json, priority, read_set_json, write_set_json, status, idempotency_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    ),
                )
        return tuple([await self.get_action(action.action_id) for action in actions])

    async def get_action(self, action_id: str) -> ActionRecord:
        row = await self._fetch_one("SELECT * FROM actions WHERE action_id = ?", (action_id,))
        if row is None:
            raise LedgerNotFoundError(
                f"action {action_id} was not found; authorize the action before dispatching it"
            )
        return self._action_from_row(row)

    async def start_attempt(self, action_id: str) -> ActionAttemptRecord:
        now = self._now()
        async with self.transaction() as db:
            action = await self._require_action(db, action_id)
            await self._require_running_run(db, action["run_id"])
            status = _action_status(action["status"], "actions.status")
            if status not in {ActionStatus.AUTHORIZED, ActionStatus.RETRY_WAIT}:
                raise LedgerTransitionError(
                    f"action {action_id} is {status.value}; authorize or schedule a retry before starting an attempt"
                )
            cursor = await db.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 AS attempt FROM action_attempts WHERE action_id = ?",
                (action_id,),
            )
            row = await cursor.fetchone()
            assert row is not None
            attempt = int(row["attempt"])
            await db.execute(
                "UPDATE actions SET status = ?, failure_signature = NULL WHERE action_id = ?",
                (ActionStatus.RUNNING.value, action_id),
            )
            await db.execute(
                "INSERT INTO action_attempts (action_id, attempt, status, started_at) VALUES (?, ?, ?, ?)",
                (action_id, attempt, ActionStatus.RUNNING.value, now),
            )
        return ActionAttemptRecord(
            action_id=action_id, attempt=attempt, status=ActionStatus.RUNNING, started_at=_parse_time(now)
        )

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
            await db.execute(
                "UPDATE action_attempts SET status = ?, failure_signature = ?, finished_at = ? "
                "WHERE action_id = ? AND attempt = ?",
                (status.value, failure_signature, now, action_id, attempt),
            )
            await db.execute(
                "UPDATE actions SET status = ?, failure_signature = ? WHERE action_id = ?",
                (status.value, failure_signature, action_id),
            )
        return ActionAttemptRecord(
            action_id=action_id,
            attempt=attempt,
            status=status,
            started_at=_parse_time(row["started_at"]),
            finished_at=_parse_time(now),
            failure_signature=failure_signature,
        )

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
                    payload = json.dumps(
                        {"action_id": commit.action_id, "attempt": commit.attempt}, sort_keys=True
                    )
                    await db.execute(
                        "INSERT INTO event_outbox (event_id, event_name, aggregate_id, payload_json, "
                        "idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            f"action.committed:{commit.action_id}",
                            "action.committed",
                            commit.action_id,
                            payload,
                            f"action.committed:{commit.action_id}",
                            now,
                        ),
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

    async def record_incident(
        self, run_id: str, *, error_code: str, message: str, action_id: str | None = None
    ) -> IncidentRecord:
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
                db, run_id=run_id, error_code=error_code, message=message, action_id=action_id, now=now
            )

    async def create_promotion_intent(
        self,
        *,
        action_id: str,
        attempt: int,
        staged_relpath: str,
        canonical_relpath: str,
        checksum: str,
        media_type: str,
    ) -> PromotionIntent:
        """Record an immutable pending promotion before changing the filesystem."""
        canonical_key = canonical_artifact_key(canonical_relpath)
        now = self._now()
        canonical_conflict = False
        async with self.transaction() as db:
            action = await self._require_action(db, action_id)
            await self._attempt_row(db, action_id, attempt)
            cursor = await db.execute(
                "SELECT * FROM promotion_intents WHERE action_id = ? AND attempt = ? "
                "AND staged_relpath = ? AND canonical_relpath = ?",
                (action_id, attempt, staged_relpath, canonical_key),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                if existing["checksum"] != checksum or existing["media_type"] != media_type:
                    raise LedgerConflictError(
                        f"promotion for {canonical_key} disagrees with its recorded checksum; "
                        "inspect and choose the canonical artifact before retrying"
                    )
                return self._promotion_intent_from_row(existing)
            cursor = await db.execute(
                "SELECT intent_id FROM promotion_intents WHERE canonical_relpath = ?",
                (canonical_key,),
            )
            canonical_intent = await cursor.fetchone()
            if canonical_intent is not None:
                await self._insert_incident(
                    db,
                    run_id=action["run_id"],
                    error_code="artifact_checksum_conflict",
                    message=(
                        f"canonical artifact {canonical_key} already has promotion intent "
                        f"{canonical_intent['intent_id']}; inspect and choose the canonical artifact"
                    ),
                    action_id=action_id,
                    now=now,
                )
                canonical_conflict = True
            else:
                intent_id = str(uuid4())
                await db.execute(
                    "INSERT INTO promotion_intents (intent_id, action_id, attempt, staged_relpath, "
                    "canonical_relpath, checksum, media_type, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        intent_id,
                        action_id,
                        attempt,
                        staged_relpath,
                        canonical_key,
                        checksum,
                        media_type,
                        "PENDING",
                        now,
                    ),
                )
        if canonical_conflict:
            raise LedgerConflictError(
                f"canonical artifact {canonical_key} already has a promotion intent; "
                "inspect and choose the canonical artifact"
            )
        return PromotionIntent(
            intent_id=intent_id,
            action_id=action_id,
            attempt=attempt,
            staged_relpath=staged_relpath,
            canonical_relpath=canonical_key,
            checksum=checksum,
            media_type=media_type,
            status="PENDING",
            created_at=_parse_time(now),
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

    async def promotion_intents(self) -> tuple[PromotionIntent, ...]:
        """List promotion intents so a filesystem reconciler can resume them."""
        rows = await self._fetch_all("SELECT * FROM promotion_intents ORDER BY created_at, intent_id", ())
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
        now = self._now()
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
            status = _promotion_status(row["status"], intent_id)
            if status != "CONFLICT":
                await db.execute(
                    "UPDATE promotion_intents SET status = ? WHERE intent_id = ?",
                    ("CONFLICT", intent_id),
                )
            cursor = await db.execute(
                "SELECT * FROM incidents WHERE action_id = ? AND error_code = ? AND subject = ? "
                "AND status = 'OPEN'",
                (row["action_id"], error_code, intent_id),
            )
            if await cursor.fetchone() is None:
                await self._insert_incident(
                    db,
                    run_id=row["run_id"],
                    error_code=error_code,
                    message=message,
                    action_id=row["action_id"],
                    subject=intent_id,
                    now=now,
                )
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
        return await self.get_run(run_id)

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
                    message=row["message"],
                    action_id=row["action_id"],
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

    async def event_names(self) -> tuple[str, ...]:
        rows = await self._fetch_all("SELECT event_name FROM event_outbox ORDER BY created_at, event_id", ())
        return tuple(row["event_name"] for row in rows)

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
        cursor = await db.execute(
            "SELECT * FROM action_attempts WHERE action_id = ? AND attempt = ?", (action_id, attempt)
        )
        row = await cursor.fetchone()
        if row is None:
            raise LedgerNotFoundError(
                f"attempt {attempt} for {action_id} was not found; start the action before finishing it"
            )
        return row

    async def _latest_plan_version(self, db: aiosqlite.Connection, run_id: str) -> int | None:
        cursor = await db.execute("SELECT MAX(version) AS version FROM plan_versions WHERE run_id = ?", (run_id,))
        row = await cursor.fetchone()
        assert row is not None
        return None if row["version"] is None else int(row["version"])

    async def _ready_to_complete(self, db: aiosqlite.Connection, run_id: str) -> bool:
        cursor = await db.execute(
            "SELECT COUNT(*) AS total, SUM(status = ?) AS successful FROM actions WHERE run_id = ?",
            (ActionStatus.SUCCEEDED.value, run_id),
        )
        row = await cursor.fetchone()
        assert row is not None
        return int(row["total"]) > 0 and int(row["successful"] or 0) == int(row["total"])

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

    async def _insert_incident(
        self,
        db: aiosqlite.Connection,
        *,
        run_id: str,
        error_code: str,
        message: str,
        action_id: str | None,
        subject: str | None = None,
        now: str,
    ) -> IncidentRecord:
        record = IncidentRecord(
            incident_id=str(uuid4()), run_id=run_id, error_code=error_code, subject=subject, message=message,
            action_id=action_id, status="OPEN", created_at=_parse_time(now)
        )
        await db.execute(
            "INSERT INTO incidents (incident_id, run_id, action_id, error_code, subject, message, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (record.incident_id, run_id, action_id, error_code, subject, message, record.status, now),
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
            failure_signature=row["failure_signature"],
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
            status=row["status"],
            created_at=_parse_time(row["created_at"]),
            resolved_at=None if row["resolved_at"] is None else _parse_time(row["resolved_at"]),
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
            started_at=_parse_time(row["started_at"]),
            finished_at=None if row["finished_at"] is None else _parse_time(row["finished_at"]),
            failure_signature=row["failure_signature"],
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


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)
