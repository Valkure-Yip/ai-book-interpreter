"""Pass 0.5 — LLM-assisted TOC refinement (ingest-design.md §Pass 0.5).

When the deterministic Pass 0 heading detection yields a suspiciously flat
structure (e.g. the entire book in a single "Front Matter" section), this
module extracts *candidate heading lines* from the raw text, sends them to the
LLM as a structured-output call, and rebuilds ``book.toc`` from the response.

Design invariants:

- **Candidate-only context**: we never send the full book text.  Candidates are
  lines that *could* be headings (short, between blanks, no sentence-ending
  punctuation) — typically ≤250 lines / ~4K tokens regardless of book length.
- **Deterministic fallback**: if the LLM errors, returns empty, or produces
  anchors that don't map to real line numbers, we silently return the original
  ``Book`` unchanged.
- **Paragraph IDs stable**: we rebuild sections using the same paragraph
  stream; ``paragraph_id`` (content-hash) is preserved.  Only ``section_id``
  (heading-trail-hash) may change.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from abi.types.book import Book, Paragraph, Section
from abi.types.ids import section_id

_log = logging.getLogger(__name__)

# Maximum candidate lines to send to the LLM (keeps prompt compact).
_MAX_CANDIDATES = 250


# --------------- Pydantic schema for the LLM structured output ---------------

class TOCEntry(BaseModel):
    """One chapter/section detected by the LLM."""
    line_number: int = Field(description="1-based line number in the candidate list")
    title: str = Field(description="Chapter title in the source language")
    level: int = Field(
        default=1,
        description="Heading level: 1 for top-level chapters/parts, "
        "2 for sub-chapters, 3 for sub-sections",
    )


class TOCResponse(BaseModel):
    """Structured response from the LLM: the detected table of contents."""
    chapters: list[TOCEntry] = Field(
        description="Ordered list of chapter/section headings found in the text"
    )


# ---------------------- candidate line extraction ----------------------------

_SENTENCE_END_RE = re.compile(r"[.?!;,]\s*$")


def extract_candidates(text: str) -> list[tuple[int, str]]:
    """Return ``(1-based line_number, stripped_text)`` for heading candidates.

    A candidate is a non-empty line that is "heading-shaped":
    - Adjacent to at least one blank line (before or after),
    - Short enough (≤120 chars),
    - Does not end with sentence-ending punctuation.
    """
    lines = text.split("\n")
    out: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > 120:
            continue
        if _SENTENCE_END_RE.search(stripped):
            continue
        prev_blank = i == 0 or not lines[i - 1].strip()
        next_blank = i + 1 >= len(lines) or not lines[i + 1].strip()
        if prev_blank or next_blank:
            out.append((i + 1, stripped))
        if len(out) >= _MAX_CANDIDATES:
            break
    return out


# ----------------------------- trigger check ---------------------------------

def needs_refinement(book: Book, warnings: list[str]) -> bool:
    """True when Pass 0 produced a suspiciously flat structure.

    Triggers:
    - ``no_explicit_chapter_detected`` warning, or
    - ≤1 top-level section with >30 paragraphs (single mega-chapter).
    """
    if "no_explicit_chapter_detected" in warnings:
        return True
    if len(book.toc) <= 1:
        total_paras = len(book.iter_paragraphs())
        if total_paras > 30:
            return True
    return False


# ------------------------------ LLM call -------------------------------------

_SYSTEM_PROMPT = """\
You are a book structure analyst. Given a numbered list of candidate heading \
lines extracted from a book's source text, identify which lines are actual \
chapter/section headings and return them as a structured JSON list.

Rules:
- Only select lines that are genuine structural headings (chapter titles, part \
titles, section headings). Ignore epigraphs, author attributions, publisher \
info, and list items.
- Preserve the original line numbers exactly.
- Use level=1 for top-level divisions (Parts, main Chapters), level=2 for \
sub-chapters, level=3 for sub-sections.
- Return chapters in the order they appear (ascending line numbers).
- If you cannot identify any chapters, return an empty list.
"""


def _build_user_prompt(candidates: list[tuple[int, str]]) -> str:
    lines = ["Candidate heading lines (L<number>: text):", ""]
    for lineno, text in candidates:
        lines.append(f"L{lineno}: {text}")
    lines.append("")
    lines.append(
        "Return the detected chapter structure as JSON with the schema "
        "{chapters: [{line_number, title, level}]}."
    )
    return "\n".join(lines)


async def refine_toc_with_llm(
    book: Book,
    raw_text: str,
    *,
    router: Any,
) -> Book:
    """Call the LLM to re-detect chapter boundaries and rebuild ``book.toc``.

    Returns a new ``Book`` with the refined TOC, or the original ``book``
    unchanged on any failure.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    candidates = extract_candidates(raw_text)
    if not candidates:
        _log.info("toc_refiner: no candidates extracted, keeping heuristic TOC")
        return book

    user_prompt = _build_user_prompt(candidates)
    messages = [SystemMessage(content=_SYSTEM_PROMPT), HumanMessage(content=user_prompt)]

    try:
        response, _llm_resp = await router.invoke_structured(
            TOCResponse,
            messages,
            agent_name="toc_refiner",
            prompt_version="v1",
        )
    except Exception:
        _log.warning("toc_refiner: LLM call failed, keeping heuristic TOC", exc_info=True)
        return book

    if not response.chapters:
        _log.info("toc_refiner: LLM returned empty chapters, keeping heuristic TOC")
        return book

    # Map line numbers back to candidate texts for validation.
    cand_map: dict[int, str] = dict(candidates)
    valid_entries: list[TOCEntry] = []
    for entry in response.chapters:
        if entry.line_number in cand_map:
            valid_entries.append(entry)
        else:
            _log.warning(
                "toc_refiner: LLM returned line_number=%d not in candidates, skipping",
                entry.line_number,
            )

    if not valid_entries:
        _log.warning("toc_refiner: no valid entries after filtering, keeping heuristic TOC")
        return book

    _log.info(
        "toc_refiner: LLM returned %d valid entries (from %d candidates)",
        len(valid_entries), len(candidates),
    )

    return _rebuild_book(book, raw_text, valid_entries)


# ---------------------- TOC rebuild from LLM entries -------------------------

def _rebuild_book(book: Book, raw_text: str, entries: list[TOCEntry]) -> Book:
    """Rebuild ``book.toc`` using the LLM-detected headings.

    Strategy: walk the flat paragraph list from the original book, assign each
    paragraph to the section whose heading line_number is closest preceding it
    in the source text.  We use the raw_text line positions to map paragraphs
    to sections.
    """
    # Build heading markers: sorted by line number.
    sorted_entries = sorted(entries, key=lambda e: e.line_number)

    # Collect all paragraphs in document order from the original book.
    all_paras: list[Paragraph] = book.iter_paragraphs()
    if not all_paras:
        return book

    # For each paragraph, find its approximate line position in the raw text
    # by searching for its content.
    para_positions: list[tuple[Paragraph, int]] = []
    search_start = 0
    for p in all_paras:
        snippet = p.source_text.strip()[:60]
        pos = raw_text.find(snippet, search_start)
        if pos < 0:
            pos = raw_text.find(snippet)
        if pos >= 0:
            lineno = raw_text[:pos].count("\n") + 1
            search_start = pos + len(snippet)
        else:
            lineno = search_start  # fallback: use last known position
        para_positions.append((p, lineno))

    # Build sections: for each entry, collect paragraphs until the next entry.
    entry_linenos = [e.line_number for e in sorted_entries]

    def _section_for_para(para_line: int) -> int:
        """Return index into sorted_entries for the section owning this para.
        Returns -1 for paragraphs before the first heading (Front Matter)."""
        idx = -1
        for j, eln in enumerate(entry_linenos):
            if para_line >= eln:
                idx = j
            else:
                break
        return idx

    # Group paragraphs by section index.
    section_paras: dict[int, list[Paragraph]] = {}
    for p, pline in para_positions:
        idx = _section_for_para(pline)
        section_paras.setdefault(idx, []).append(p)

    # Build Section objects.
    new_toc: list[Section] = []

    # Front matter: paragraphs before first heading.
    if -1 in section_paras:
        fm_trail = ["Front Matter"]
        sid = section_id(fm_trail)
        new_toc.append(Section(
            section_id=sid, level=1, heading="Front Matter",
            heading_trail=fm_trail, paragraphs=section_paras[-1], children=[],
        ))

    # Build hierarchical sections from the flat entry list.
    # Simple approach: create flat sections first, then nest level>1 under
    # the preceding level=1 section.
    flat_sections: list[tuple[TOCEntry, Section]] = []
    for j, entry in enumerate(sorted_entries):
        trail = [entry.title]
        sid = section_id(trail)
        sec = Section(
            section_id=sid, level=entry.level, heading=entry.title,
            heading_trail=trail, paragraphs=section_paras.get(j, []), children=[],
        )
        flat_sections.append((entry, sec))

    # Nest: level>1 sections become children of the last level=1 section.
    for entry, sec in flat_sections:
        if entry.level <= 1 or not new_toc:
            new_toc.append(sec)
        else:
            # Find the last top-level section and add as child.
            parent = new_toc[-1]
            # Rebuild parent with the new child (Section is frozen).
            new_toc[-1] = Section(
                section_id=parent.section_id,
                level=parent.level,
                heading=parent.heading,
                heading_trail=parent.heading_trail,
                paragraphs=parent.paragraphs,
                children=[*parent.children, sec],
            )

    if not new_toc:
        _log.warning("toc_refiner: rebuild produced empty TOC, keeping original")
        return book

    return Book(meta=book.meta, toc=new_toc)
