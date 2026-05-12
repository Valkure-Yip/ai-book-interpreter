"""Reduce chapter summaries into a BookOverview."""

from __future__ import annotations

from abi.prompts import get_registry
from abi.providers.llm.factory import LLMRouter, system_message, user_message
from abi.survey._schemas import BookOverviewOutput
from abi.types.book import Book
from abi.types.survey import BookOverview, ChapterSummary


async def synthesize_overview(
    *,
    router: LLMRouter,
    book: Book,
    chapter_summaries: list[ChapterSummary],
    target_language: str,
) -> BookOverview:
    registry = get_registry()
    prompt = registry.render(
        "book_synthesizer",
        target_language=target_language,
        title=book.meta.title,
        authors=book.meta.authors,
        chapters=[
            {
                "heading": c.heading,
                "abstract": c.abstract,
                "key_points": c.key_points,
            }
            for c in chapter_summaries
        ],
    )
    messages = [
        system_message("You output strict JSON only."),
        user_message(prompt),
    ]
    parsed, _ = await router.invoke_structured(
        BookOverviewOutput,
        messages,
        agent_name="book_synthesizer",
        prompt_version=registry.version_for("book_synthesizer"),
        metadata={"book_id": book.meta.book_id},
    )
    return BookOverview(
        book_id=book.meta.book_id,
        title=book.meta.title,
        thesis=parsed.thesis,
        target_audience=parsed.target_audience,
        register=parsed.register,
        tone_notes=parsed.tone_notes,
        chapter_summaries=chapter_summaries,
        mindmap_mermaid="",  # filled in later by mindmap_drawer
    )
