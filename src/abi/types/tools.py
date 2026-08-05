"""Internal tool-runtime bindings for scoped action harnesses."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import Field, field_validator

from abi.types._base import FrozenModel

ToolCallable = Callable[..., object] | Callable[..., Awaitable[object]]


@dataclass(frozen=True, slots=True)
class ToolBinding:
    """A non-persisted binding between a declared tool and its implementation."""

    name: str
    description: str
    args_schema: type[FrozenModel]
    callable: ToolCallable


class ReviewActionIdentity(FrozenModel):
    """Controller-owned identity used to derive isolated reviewer checkpoints."""

    run_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)


class GateRuntimeMetadata(FrozenModel):
    """Controller-owned immutable metadata consumed by deterministic gate code."""

    target_language: str = Field(min_length=1)
    publication_mode: str = "public_domain"

    @field_validator("publication_mode")
    @classmethod
    def _known_publication_mode(cls, value: str) -> str:
        if value not in {"public_domain", "licensed", "private_use"}:
            raise ValueError("publication_mode must be public_domain, licensed, or private_use")
        return value
