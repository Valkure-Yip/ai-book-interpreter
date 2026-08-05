"""Typed public entry points for one durable dynamic book run."""

from __future__ import annotations

import shlex
import shutil
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import httpx
from pydantic import Field, model_validator

from abi.project.artifacts import sha256_file
from abi.project.layout import BookProject
from abi.project.run_ledger import (
    ActionRecord,
    AttemptOutcomeReceiptRecord,
    GateReceiptRecord,
    IncidentRecord,
    LedgerConflictError,
    LedgerNotFoundError,
    LedgerTransitionError,
    PromotionIntent,
    RunLedger,
    RunRecord,
    RunSeed,
)
from abi.project.scaffold import ScaffoldRequest, scaffold_project
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionOutcomeEnvelope,
    CanonicalResolutionEvidence,
    Paused,
    PendingHitlInterrupt,
    RunResult,
    RunSnapshot,
    RunStatus,
    UnblockRequest,
    UnblockResult,
)
from abi.types.run import RunConfig


class DynamicRunService(Protocol):
    """One already-wired dynamic controller/runtime pair."""

    async def run(self, run_id: str) -> RunResult: ...


@dataclass(frozen=True, slots=True)
class RunServiceContext:
    """Stable typed inputs shared by lifecycle, controller, and observability."""

    project: BookProject
    ledger: RunLedger
    run: RunRecord
    config: RunConfig
    events: EventLogger
    metrics: MetricsAggregator


class RunServiceFactory(Protocol):
    """Provider boundary that assembles the dynamic controller and durable loop."""

    def __call__(
        self, context: RunServiceContext
    ) -> AbstractAsyncContextManager[DynamicRunService]: ...


class RunInspection(FrozenModel):
    """One ledger-backed lifecycle inspection without projection authority."""

    run: RunRecord
    snapshot: RunSnapshot
    actions: tuple[ActionRecord, ...]
    outcome_receipts: tuple[AttemptOutcomeReceiptRecord, ...]
    gate_receipts: tuple[GateReceiptRecord, ...]
    promotion_intents: tuple[PromotionIntent, ...]
    open_incidents: tuple[IncidentRecord, ...]
    current_hitl_interrupts: tuple[CurrentHitlInterrupt, ...] = ()
    budget_spent_usd: float
    next_safe_recovery: str


class CurrentHitlInterrupt(FrozenModel):
    """One public interrupt discoverable from the current effective Paused outcome."""

    run_id: str
    action_id: str
    attempt: int = Field(ge=1)
    thread_id: str
    interrupt_id: str
    pending: PendingHitlInterrupt
    continuation_sequence: int | None = Field(default=None, ge=1)
    claim_status: Literal["UNCLAIMED", "CLAIMED", "STARTED", "RESOLVED"]
    approve_command: str


class InterruptDecisionRequest(FrozenModel):
    """Ordered human decisions for one exact public HITL interrupt."""

    run_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    interrupt_id: str = Field(min_length=1)
    decisions: tuple[Literal["approve", "reject"], ...] = Field(min_length=1)
    feedback: tuple[str | None, ...]

    @model_validator(mode="after")
    def _ordered_feedback(self) -> InterruptDecisionRequest:
        if len(self.decisions) != len(self.feedback):
            raise ValueError("feedback must align one-for-one with ordered decisions")
        return self


class HitlContinuationRequest(FrozenModel):
    """Stable Task 6 checkpoint identity and ordered decision payload."""

    run_id: str
    action_id: str
    attempt: int = Field(ge=1)
    thread_id: str
    interrupt_id: str
    decisions: tuple[Literal["approve", "reject"], ...]
    feedback: tuple[str | None, ...]


class HitlRecoveryInspection(FrozenModel):
    """Typed checkpoint inspection used before recovering a durable claim."""

    disposition: Literal["outcome", "not_started", "indeterminate"]
    outcome: ActionOutcomeEnvelope | None = None

    @model_validator(mode="after")
    def _outcome_matches_disposition(self) -> HitlRecoveryInspection:
        if (self.disposition == "outcome") != (self.outcome is not None):
            raise ValueError("only outcome recovery may carry a typed outcome")
        return self


class HitlContinuationBoundary(Protocol):
    """Provider-free boundary for safe checkpoint inspection and continuation."""

    async def inspect_hitl(self, request: HitlContinuationRequest) -> HitlRecoveryInspection: ...

    async def resume_hitl(self, request: HitlContinuationRequest) -> ActionOutcomeEnvelope: ...


class _DefaultHitlContinuation:
    """Reconstruct one registered Action and delegate only to Task 6 APIs."""

    def __init__(
        self,
        *,
        project: BookProject,
        ledger: RunLedger,
        run: RunRecord,
        config: RunConfig,
    ) -> None:
        from abi.actions.builtins import build_action_registry
        from abi.providers.services import build_run_services
        from abi.tools.context import ToolContext

        events, metrics = _observability(project, run.run_id)
        self._project = project
        self._ledger = ledger
        self._run = run
        self._services = build_run_services(config=config, events=events, metrics=metrics)
        self._registry = build_action_registry(
            tool_context=ToolContext(project=project, services=self._services, config=config)
        )

    async def _binding(self, request: HitlContinuationRequest) -> tuple[object, object, object]:
        from abi.actions.contracts import ActionExecutionContext
        from abi.types.tools import GateRuntimeMetadata

        action = await self._ledger.get_action(request.action_id)
        if (
            action.run_id != request.run_id
            or request.run_id != self._run.run_id
            or request.thread_id != f"{request.run_id}/{request.action_id}/{request.attempt}"
        ):
            raise LedgerTransitionError(
                "default HITL continuation identity differs from the durable Action"
            )
        resolved = self._registry.resolve_json(action.capability, action.parameters_json)
        context = ActionExecutionContext(
            project=self._project,
            run_id=request.run_id,
            snapshot=await self._ledger.load_snapshot(request.run_id),
            action_id=request.action_id,
            attempt=request.attempt,
            source_lang=self._run.source_lang,
            target_lang=self._run.target_lang,
            source_target=self._run.source_target,
            publication_mode=self._run.publication_mode,
            book_slug=self._run.book_slug,
            profile=self._run.profile,
            runtime_metadata=GateRuntimeMetadata(
                target_language=self._run.target_lang,
                publication_mode=self._run.publication_mode,
            ),
        )
        return resolved.definition.executor, context, resolved.parameters

    @staticmethod
    def _resume(request: HitlContinuationRequest) -> object:
        from abi.providers.agent_runtime import (
            HitlDecision,
            HitlInterruptDecision,
            HitlResume,
        )

        return HitlResume(
            interrupts=(
                HitlInterruptDecision(
                    interrupt_id=request.interrupt_id,
                    decisions=tuple(
                        HitlDecision(decision=decision, feedback=feedback)
                        for decision, feedback in zip(
                            request.decisions, request.feedback, strict=True
                        )
                    ),
                ),
            )
        )

    async def inspect_hitl(self, request: HitlContinuationRequest) -> HitlRecoveryInspection:
        from abi.providers.agent_runtime import HitlCheckpointInspection

        executor, context, parameters = await self._binding(request)
        inspect = getattr(executor, "inspect_hitl", None)
        if inspect is None:
            return HitlRecoveryInspection(disposition="indeterminate")
        raw = await inspect(context, parameters, self._resume(request))
        inspection = HitlCheckpointInspection.model_validate(raw)
        if inspection.disposition != "outcome":
            return HitlRecoveryInspection(disposition=inspection.disposition)
        assert inspection.outcome is not None
        return HitlRecoveryInspection(
            disposition="outcome",
            outcome=ActionOutcomeEnvelope(
                action_id=request.action_id,
                attempt=request.attempt,
                outcome=inspection.outcome,
            ),
        )

    async def resume_hitl(self, request: HitlContinuationRequest) -> ActionOutcomeEnvelope:
        executor, context, parameters = await self._binding(request)
        resume = getattr(executor, "resume_hitl", None)
        if resume is None:
            raise LedgerTransitionError(
                "only a registered agent Action can own a durable HITL checkpoint"
            )
        return ActionOutcomeEnvelope.model_validate(
            await resume(context, parameters, self._resume(request))
        )

    def close(self) -> None:
        self._services.flush()


def split_source_target(source_target: str) -> tuple[str, str]:
    """Split a source-target template once, preserving compound target tags."""
    if "-" not in source_target:
        raise ValueError(
            f"invalid source_target {source_target!r}; expected '{{source}}-{{target}}' "
            "like 'en-zh-hans' or 'ja-es'"
        )
    source_lang, target_lang = source_target.split("-", 1)
    if not source_lang or not target_lang:
        raise ValueError("source_target needs non-empty source and target language tags")
    return source_lang, target_lang


def _place_source(project: BookProject, source: str) -> None:
    project.source_raw.parent.mkdir(parents=True, exist_ok=True)
    if source.startswith(("http://", "https://")):
        response = httpx.get(source, follow_redirects=True, timeout=60)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "epub" in content_type or source.lower().endswith(".epub"):
            (project.root / "source/source.epub").write_bytes(response.content)
        else:
            project.source_raw.write_text(response.text, encoding="utf-8")
        return
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"source not found: {source}")
    if source_path.suffix.lower() == ".epub":
        shutil.copy2(source_path, project.root / "source" / source_path.name)
    else:
        project.source_raw.write_text(
            source_path.read_text(encoding="utf-8", errors="replace"),
            encoding="utf-8",
        )


def _observability(project: BookProject, run_id: str) -> tuple[EventLogger, MetricsAggregator]:
    return (
        EventLogger(project.root / "events.jsonl", run_id=run_id),
        MetricsAggregator(project.root / "metrics.json", run_id=run_id, book_id=project.root.name),
    )


def _required_factory(factory: RunServiceFactory | None) -> RunServiceFactory:
    return default_run_service_factory if factory is None else factory


@dataclass(frozen=True, slots=True)
class _DefaultDynamicRunService:
    context: RunServiceContext
    controller: object
    runtime: object
    services: object

    async def run(self, run_id: str) -> RunResult:
        from abi.orchestrator.controller import DynamicController
        from abi.providers.orchestration_runtime import DurableLoopRuntime

        if not isinstance(self.controller, DynamicController) or not isinstance(
            self.runtime, DurableLoopRuntime
        ):
            raise TypeError("default dynamic service has invalid controller/runtime wiring")
        await self.runtime.run(run_id=run_id, tick=self.controller.tick)
        run = await self.context.ledger.get_run(run_id)
        incidents = await self.context.ledger.list_incidents(run_id, open_only=True)
        return RunResult(
            run_id=run_id,
            status=run.status,
            cost_usd=await self.context.ledger.budget_spent_usd(run_id),
            blocked_reason=(incidents[-1].error_code if incidents else None),
        )


@asynccontextmanager
async def default_run_service_factory(
    context: RunServiceContext,
) -> AsyncIterator[DynamicRunService]:
    """Assemble the registered production controller and durable loop by default."""
    from abi.actions.builtins import build_action_registry
    from abi.orchestrator.committer import Committer
    from abi.orchestrator.controller import DynamicController
    from abi.orchestrator.dispatcher import Dispatcher
    from abi.orchestrator.projector import OutboxProjector
    from abi.orchestrator.reconcile import Reconciler
    from abi.planning.context import SnapshotBuilder
    from abi.planning.planner import Planner
    from abi.planning.policy import PolicyEngine
    from abi.planning.scheduler import Scheduler
    from abi.project.artifacts import ArtifactStore
    from abi.providers.orchestration_runtime import DurableLoopRuntime
    from abi.providers.services import build_run_services
    from abi.tools.context import ToolContext
    from abi.types.orchestration import ActionStatus
    from abi.types.tools import GateRuntimeMetadata

    services = build_run_services(
        config=context.config, events=context.events, metrics=context.metrics
    )
    registry = build_action_registry(
        tool_context=ToolContext(project=context.project, services=services, config=context.config)
    )
    artifacts = ArtifactStore(context.project, context.ledger)
    committer = Committer(
        ledger=context.ledger,
        registry=registry,
        project=context.project,
        artifacts=artifacts,
    )
    reconciler = Reconciler(
        ledger=context.ledger,
        artifacts=artifacts,
        registry=registry,
        committer=committer,
    )

    def complete_when(snapshot: RunSnapshot) -> bool:
        return any(
            action.capability == "retrospective.capture" and action.status is ActionStatus.SUCCEEDED
            for action in snapshot.actions
        )

    controller = DynamicController(
        ledger=context.ledger,
        registry=registry,
        planner=Planner(router=services.router, horizon=context.config.planner.horizon),
        policy=PolicyEngine(registry),
        snapshots=SnapshotBuilder(ledger=context.ledger, registry=registry),
        scheduler=Scheduler(max_parallel=context.config.orchestration.max_parallel_actions),
        dispatcher=Dispatcher(
            ledger=context.ledger,
            registry=registry,
            project=context.project,
            runtime_metadata=GateRuntimeMetadata(
                target_language=context.run.target_lang,
                publication_mode=context.run.publication_mode,
            ),
            source_lang=context.run.source_lang,
            source_target=context.run.source_target,
            book_slug=context.run.book_slug,
            profile=context.run.profile,
            timeout_s=float(context.config.llm.request_timeout_s),
        ),
        committer=committer,
        reconciler=reconciler,
        projector=OutboxProjector(
            ledger=context.ledger,
            events=context.events,
            status_path=context.project.status_projection,
        ),
        complete_when=complete_when,
    )
    runtime = DurableLoopRuntime(
        checkpoint_path=context.project.graph_checkpoints,
        max_cycles=context.config.orchestration.max_cycles,
        on_exhausted=controller.on_cycles_exhausted,
    )
    try:
        yield _DefaultDynamicRunService(
            context=context,
            controller=controller,
            runtime=runtime,
            services=services,
        )
    finally:
        artifacts.close()
        services.flush()


async def _select_exact_run(ledger: RunLedger) -> RunRecord:
    """Fail closed unless the project ledger owns exactly one business run."""
    runs = await ledger.list_runs()
    if not runs:
        raise LedgerNotFoundError(
            "resume requires exactly one durable business run; use make-book to create it"
        )
    if len(runs) > 1:
        raise LedgerConflictError(
            "resume found multiple durable business runs; repair the project ledger and "
            "select one stable identity explicitly"
        )
    return runs[0]


async def _drive(
    *,
    project: BookProject,
    ledger: RunLedger,
    run: RunRecord,
    config: RunConfig,
    factory: RunServiceFactory,
) -> RunResult:
    events, metrics = _observability(project, run.run_id)
    context = RunServiceContext(
        project=project,
        ledger=ledger,
        run=run,
        config=config,
        events=events,
        metrics=metrics,
    )
    async with factory(context) as service:
        result = await service.run(run.run_id)
    metrics.flush()
    if result.run_id != run.run_id:
        raise LedgerConflictError(
            "dynamic runtime returned a different run ID; preserve the durable ledger and "
            "observability identity before resuming"
        )
    return result


async def make_book(
    *,
    source: str,
    source_target: str,
    config: RunConfig,
    books_root: Path,
    book_slug: str | None = None,
    publication_mode: str = "public_domain",
    profile: str | None = None,
    project_root: Path | None = None,
    run_service_factory: RunServiceFactory | None = None,
) -> tuple[BookProject, RunResult]:
    """Create exactly one business run, then drive it by its stable identity."""
    factory = _required_factory(run_service_factory)
    source_lang, target_lang = split_source_target(source_target)
    slug = book_slug or Path(source).stem or "book"
    project = scaffold_project(
        ScaffoldRequest(
            target_root=books_root / target_lang,
            book_slug=slug,
            source_lang=source_lang,
            target_lang=target_lang,
            source_target=source_target,
            publication_mode=publication_mode,
            profile=profile,
        ),
        root=project_root,
    )
    _place_source(project, source)
    async with RunLedger.open(project.run_db) as ledger:
        prior = await ledger.list_runs()
        if prior:
            raise LedgerConflictError(
                f"project already contains run {prior[0].run_id}; use resume instead of "
                "creating a second business run"
            )
        run_id = await ledger.create_run(
            RunSeed(
                book_slug=slug,
                source_lang=source_lang,
                target_lang=target_lang,
                source_target=source_target,
                publication_mode=publication_mode,
                profile=profile,
                budget_usd=config.cost.hard_cap_usd,
            )
        )
        run = await ledger.get_run(run_id)
        return project, await _drive(
            project=project,
            ledger=ledger,
            run=run,
            config=config,
            factory=factory,
        )


async def resume(
    *,
    project_root: Path,
    config: RunConfig,
    run_service_factory: RunServiceFactory | None = None,
) -> tuple[BookProject, RunResult]:
    """Resume only the one durable run already owned by this project."""
    project = BookProject(Path(project_root).expanduser().resolve())
    if not project.exists():
        raise FileNotFoundError(f"no durable ledger at {project.run_db}; run `abi make-book` first")
    async with RunLedger.open(project.run_db) as ledger:
        run = await _select_exact_run(ledger)
        factory = _required_factory(run_service_factory)
        return project, await _drive(
            project=project,
            ledger=ledger,
            run=run,
            config=config,
            factory=factory,
        )


async def inspect_run(*, project_root: Path) -> RunInspection:
    """Read authoritative run, plan, Action, receipt, incident, and budget facts."""
    project = BookProject(Path(project_root).expanduser().resolve())
    if not project.exists():
        raise FileNotFoundError(f"no durable ledger at {project.run_db}; run `abi make-book` first")
    async with RunLedger.open(project.run_db) as ledger:
        run = await _select_exact_run(ledger)
        snapshot = await ledger.load_snapshot(run.run_id)
        actions = await ledger.list_actions(run.run_id)
        outcome_receipts = await ledger.list_attempt_outcome_receipts(run.run_id)
        gate_receipts = await ledger.list_gate_receipts(run.run_id)
        promotion_intents = await ledger.promotion_intents(run.run_id)
        open_incidents = await ledger.list_incidents(run.run_id, open_only=True)
        spent = await ledger.budget_spent_usd(run.run_id)
        current_hitl_interrupts = await _current_hitl_interrupts(
            project, ledger, run, outcome_receipts
        )
    return RunInspection(
        run=run,
        snapshot=snapshot,
        actions=actions,
        outcome_receipts=outcome_receipts,
        gate_receipts=gate_receipts,
        promotion_intents=promotion_intents,
        open_incidents=open_incidents,
        current_hitl_interrupts=current_hitl_interrupts,
        budget_spent_usd=spent,
        next_safe_recovery=_next_recovery(
            project, run.status, open_incidents, current_hitl_interrupts
        ),
    )


async def _current_hitl_interrupts(
    project: BookProject,
    ledger: RunLedger,
    run: RunRecord,
    receipts: tuple[AttemptOutcomeReceiptRecord, ...],
) -> tuple[CurrentHitlInterrupt, ...]:
    current: list[CurrentHitlInterrupt] = []
    for receipt in receipts:
        effective = await ledger.get_effective_attempt_outcome(
            receipt.action_id, receipt.attempt
        )
        envelope = ActionOutcomeEnvelope.model_validate_json(
            effective.canonical_outcome_json
        )
        if not isinstance(envelope.outcome, Paused) or envelope.outcome.reason != "hitl":
            continue
        for pending in envelope.outcome.pending_hitl_interrupts:
            try:
                claim = await ledger.get_interrupt_decision(pending.interrupt_id)
            except LedgerNotFoundError:
                claim_status = "UNCLAIMED"
            else:
                claim_status = claim.status
            decisions = [
                (
                    "approve"
                    if "approve" in review.allowed_decisions
                    else review.allowed_decisions[0]
                )
                for review in pending.action_reviews
            ]
            command = " ".join(
                [
                    "abi",
                    "approve",
                    shlex.quote(str(project.root)),
                    shlex.quote(pending.interrupt_id),
                    *[
                        token
                        for decision in decisions
                        for token in ("--decision", decision)
                    ],
                ]
            )
            current.append(
                CurrentHitlInterrupt(
                    run_id=run.run_id,
                    action_id=receipt.action_id,
                    attempt=receipt.attempt,
                    thread_id=f"{run.run_id}/{receipt.action_id}/{receipt.attempt}",
                    interrupt_id=pending.interrupt_id,
                    pending=pending,
                    continuation_sequence=effective.sequence,
                    claim_status=claim_status,
                    approve_command=command,
                )
            )
    return tuple(current)


async def approve_interrupt(
    *,
    project_root: Path,
    request: InterruptDecisionRequest,
    config: RunConfig | None = None,
    continuation: HitlContinuationBoundary | None = None,
) -> ActionOutcomeEnvelope:
    """Durably bind, safely recover/resume, and reconcile one public HITL interrupt."""
    project = BookProject(Path(project_root).expanduser().resolve())
    if not project.exists():
        raise FileNotFoundError(f"no durable ledger at {project.run_db}")
    async with RunLedger.open(project.run_db) as ledger:
        run = await _select_exact_run(ledger)
        if run.run_id != request.run_id:
            raise LedgerTransitionError("interrupt identity names a different durable run")
        claim, newly_claimed = await ledger.claim_hitl_interrupt(
            run_id=request.run_id,
            action_id=request.action_id,
            attempt=request.attempt,
            interrupt_id=request.interrupt_id,
            decisions=request.decisions,
            feedback=request.feedback,
        )
        if claim.status == "RESOLVED":
            receipts = await ledger.list_hitl_continuation_receipts(
                request.action_id, request.attempt
            )
            receipt = next(item for item in receipts if item.interrupt_id == request.interrupt_id)
            envelope = ActionOutcomeEnvelope.model_validate_json(
                receipt.canonical_outcome_json
            )
            await _reconcile_effective_outcome(project, ledger, run)
            return envelope

        continuation_request = HitlContinuationRequest(
            run_id=request.run_id,
            action_id=request.action_id,
            attempt=request.attempt,
            thread_id=claim.thread_id,
            interrupt_id=request.interrupt_id,
            decisions=request.decisions,
            feedback=request.feedback,
        )
        owned_continuation: _DefaultHitlContinuation | None = None
        boundary = continuation
        if boundary is None:
            if config is None:
                raise TypeError(
                    "default HITL continuation requires RunConfig; pass the active CLI config"
                )
            owned_continuation = _DefaultHitlContinuation(
                project=project,
                ledger=ledger,
                run=run,
                config=config,
            )
            boundary = owned_continuation
        try:
            if newly_claimed or claim.status == "CLAIMED":
                _, newly_started = await ledger.start_hitl_resume(
                    request.interrupt_id
                )
                if not newly_started:
                    raise LedgerTransitionError(
                        "HITL resume already started; inspect before any provider re-entry"
                    )
                envelope = await boundary.resume_hitl(continuation_request)
            else:
                inspection = await boundary.inspect_hitl(continuation_request)
                if inspection.disposition == "outcome":
                    assert inspection.outcome is not None
                    envelope = inspection.outcome
                else:
                    await ledger.block_hitl_resume_indeterminate(
                        request.interrupt_id,
                        message=(
                            "A claimed HITL checkpoint could not prove whether its approved tool "
                            "started; inspect side-effect evidence before any continuation."
                        ),
                    )
                    raise LedgerTransitionError(
                        "HITL continuation is indeterminate; run blocked before blind resume"
                    )
        finally:
            if owned_continuation is not None:
                owned_continuation.close()
        await ledger.record_hitl_continuation(request.interrupt_id, envelope)
        await _reconcile_effective_outcome(project, ledger, run)
        return envelope


async def _reconcile_effective_outcome(
    project: BookProject, ledger: RunLedger, run: RunRecord
) -> None:
    """Route a continuation receipt through the same deterministic business authority."""
    from abi.actions.builtins import build_action_registry
    from abi.orchestrator.committer import Committer
    from abi.orchestrator.reconcile import Reconciler
    from abi.project.artifacts import ArtifactStore

    registry = build_action_registry()
    artifacts = ArtifactStore(project, ledger)
    try:
        committer = Committer(
            ledger=ledger,
            registry=registry,
            project=project,
            artifacts=artifacts,
        )
        await Reconciler(
            ledger=ledger,
            artifacts=artifacts,
            registry=registry,
            committer=committer,
        ).reconcile(run.run_id)
    finally:
        artifacts.close()


def _next_recovery(
    project: BookProject,
    status: RunStatus,
    incidents: tuple[IncidentRecord, ...],
    current_hitl_interrupts: tuple[CurrentHitlInterrupt, ...] = (),
) -> str:
    if status is RunStatus.RUNNING:
        return f"abi resume {project.root}"
    if status is RunStatus.PAUSED_HITL:
        if current_hitl_interrupts:
            return current_hitl_interrupts[0].approve_command
        return "No current public HITL interrupt; inspect durable outcome bindings"
    if status is RunStatus.PAUSED_BUDGET:
        return f"abi unblock {project.root} --reason REASON --evidence-ref BUDGET_CHANGE"
    hitl_indeterminate = next(
        (
            incident
            for incident in incidents
            if incident.reason_code == "hitl_resume_indeterminate"
            and incident.action_id is not None
        ),
        None,
    )
    if status is RunStatus.BLOCKED and hitl_indeterminate is not None:
        source_action_id = hitl_indeterminate.action_id
        assert source_action_id is not None
        return " ".join(
            (
                "abi",
                "unblock",
                shlex.quote(str(project.root)),
                "--source-action",
                shlex.quote(source_action_id),
                "--reason",
                "REASON",
                "--evidence-ref",
                "SIDE_EFFECT_EVIDENCE",
            )
        )
    if status is RunStatus.BLOCKED and any(
        incident.repair_class == "integrity" for incident in incidents
    ):
        return (
            f"abi unblock {project.root} --source-action ACTION_ID --reason REASON "
            "--evidence-ref EVIDENCE --resolved-canonical PATH:removed|selected:SHA256"
        )
    if status is RunStatus.BLOCKED:
        return "Repair the external condition named by the open incident, then run abi resume"
    return f"No recovery action: run is terminal {status.value}"


async def cancel(*, project_root: Path) -> RunRecord:
    """Idempotently cancel work while preserving COMPLETED as terminal success."""
    project = BookProject(Path(project_root).expanduser().resolve())
    if not project.exists():
        raise FileNotFoundError(f"no durable ledger at {project.run_db}")
    async with RunLedger.open(project.run_db) as ledger:
        run = await _select_exact_run(ledger)
        return await ledger.cancel_run(run.run_id)


async def unblock(*, project_root: Path, request: UnblockRequest) -> UnblockResult:
    """Verify canonical disposition, then apply one ledger-owned recovery transaction."""
    project = BookProject(Path(project_root).expanduser().resolve())
    if not project.exists():
        raise FileNotFoundError(f"no durable ledger at {project.run_db}")
    _verify_canonical_resolutions(project, request.canonical_resolutions)
    async with RunLedger.open(project.run_db) as ledger:
        run = await _select_exact_run(ledger)
        return await ledger.unblock_run(run.run_id, request)


def _verify_canonical_resolutions(
    project: BookProject,
    resolutions: tuple[CanonicalResolutionEvidence, ...],
) -> None:
    for resolution in resolutions:
        path = project.root / resolution.canonical_relpath
        if not project.within(path):
            raise ValueError("canonical resolution escapes the project; use a portable ledger path")
        if resolution.disposition == "removed":
            if path.exists() or path.is_symlink():
                raise LedgerConflictError(
                    f"canonical {resolution.canonical_relpath} still exists; explicitly remove "
                    "or select it before unblocking"
                )
            continue
        assert resolution.sha256 is not None
        if not path.is_file() or sha256_file(path) != resolution.sha256:
            raise LedgerConflictError(
                f"canonical {resolution.canonical_relpath} does not match the selected checksum; "
                "preserve it and refresh the resolution evidence"
            )
