"""Book-project layer: directory contract + 28-state pipeline state machine.

This replaces the old ``runs/<book_id>/<run_id>`` layout. Every artifact a book
produces lives under one project root, described by :class:`BookProject`, and
its progress is tracked by :class:`PipelineState` (``state/pipeline_state.json``).
"""

from __future__ import annotations

from abi.project.layout import BookProject, slugify
from abi.project.scaffold import ScaffoldRequest, project_dir_for, scaffold_project
from abi.project.state import HAPPY_PATH, PipelineState, Status

__all__ = [
    "HAPPY_PATH",
    "BookProject",
    "PipelineState",
    "ScaffoldRequest",
    "Status",
    "project_dir_for",
    "scaffold_project",
    "slugify",
]
