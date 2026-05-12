"""Intermediate ``RawBlock`` used by parsers before assigning IDs and section structure."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from abi.types.book import ParagraphKind

RawBlockKind = Literal[
    "heading", "prose", "quote", "list_item", "code", "equation",
    "figure_caption", "footnote", "citation",
]


@dataclass(frozen=True)
class RawBlock:
    """Format-agnostic block produced by a parser."""

    kind: RawBlockKind
    text: str
    level: int = 0  # for heading kind
    attrs: dict[str, str] = field(default_factory=dict)


def block_kind_to_paragraph_kind(k: RawBlockKind) -> ParagraphKind:
    return k  # types are aligned for now
