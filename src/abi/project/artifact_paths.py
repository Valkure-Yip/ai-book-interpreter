"""Portable lexical boundary for machine-managed canonical artifact paths."""

from __future__ import annotations

import re

_PORTABLE_COMPONENT = re.compile(r"[a-z0-9._-]+")
_STAGING_PREFIX = ("state", "staging")


def canonical_artifact_key(value: str) -> str:
    """Validate and return one portable, lowercase canonical reservation key."""
    if not value or value.startswith("/") or "\\" in value:
        raise ValueError(
            "canonical artifact path must be project-relative and use '/' separators"
        )
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError("canonical artifact path may not contain empty, '.', or '..' components")
    if any(_PORTABLE_COMPONENT.fullmatch(component) is None for component in components):
        raise ValueError(
            "canonical artifact path components may contain only ASCII a-z, 0-9, '.', '_', and '-'"
        )
    if tuple(components[:2]) == _STAGING_PREFIX:
        raise ValueError("canonical artifact must be outside the staging root")
    return value
