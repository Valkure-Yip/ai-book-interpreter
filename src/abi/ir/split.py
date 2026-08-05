"""Split an ingested Book IR into per-chapter source Markdown files.

Produces ``chapters/src/{NNN_slug}.md`` + ``source/toc.json`` (PDBT contract).
Each top-level TOC section becomes one chapter file; the rendered Markdown is
what the per-chapter translation stage consumes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from abi.types.book import Book, Paragraph, Section

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def _slug(text: str, *, max_len: int = 40) -> str:
    text = _SLUG_RE.sub("_", text.strip().casefold())
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:max_len] or "section"


def _render_paragraph(p: Paragraph) -> str:
    text = p.source_text.strip()
    if not text:
        return ""
    if p.kind == "heading":
        return f"## {text}"
    if p.kind == "quote":
        return "\n".join(f"> {line}" for line in text.splitlines())
    if p.kind == "code":
        return f"```\n{text}\n```"
    if p.kind == "list_item":
        return f"- {text}"
    if p.kind == "equation":
        return f"$$\n{text}\n$$"
    return text


def _collect_paragraphs(section: Section) -> list[Paragraph]:
    out: list[Paragraph] = list(section.paragraphs)
    for child in section.children:
        out.extend(_collect_paragraphs(child))
    return out


def _render_chapter(section: Section) -> str:
    lines: list[str] = [f"# {section.heading.strip()}", ""]
    paras = _collect_paragraphs(section)
    for p in paras:
        rendered = _render_paragraph(p)
        if rendered:
            lines.append(rendered)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class ChapterEntry:
    index: int
    slug: str
    title: str
    src_path: str
    paragraph_count: int


def split_book_to_chapters(book: Book, chapters_src_dir: Path) -> list[ChapterEntry]:
    """Write one Markdown file per top-level section. Returns the TOC entries."""
    chapters_src_dir.mkdir(parents=True, exist_ok=True)
    rendered = render_chapters(book)
    entries = [entry for entry, _ in rendered]
    for entry, content in rendered:
        path = chapters_src_dir / entry.src_path
        path.write_text(content, encoding="utf-8")
    return entries


def plan_chapters(book: Book) -> list[ChapterEntry]:
    """Return deterministic chapter identities without writing filesystem output."""
    entries: list[ChapterEntry] = []
    for index, section in enumerate(book.toc, start=1):
        slug = f"{index:03d}_{_slug(section.heading)}"
        entries.append(
            ChapterEntry(
                index=index,
                slug=slug,
                title=section.heading.strip(),
                src_path=f"{slug}.md",
                paragraph_count=len(_collect_paragraphs(section)),
            )
        )
    return entries


def render_chapters(book: Book) -> list[tuple[ChapterEntry, str]]:
    """Render deterministic chapter payloads in memory without filesystem effects."""
    return [
        (entry, _render_chapter(section))
        for entry, section in zip(plan_chapters(book), book.toc, strict=True)
    ]


def write_toc_json(entries: list[ChapterEntry], toc_path: Path) -> None:
    toc_path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "index": e.index,
            "slug": e.slug,
            "title": e.title,
            "src": f"chapters/src/{e.src_path}",
            "paragraphs": e.paragraph_count,
        }
        for e in entries
    ]
    toc_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
