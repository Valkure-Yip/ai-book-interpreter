"""Registered built-in book-production capabilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

from abi.actions.builtins.inputs import (
    BuildEpubInput,
    ChapterBatchInput,
    EmptyInput,
    ReleaseInput,
    ResearchInput,
    ReviewBatchInput,
    SourceIngestInput,
    SourceSplitInput,
)
from abi.actions.registry import ActionRegistry
from abi.tools.context import ToolContext
from abi.types._base import FrozenModel

if TYPE_CHECKING:
    from abi.actions.builtins.catalog import ActionEnvelope


def build_action_registry(*, tool_context: ToolContext | None = None) -> ActionRegistry:
    from abi.actions.builtins.catalog import build_action_registry as _build

    return _build(tool_context=tool_context)


def build_action_envelope(capability: str, parameters: FrozenModel) -> ActionEnvelope:
    from abi.actions.builtins.catalog import build_action_envelope as _build

    return _build(capability, parameters)

__all__ = [
    "BuildEpubInput",
    "ChapterBatchInput",
    "EmptyInput",
    "ReleaseInput",
    "ResearchInput",
    "ReviewBatchInput",
    "SourceIngestInput",
    "SourceSplitInput",
    "build_action_envelope",
    "build_action_registry",
]
