"""Batch-translate every section heading in the book.

Runs once at the end of Pass 1, after the glossary is locked. Returns a
``{section_id: translated_heading}`` map persisted as ``survey/headings.json``.

Falls back to the source heading for any section the LLM omits, so callers
can always look up ``headings.get(sid, section.heading)``.
"""

from __future__ import annotations

from dataclasses import dataclass

from abi.prompts import get_registry
from abi.providers.llm.factory import LLMRouter, system_message, user_message
from abi.survey._schemas import HeadingTranslationsOutput
from abi.types.book import Book, Section
from abi.types.glossary import Glossary
from abi.types.survey import BookOverview


@dataclass(frozen=True)
class HeadingMap:
    """Container holding both the lookup table and provenance metadata."""

    by_section_id: dict[str, str]
    target_language: str

    def get(self, section_id: str, default: str) -> str:
        return self.by_section_id.get(section_id, default)


def _flatten(book: Book) -> list[Section]:
    out: list[Section] = []

    def walk(s: Section) -> None:
        out.append(s)
        for c in s.children:
            walk(c)

    for s in book.toc:
        walk(s)
    return out


def _chunk(seq: list[Section], n: int) -> list[list[Section]]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


# Cap batch size to keep prompt + output well under context limits. A typical
# academic book has 30-80 headings; one batch usually suffices. Bigger books
# get split.
_HEADINGS_PER_BATCH = 40


async def translate_headings(
    *,
    router: LLMRouter,
    book: Book,
    glossary: Glossary,
    overview: BookOverview,
    target_language: str,
) -> HeadingMap:
    """Translate every section heading. Empty / blank headings are skipped."""
    sections = [s for s in _flatten(book) if s.heading.strip()]
    if not sections:
        return HeadingMap(by_section_id={}, target_language=target_language)

    registry = get_registry()
    # Use only core glossary entries to keep prompt small.
    core_glossary = [
        {"term": e.term, "target": e.target}
        for e in glossary.entries
        if getattr(e, "is_core", False)
    ][:80]

    aggregated: dict[str, str] = {}
    for batch in _chunk(sections, _HEADINGS_PER_BATCH):
        prompt = registry.render(
            "heading_translator",
            target_language=target_language,
            book_title=book.meta.title,
            book_thesis=overview.thesis,
            glossary=core_glossary,
            headings=[
                {
                    "section_id": s.section_id,
                    "level": s.level,
                    "heading": s.heading,
                }
                for s in batch
            ],
        )
        messages = [
            system_message("You output strict JSON only. No prose, no markdown fences."),
            user_message(prompt),
        ]
        try:
            parsed, _ = await router.invoke_structured(
                HeadingTranslationsOutput,
                messages,
                agent_name="heading_translator",
                prompt_version=registry.version_for("heading_translator"),
                metadata={"batch_size": len(batch)},
            )
            for item in parsed.items:
                if item.section_id and item.translated.strip():
                    aggregated[item.section_id] = item.translated.strip()
        except Exception:
            # On failure, leave this batch untranslated; assemble layer falls
            # back to source heading. We deliberately swallow — heading
            # translation is best-effort and must not abort the run.
            continue

    return HeadingMap(by_section_id=aggregated, target_language=target_language)
