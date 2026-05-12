"""Stable, pure ID functions. See docs/design-docs/data-model.md §3."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WHITESPACE_RE = re.compile(r"\s+")


def _canonicalize(text: str) -> str:
    """NFKC + collapse whitespace + strip. Pure function."""
    text = unicodedata.normalize("NFKC", text).strip()
    return _WHITESPACE_RE.sub(" ", text)


def paragraph_id(text: str, position: int) -> str:
    """Stable paragraph ID.

    Format: ``<10-hex>-<6-digit-position>``.
    Pure function of (canonicalized text, position).
    """
    if position < 0:
        raise ValueError(f"position must be >= 0, got {position}")
    canonical = _canonicalize(text)
    digest = hashlib.sha1(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"{digest}-{position:06d}"


def section_id(heading_trail: list[str]) -> str:
    """Stable section ID derived from the full heading path."""
    canonical = " > ".join(_canonicalize(h) for h in heading_trail)
    return hashlib.sha1(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def book_id(source_bytes: bytes) -> str:
    """Stable book ID. Pure function of the source file bytes."""
    return hashlib.sha1(source_bytes, usedforsecurity=False).hexdigest()[:12]
