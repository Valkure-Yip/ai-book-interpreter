"""Summarize a single chapter into ChapterSummary."""

from __future__ import annotations

from abi.prompts import get_registry
from abi.providers.llm.factory import LLMRouter, system_message, user_message
from abi.survey._schemas import ChapterSummaryOutput
from abi.types.book import Section
from abi.types.survey import ChapterSummary


def _chapter_text(section: Section, max_chars: int = 6000) -> str:
    """Render chapter content (skipping code/equation blocks) up to a char budget."""
    parts: list[str] = []
    used = 0

    def walk(s: Section) -> None:
        nonlocal used
        for p in s.paragraphs:
            if p.kind in ("code", "equation"):
                continue
            if used + len(p.source_text) > max_chars:
                return
            parts.append(p.source_text)
            used += len(p.source_text)
        for c in s.children:
            walk(c)

    walk(section)
    return "\n\n".join(parts)


async def summarize_chapter(
    *,
    router: LLMRouter,
    section: Section,
    section_index: int,
    total_sections: int,
    target_language: str,
) -> ChapterSummary:
    registry = get_registry()
    chapter_text = _chapter_text(section)
    if not chapter_text.strip():
        # Empty chapter (e.g., front matter with only headings)
        return ChapterSummary(
            section_id=section.section_id,
            heading=section.heading,
            one_liner=section.heading,
            abstract="",
        )

    prompt = registry.render(
        "chapter_summarizer",
        target_language=target_language,
        heading_trail=section.heading_trail,
        section_index=section_index,
        total_sections=total_sections,
        chapter_text=chapter_text,
    )
    messages = [
        system_message("You output strict JSON only. No prose, no markdown fences."),
        user_message(prompt),
    ]
    parsed, _ = await router.invoke_structured(
        ChapterSummaryOutput,
        messages,
        agent_name="chapter_summarizer",
        prompt_version=registry.version_for("chapter_summarizer"),
        metadata={"section_id": section.section_id, "heading": section.heading},
    )
    # Attach paragraph_id for first_surface where possible.
    annotated_terms = []
    first_pid = section.paragraphs[0].paragraph_id if section.paragraphs else ""
    for t in parsed.key_terms:
        annotated_terms.append(
            t.model_copy(update={"first_surface_paragraph_id": first_pid})
        )
    return ChapterSummary(
        section_id=section.section_id,
        heading=section.heading,
        one_liner=parsed.one_liner,
        abstract=parsed.abstract,
        key_points=parsed.key_points,
        key_terms=annotated_terms,
        open_questions=parsed.open_questions,
    )
