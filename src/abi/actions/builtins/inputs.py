"""Frozen parameter schemas for every built-in ABI capability."""

from __future__ import annotations

import re

from pydantic import Field, field_validator

from abi.project.artifact_paths import canonical_artifact_key
from abi.types._base import FrozenModel

_CHAPTER_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")


class EmptyInput(FrozenModel):
    """A capability that accepts no parameters."""


class SourceIngestInput(FrozenModel):
    """Canonical source path to parse into ABI source artifacts."""

    source_relpath: str = "source/source_text_raw.txt"

    @field_validator("source_relpath")
    @classmethod
    def _portable_source_path(cls, value: str) -> str:
        return canonical_artifact_key(value)


class SourceSplitInput(FrozenModel):
    """Options for deterministic source-to-chapter splitting."""

    source_relpath: str = "source/source_text_raw.txt"
    refine_toc: bool = True
    expected_chapters: tuple[str, ...] = Field(min_length=1)

    @field_validator("source_relpath")
    @classmethod
    def _portable_source_path(cls, value: str) -> str:
        return canonical_artifact_key(value)

    @field_validator("expected_chapters")
    @classmethod
    def _ordered_expected_chapters(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(_CHAPTER_RE.fullmatch(chapter) is None for chapter in value):
            raise ValueError("expected chapter stems must use portable lowercase ASCII")
        if value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("expected chapter stems must be unique and in canonical order")
        return value


class ResearchInput(FrozenModel):
    """Optional bounded focus for a registered research Action."""

    focus: str = ""


class ChapterBatchInput(FrozenModel):
    """Explicit chapter stems whose writes may be independently authorized."""

    chapters: tuple[str, ...] = Field(min_length=1)

    @field_validator("chapters")
    @classmethod
    def _portable_unique_chapters(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("chapter names must be unique within one Action")
        if any(_CHAPTER_RE.fullmatch(chapter) is None for chapter in value):
            raise ValueError(
                "chapter names must use portable lowercase ASCII a-z, 0-9, '.', '_', or '-'"
            )
        return value


class ReviewBatchInput(FrozenModel):
    """Explicit chapters or reviewer labels covered by a review Action."""

    chapters: tuple[str, ...] = ()
    reviewers: tuple[str, ...] = ()

    @field_validator("chapters", "reviewers")
    @classmethod
    def _portable_unique_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("review batch names must be unique")
        if any(_CHAPTER_RE.fullmatch(item) is None for item in value):
            raise ValueError(
                "review batch names must use portable lowercase ASCII a-z, 0-9, '.', '_', or '-'"
            )
        return value


class SpotcheckInput(FrozenModel):
    """Controller-frozen identity and sampling inputs for one review round."""

    round_id: str = Field(pattern=r"^round_[0-9]{3}$")
    reviewers: tuple[str, ...] = Field(min_length=1)
    chapters: tuple[str, ...] = Field(min_length=1)
    samples_per_agent: int = Field(ge=1)
    seed: int = Field(ge=0)

    @field_validator("reviewers", "chapters")
    @classmethod
    def _portable_ordered_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(_CHAPTER_RE.fullmatch(item) is None for item in value):
            raise ValueError("spot-check names must use portable lowercase ASCII")
        if value != tuple(sorted(value)) or len(value) != len(set(value)):
            raise ValueError("spot-check names must be unique and in canonical order")
        return value


class BuildEpubInput(FrozenModel):
    """Fixed ABI EPUB build; paths and chapter selection are controller-owned."""


class ReleaseInput(FrozenModel):
    """Semantic release version chosen by controller policy."""

    version: str

    @field_validator("version")
    @classmethod
    def _portable_version(cls, value: str) -> str:
        if re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", value) is None:
            raise ValueError("version must use semantic form vN.N.N")
        return value if value.startswith("v") else f"v{value}"
