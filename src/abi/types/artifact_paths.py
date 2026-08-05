"""Pure lexical validation for durable machine-managed artifact keys."""

from __future__ import annotations

import re

_COMPONENT = re.compile(r"[a-z0-9._-]+\Z")


def canonical_artifact_key(value: str) -> str:
    """Return an exact portable lowercase key or reject it without normalization."""
    if not value or value.startswith("/") or "\\" in value:
        raise ValueError("canonical artifact paths must be non-empty relative POSIX keys")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or _COMPONENT.fullmatch(part) is None for part in parts):
        raise ValueError(
            "canonical artifact path components must match lowercase ASCII [a-z0-9._-]+"
        )
    if parts[:2] == ["state", "staging"]:
        raise ValueError("canonical artifact paths may not use the state/staging namespace")
    return value


__all__ = ["canonical_artifact_key"]
