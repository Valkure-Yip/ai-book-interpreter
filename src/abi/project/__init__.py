"""Book-project layer: directory contract + 28-state pipeline state machine.

This replaces the old ``runs/<book_id>/<run_id>`` layout. Every artifact a book
produces lives under one project root, described by :class:`BookProject`, and
its progress is tracked by :class:`PipelineState` (``state/pipeline_state.json``).
"""

from __future__ import annotations

from abi.project.artifacts import (
    ArtifactConflictError,
    ArtifactReconciliationError,
    ArtifactStore,
    InjectedCrash,
    sha256_file,
)
from abi.project.layout import BookProject, slugify
from abi.project.run_ledger import (
    ActionAttemptRecord,
    ActionRecord,
    CommittedAction,
    IncidentRecord,
    LedgerConflictError,
    LedgerError,
    LedgerNotFoundError,
    LedgerTransitionError,
    PlanVersionRecord,
    PromotionIntent,
    RunLedger,
    RunRecord,
    RunSeed,
    SuccessCommit,
)
from abi.project.scaffold import ScaffoldRequest, project_dir_for, scaffold_project
from abi.project.state import HAPPY_PATH, PipelineState, Status

__all__ = [
    "HAPPY_PATH",
    "ActionAttemptRecord",
    "ActionRecord",
    "ArtifactConflictError",
    "ArtifactReconciliationError",
    "ArtifactStore",
    "BookProject",
    "CommittedAction",
    "IncidentRecord",
    "InjectedCrash",
    "LedgerConflictError",
    "LedgerError",
    "LedgerNotFoundError",
    "LedgerTransitionError",
    "PipelineState",
    "PlanVersionRecord",
    "PromotionIntent",
    "RunLedger",
    "RunRecord",
    "RunSeed",
    "ScaffoldRequest",
    "Status",
    "SuccessCommit",
    "project_dir_for",
    "scaffold_project",
    "sha256_file",
    "slugify",
]
