"""Build the sliding-window context for a single paragraph translation.

See docs/design-docs/sliding-window.md for the spec.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from abi.types.book import Book, Paragraph, Section
from abi.types.glossary import Glossary, GlossaryEntry
from abi.types.run import WindowConfig
from abi.types.survey import BookOverview, ChapterSummary, StyleGuide
from abi.types.translation import TranslationUnit

# Anchor extraction patterns (conservative: only well-known shapes).
_ANCHOR_PATTERNS = [
    re.compile(r"\[\d+\]"),                       # [12]
    re.compile(r"\[\w+\d+\]"),                    # [Smith2020]
    re.compile(r"\bFig(?:ure|\.)\s*\d+(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\bTable\s*\d+(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\bEq(?:uation|\.)\s*\(?\d+(?:\.\d+)?\)?", re.IGNORECASE),
]


@dataclass(frozen=True)
class WindowParagraph:
    offset: int
    id: str
    source: str
    translated: str | None


@dataclass(frozen=True)
class TranslationContext:
    target: WindowParagraph
    target_kind: str
    prev_window: list[WindowParagraph]
    next_window: list[WindowParagraph]
    glossary_slice: list[GlossaryEntry]
    heading_trail: list[str]
    chapter_abstract: str
    chapter_length: int
    position_in_chapter: int
    crossed_chapter_boundary: bool
    anchors: list[str]
    trimmed_reasons: list[str] = field(default_factory=list)
    target_language: str = "zh"
    source_language: str = "en"
    register: str = "academic-formal"
    quote_style: str = "「」"
    directives: list[str] = field(default_factory=list)
    book_thesis: str = ""
    book_audience: str = ""


def extract_anchors(text: str) -> list[str]:
    found: list[str] = []
    for pat in _ANCHOR_PATTERNS:
        found.extend(m.group(0) for m in pat.finditer(text))
    # Preserve order, dedupe.
    seen: set[str] = set()
    out: list[str] = []
    for a in found:
        if a not in seen:
            out.append(a)
            seen.add(a)
    return out


def _select_glossary_slice(
    target_text: str,
    window_text: str,
    glossary: Glossary,
    max_entries: int,
) -> list[GlossaryEntry]:
    must_include: dict[str, GlossaryEntry] = {}
    surfaces = glossary.all_surfaces()
    haystack = f"{target_text}\n{window_text}".lower()
    for surface, entry in surfaces.items():
        if surface and surface in haystack:
            must_include[entry.term] = entry

    # Add core terms next.
    core_terms = [e for e in glossary.entries if e.is_core and e.term not in must_include]
    extras: list[GlossaryEntry] = list(must_include.values()) + core_terms

    # Pad with remaining entries up to max_entries.
    if len(extras) < max_entries:
        existing = {e.term for e in extras}
        for e in glossary.entries:
            if e.term not in existing:
                extras.append(e)
                if len(extras) >= max_entries:
                    break
    return extras[:max_entries]


def _find_section_for(book: Book, section_id: str) -> Section | None:
    for s in book.iter_sections():
        if s.section_id == section_id:
            return s
    return None


def build_context(
    *,
    book: Book,
    target: Paragraph,
    paragraphs_in_order: list[Paragraph],
    translations: dict[str, TranslationUnit],
    glossary: Glossary,
    overview: BookOverview,
    style_guide: StyleGuide,
    chapter_summary: ChapterSummary | None,
    window_config: WindowConfig,
) -> TranslationContext:
    """Construct everything needed to translate ``target`` in context."""

    # Build paragraph position index for O(1) neighbor lookup.
    by_position = {p.position: p for p in paragraphs_in_order}
    positions = sorted(by_position)
    idx_in_order = positions.index(target.position) if target.position in by_position else -1

    # If the target's chapter is short enough, override the sliding window
    # with the FULL chapter (prev = everything before target, next = everything
    # after target) so each paragraph sees the same global context that a naive
    # single-prompt baseline would have. This eliminates ABI's structural
    # disadvantage on short documents (news commentary, essays, short stories)
    # without affecting full-length book translation behavior.
    section_for_target = _find_section_for(book, target.section_id)
    chapter_paragraphs_full = (
        section_for_target.paragraphs if section_for_target else []
    )
    use_full_chapter = (
        window_config.short_chapter_threshold > 0
        and 0 < len(chapter_paragraphs_full) <= window_config.short_chapter_threshold
    )

    trimmed_reasons: list[str] = []
    prev_paragraphs: list[Paragraph] = []
    next_paragraphs: list[Paragraph] = []
    if use_full_chapter:
        try:
            i_in_chapter = chapter_paragraphs_full.index(target)
        except ValueError:
            i_in_chapter = 0
        prev_paragraphs = list(chapter_paragraphs_full[:i_in_chapter])
        next_paragraphs = list(chapter_paragraphs_full[i_in_chapter + 1 :])
        trimmed_reasons.append(
            f"short_chapter:full_context({len(chapter_paragraphs_full)})"
        )
    else:
        for off in range(1, window_config.before + 1):
            if idx_in_order - off < 0:
                break
            prev_paragraphs.append(by_position[positions[idx_in_order - off]])
        prev_paragraphs.reverse()  # earliest first

        for off in range(1, window_config.after + 1):
            if idx_in_order + off >= len(positions):
                break
            next_paragraphs.append(by_position[positions[idx_in_order + off]])

    crossed_boundary = any(p.section_id != target.section_id for p in prev_paragraphs)

    prev_window: list[WindowParagraph] = []
    for off, p in enumerate(prev_paragraphs, start=1):
        unit = translations.get(p.paragraph_id)
        prev_window.append(
            WindowParagraph(
                offset=len(prev_paragraphs) - off + 1,
                id=p.paragraph_id,
                source=p.source_text,
                translated=unit.translated_text if unit else None,
            )
        )

    next_window: list[WindowParagraph] = []
    for off, p in enumerate(next_paragraphs, start=1):
        next_window.append(
            WindowParagraph(offset=off, id=p.paragraph_id, source=p.source_text, translated=None)
        )

    # We already resolved the target's section above (for the full-chapter
    # override). Reuse it here.
    section = section_for_target
    heading_trail = section.heading_trail if section else []
    chapter_abstract = (chapter_summary.abstract[: window_config.chapter_abstract_max_chars]
                        if chapter_summary else "")
    chapter_paragraphs = chapter_paragraphs_full
    chapter_length = max(1, len(chapter_paragraphs))
    try:
        position_in_chapter = chapter_paragraphs.index(target) + 1
    except ValueError:
        position_in_chapter = 1

    window_text = "\n".join(
        p.source_text for p in (prev_paragraphs + next_paragraphs)
    )
    glossary_slice = _select_glossary_slice(
        target.source_text, window_text, glossary, window_config.glossary_max
    )
    anchors = extract_anchors(target.source_text)

    target_wp = WindowParagraph(
        offset=0, id=target.paragraph_id, source=target.source_text, translated=None
    )

    return TranslationContext(
        target=target_wp,
        target_kind=target.kind,
        prev_window=prev_window,
        next_window=next_window,
        glossary_slice=glossary_slice,
        heading_trail=heading_trail,
        chapter_abstract=chapter_abstract,
        chapter_length=chapter_length,
        position_in_chapter=position_in_chapter,
        crossed_chapter_boundary=crossed_boundary,
        anchors=anchors,
        trimmed_reasons=trimmed_reasons,
        target_language=glossary.target_language,
        source_language=book.meta.source_language,
        register=style_guide.register,  # type: ignore[arg-type]
        quote_style=style_guide.quote_style,
        directives=style_guide.register_directives,
        book_thesis=overview.thesis,
        book_audience=overview.target_audience,
    )
