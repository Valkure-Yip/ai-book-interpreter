"""Typed, read-only RunLedger facts used by synchronous eval entry points."""

from __future__ import annotations

import asyncio

from abi.project.layout import BookProject
from abi.project.run_ledger import (
    GateReceiptRecord,
    IncidentRecord,
    LedgerConflictError,
    LedgerNotFoundError,
    RunLedger,
    RunRecord,
)
from abi.types._base import FrozenModel


class EvalRunFacts(FrozenModel):
    """The exact durable run facts needed by deterministic evaluation."""

    run: RunRecord
    gate_receipts: tuple[GateReceiptRecord, ...]
    open_incidents: tuple[IncidentRecord, ...]


async def _load_eval_run_facts(project: BookProject) -> EvalRunFacts:
    async with RunLedger.open(project.run_db) as ledger:
        runs = await ledger.list_runs()
        if not runs:
            raise LedgerNotFoundError("evaluation requires exactly one durable business run")
        if len(runs) != 1:
            raise LedgerConflictError("evaluation found multiple durable business runs")
        run = runs[0]
        return EvalRunFacts(
            run=run,
            gate_receipts=await ledger.list_gate_receipts(run.run_id),
            open_incidents=await ledger.list_incidents(run.run_id, open_only=True),
        )


def load_eval_run_facts(project: BookProject) -> EvalRunFacts:
    """Load one exact run through RunLedger without reviving legacy state APIs."""
    if not project.exists():
        raise FileNotFoundError(
            f"no durable run ledger at {project.run_db}; run `abi make-book` first"
        )
    return asyncio.run(_load_eval_run_facts(project))
