"""Internal tool-runtime bindings for scoped action harnesses."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from abi.types._base import FrozenModel

ToolCallable = Callable[..., object] | Callable[..., Awaitable[object]]


@dataclass(frozen=True, slots=True)
class ToolBinding:
    """A non-persisted binding between a declared tool and its implementation."""

    name: str
    description: str
    args_schema: type[FrozenModel]
    callable: ToolCallable
