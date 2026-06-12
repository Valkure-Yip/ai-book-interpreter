"""Assemble RawBlocks into a Book IR (sections, IDs, validation)."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path

from abi.ir.blocks import RawBlock, block_kind_to_paragraph_kind
from abi.ir.epub import parse_epub
from abi.ir.txt import parse_txt
from abi.types.book import Book, BookMeta, Paragraph, Section, SourceFormat
from abi.types.ids import book_id, paragraph_id, section_id

# Top-level section headings that ingesters universally pull in but that carry
# no translatable *book content*. Matching is case-insensitive on the trimmed
# heading; nested sections under a kept parent are unaffected.
#
# Rationale (per top-level heading):
#   Cover / Title Page  → image-only or already in BookMeta
#   Guide               → EPUB landmarks <nav epub:type="landmarks">
#   Contents / TOC      → table of contents text duplicates ``book.toc``
#   Index               → page-number reference entries; translation is noise
#   Copyright / Imprint → legal boilerplate
_NON_CONTENT_TOP_LEVEL_HEADINGS: frozenset[str] = frozenset(
    {
        "cover",
        "title page",
        "guide",
        "contents",
        "table of contents",
        "index",
        "copyright",
        "imprint",
        "colophon",
    }
)


def _is_non_content_heading(heading: str) -> bool:
    return is_non_content_heading(heading)


def is_non_content_heading(heading: str) -> bool:
    """True for top-level scaffolding headings (Index, Guide, etc.) to drop."""
    return re.sub(r"\s+", " ", heading.strip().lower()) in _NON_CONTENT_TOP_LEVEL_HEADINGS


def _detect_format(path: Path) -> SourceFormat:
    suffix = path.suffix.lower()
    if suffix == ".txt":
        return "txt"
    if suffix == ".epub":
        return "epub"
    if suffix == ".pdf":
        return "pdf"
    raise ValueError(f"unsupported source format: {suffix} (file: {path})")


def _build_sections(blocks: list[RawBlock]) -> tuple[list[Section], list[str]]:
    """Group flat blocks into a hierarchical Section list. Returns (toc, warnings)."""
    warnings: list[str] = []

    # Strategy: walk blocks, maintain a stack of (level, Section-being-built).
    # Headings open new sections; prose goes into the deepest open section.

    # Use mutable dicts during build; convert to frozen Section at the end.
    def new_section(level: int, heading: str, trail: list[str]) -> dict:
        return {
            "level": level,
            "heading": heading,
            "trail": trail,
            "paragraphs": [],
            "children": [],
        }

    root: dict = {"level": 0, "heading": "", "trail": [], "paragraphs": [], "children": []}
    stack: list[dict] = [root]
    global_position = 0

    def deepest() -> dict:
        return stack[-1]

    # If file has no heading at all, create a synthetic chapter to host all prose.
    has_heading = any(b.kind == "heading" for b in blocks)
    if not has_heading:
        warnings.append("no_explicit_chapter_detected")
        synth = new_section(1, "Untitled", ["Untitled"])
        root["children"].append(synth)
        stack.append(synth)

    for block in blocks:
        if block.kind == "heading":
            level = max(1, block.level)
            # Pop until we are at parent's level (< new level).
            while len(stack) > 1 and stack[-1]["level"] >= level:
                stack.pop()
            parent = stack[-1]
            trail = [*parent["trail"], block.text]
            section = new_section(level, block.text, trail)
            parent["children"].append(section)
            stack.append(section)
        else:
            target = deepest()
            if target is root:
                # Prose before any heading: create a synthetic intro.
                synth = new_section(1, "Front Matter", ["Front Matter"])
                root["children"].append(synth)
                stack.append(synth)
                target = synth
            target["paragraphs"].append((block, global_position))
            global_position += 1

    # Convert mutable dicts into frozen Section models.
    def materialize(sec: dict) -> Section:
        sid = section_id(sec["trail"])
        paragraphs: list[Paragraph] = []
        for raw, pos in sec["paragraphs"]:
            kind = block_kind_to_paragraph_kind(raw.kind)
            text = raw.text
            pid = paragraph_id(text, pos)
            paragraphs.append(
                Paragraph(
                    paragraph_id=pid,
                    kind=kind,
                    source_text=text,
                    position=pos,
                    section_id=sid,
                    anchors=[],
                    attrs=raw.attrs,
                )
            )
        children = [materialize(c) for c in sec["children"]]
        return Section(
            section_id=sid,
            level=sec["level"],
            heading=sec["heading"],
            heading_trail=sec["trail"],
            paragraphs=paragraphs,
            children=children,
        )

    toc = [materialize(child) for child in root["children"]]
    return toc, warnings


def ingest(path: Path, *, title: str | None = None, authors: list[str] | None = None,
           source_language: str | None = None) -> tuple[Book, list[str]]:
    """Parse a file at ``path`` into a Book IR. Returns (book, warnings)."""
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    fmt = _detect_format(path)
    data = path.read_bytes()

    detected_meta: dict[str, str] = {}
    if fmt == "txt":
        blocks = parse_txt(path)
    elif fmt == "epub":
        blocks, detected_meta = parse_epub(path)
    else:
        raise NotImplementedError(f"format {fmt} not supported in v0.1")

    toc, warnings = _build_sections(blocks)

    # Drop top-level non-content scaffolding (EPUB landmarks, index, TOC etc.).
    # We only filter at the top level: a child section under e.g. "Notes" with
    # heading "Index" would still be retained.
    kept_toc: list[Section] = []
    for s in toc:
        if _is_non_content_heading(s.heading):
            warnings.append(f"skipped_non_content_section:{s.heading}")
        else:
            kept_toc.append(s)
    toc = kept_toc

    bid = book_id(data)
    sha256 = hashlib.sha256(data).hexdigest()

    final_title = title or detected_meta.get("title") or path.stem
    final_authors = authors or (
        [a.strip() for a in detected_meta.get("authors", "").split(",") if a.strip()] or []
    )
    final_lang = source_language or detected_meta.get("language", "en")

    meta = BookMeta(
        book_id=bid,
        title=final_title,
        authors=final_authors,
        source_language=final_lang,
        source_format=fmt,
        source_path=str(path),
        source_sha256=sha256,
        detected_at=datetime.utcnow(),
        notes=warnings,
    )

    book = Book(meta=meta, toc=toc)
    return book, warnings
