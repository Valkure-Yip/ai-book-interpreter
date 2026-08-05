"""Portable machine-managed artifact-key boundary."""

from __future__ import annotations

import pytest

from abi.types.artifact_paths import canonical_artifact_key


@pytest.mark.parametrize(
    "candidate",
    (
        "",
        "/chapters/final/001.md",
        "chapters//final/001.md",
        "chapters/./001.md",
        "chapters/../001.md",
        "chapters\\final\\001.md",
        "CHAPTERS/final/001.md",
        "chapters/final/章节.md",
        "state/staging/a1/1/001.md",
        "state/staging",
        "chapters/final/*.md",
    ),
)
def test_canonical_artifact_key_rejects_nonportable_or_staging_paths(candidate: str) -> None:
    """Catch aliases, traversal, staging keys, and patterns entering durable identity."""
    with pytest.raises(ValueError):
        canonical_artifact_key(candidate)


def test_canonical_artifact_key_preserves_a_portable_lowercase_key() -> None:
    """Catch normalization that changes the durable reservation identity."""
    assert canonical_artifact_key("chapters/final/001-a.md") == "chapters/final/001-a.md"
