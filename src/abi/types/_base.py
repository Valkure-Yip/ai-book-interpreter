"""Common pydantic config for all domain models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Base for all domain models: immutable, strict, no extra fields."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=False,
        protected_namespaces=(),
    )
