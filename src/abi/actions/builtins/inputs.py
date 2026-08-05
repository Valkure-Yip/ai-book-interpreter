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

    @field_validator("source_relpath")
    @classmethod
    def _portable_source_path(cls, value: str) -> str:
        return canonical_artifact_key(value)


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


class BuildEpubInput(FrozenModel):
    """Optional chapter subset and canonical output for an EPUB build."""

    chapter_slugs: tuple[str, ...] = ()
    output_relpath: str = "output/book.epub"

    @field_validator("chapter_slugs")
    @classmethod
    def _portable_unique_chapters(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return ChapterBatchInput._portable_unique_chapters(value) if value else value

    @field_validator("output_relpath")
    @classmethod
    def _portable_output_path(cls, value: str) -> str:
        return canonical_artifact_key(value)


class ReleaseInput(FrozenModel):
    """Optional semantic release version chosen by policy."""

    version: str | None = None

    @field_validator("version")
    @classmethod
    def _portable_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", value) is None:
            raise ValueError("version must use semantic form vN.N.N")
        return value
