"""Shared context handed to every tool factory.

Tools are closures over a :class:`ToolContext` so they can read/write inside one
book project and reach the run's shared services (LLM router, agent runtime,
budget, events). The filesystem tools are sandboxed to ``project.root``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from abi.project.layout import BookProject
from abi.providers.services import RunServices
from abi.types.orchestration import RunSnapshot
from abi.types.run import RunConfig


@dataclass
class ToolContext:
    project: BookProject
    services: RunServices
    config: RunConfig | None = None
    run_id: str | None = None
    get_run_snapshot: Callable[[], RunSnapshot] | None = None

    def resolve(self, relpath: str) -> Path:
        """Resolve a project-relative path, enforcing the sandbox."""
        candidate = (self.project.root / relpath).resolve()
        if not self.project.within(candidate):
            raise ValueError(
                f"path escapes project sandbox: {relpath!r}. Tools may only read/write "
                f"inside the project root {self.project.root}."
            )
        return candidate
