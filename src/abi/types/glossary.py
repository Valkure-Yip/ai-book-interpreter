"""Glossary — the term-locking artifact produced by Pass 1 and refined in Pass 2."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from abi.types._base import FrozenModel


class GlossaryEntry(FrozenModel):
    term: str
    surface_forms: list[str] = Field(default_factory=list)
    target: str
    alt_targets: list[str] = Field(default_factory=list)
    definition: str = ""
    first_seen: str = ""
    locked: bool = True
    is_core: bool = False
    source: Literal["survey", "translator-proposed", "human"] = "survey"


class Glossary(FrozenModel):
    book_id: str
    target_language: str
    entries: list[GlossaryEntry] = Field(default_factory=list)
    version: int = 1

    def by_term(self) -> dict[str, GlossaryEntry]:
        return {e.term: e for e in self.entries}

    def all_surfaces(self) -> dict[str, GlossaryEntry]:
        """surface_form (lowercased) -> entry."""
        out: dict[str, GlossaryEntry] = {}
        for e in self.entries:
            for s in [e.term, *e.surface_forms]:
                out[s.lower()] = e
        return out
