"""Typed, read-only RunLedger facts used by synchronous eval entry points."""

from __future__ import annotations

import asyncio

from abi.project.layout import BookProject
from abi.project.run_ledger import (
    ActionAttemptRecord,
    ActionRecord,
    AttemptOutcomeReceiptRecord,
    CommittedGateEvidenceRecord,
    EffectiveAttemptOutcomeRecord,
    GateReceiptRecord,
    HitlContinuationReceiptRecord,
    IncidentRecord,
    InterruptDecisionRecord,
    LedgerConflictError,
    LedgerNotFoundError,
    OutboxEventRecord,
    PlanVersionRecord,
    ProbeResolutionRecord,
    PromotionIntent,
    RepairFactRecord,
    RunLedger,
    RunRecord,
    UnblockResolutionRecord,
)
from abi.types._base import FrozenModel
from abi.types.orchestration import RunSnapshot


class EvalRunFacts(FrozenModel):
    """The exact durable run facts needed by deterministic evaluation."""

    run: RunRecord
    snapshot: RunSnapshot
    actions: tuple[ActionRecord, ...]
    attempts: tuple[ActionAttemptRecord, ...]
    outcome_receipts: tuple[AttemptOutcomeReceiptRecord, ...]
    gate_receipts: tuple[GateReceiptRecord, ...]
    promotion_intents: tuple[PromotionIntent, ...]
    open_incidents: tuple[IncidentRecord, ...]
    plan_versions: tuple[PlanVersionRecord, ...] = ()
    repair_facts: tuple[RepairFactRecord, ...] = ()
    unblock_resolutions: tuple[UnblockResolutionRecord, ...] = ()
    probe_resolutions: tuple[ProbeResolutionRecord, ...] = ()
    interrupt_decisions: tuple[InterruptDecisionRecord, ...] = ()
    hitl_continuations: tuple[HitlContinuationReceiptRecord, ...] = ()
    effective_outcomes: tuple[EffectiveAttemptOutcomeRecord, ...] = ()
    outbox_events: tuple[OutboxEventRecord, ...] = ()
    committed_gate_evidence: tuple[CommittedGateEvidenceRecord, ...] = ()


async def _load_eval_run_facts(project: BookProject) -> EvalRunFacts:
    async with RunLedger.open(project.run_db) as ledger:
        runs = await ledger.list_runs()
        if not runs:
            raise LedgerNotFoundError("evaluation requires exactly one durable business run")
        if len(runs) != 1:
            raise LedgerConflictError("evaluation found multiple durable business runs")
        run = runs[0]
        actions = await ledger.list_actions(run.run_id)
        attempt_records: list[ActionAttemptRecord] = []
        for action in actions:
            for attempt in await ledger.attempt_numbers(action.action_id):
                attempt_records.append(await ledger.get_attempt(action.action_id, attempt))
        hitl_continuations = await ledger.list_run_hitl_continuation_receipts(run.run_id)
        effective_outcomes = tuple(
            [
                await ledger.get_effective_attempt_outcome(action_id, attempt)
                for action_id, attempt in sorted(
                    {(item.action_id, item.attempt) for item in hitl_continuations}
                )
            ]
        )
        return EvalRunFacts(
            run=run,
            snapshot=await ledger.load_snapshot(run.run_id, rejection_limit=None),
            actions=actions,
            attempts=tuple(attempt_records),
            outcome_receipts=await ledger.list_attempt_outcome_receipts(run.run_id),
            gate_receipts=await ledger.list_gate_receipts(run.run_id),
            committed_gate_evidence=await ledger.list_committed_gate_evidence(run.run_id),
            promotion_intents=await ledger.promotion_intents(run.run_id),
            plan_versions=await ledger.list_plan_versions(run.run_id),
            repair_facts=await ledger.list_repair_facts(run.run_id),
            unblock_resolutions=await ledger.list_unblock_resolutions(run.run_id),
            probe_resolutions=await ledger.list_probe_resolutions(run.run_id),
            interrupt_decisions=await ledger.list_interrupt_decisions(run.run_id),
            hitl_continuations=hitl_continuations,
            effective_outcomes=effective_outcomes,
            outbox_events=await ledger.list_outbox_events(run.run_id),
            open_incidents=await ledger.list_incidents(run.run_id, open_only=True),
        )


def load_eval_run_facts(project: BookProject) -> EvalRunFacts:
    """Load one exact run through RunLedger without reviving legacy state APIs."""
    if not project.exists():
        raise FileNotFoundError(
            f"no durable run ledger at {project.run_db}; run `abi make-book` first"
        )
    return asyncio.run(_load_eval_run_facts(project))
