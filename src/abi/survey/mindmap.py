"""Generate a Mermaid mindmap from chapter summaries."""

from __future__ import annotations

from abi.prompts import get_registry
from abi.providers.llm.factory import LLMRouter, system_message, user_message
from abi.survey._schemas import MindmapOutput
from abi.types.survey import BookOverview, ChapterSummary


def _fallback_mindmap(title: str, chapters: list[ChapterSummary]) -> str:
    lines = ["mindmap", f"  root(({title}))"]
    for c in chapters[:20]:
        lines.append(f"    {c.heading}")
        for p in c.key_points[:3]:
            lines.append(f"      {p[:40]}")
    return "\n".join(lines)


async def draw_mindmap(
    *,
    router: LLMRouter,
    overview: BookOverview,
    target_language: str,
) -> str:
    registry = get_registry()
    prompt = registry.render(
        "mindmap_drawer",
        target_language=target_language,
        title=overview.title,
        thesis=overview.thesis,
        chapters=[
            {
                "heading": c.heading,
                "one_liner": c.one_liner,
                "key_points": c.key_points[:4],
            }
            for c in overview.chapter_summaries
        ],
    )
    messages = [
        system_message("You output strict JSON only."),
        user_message(prompt),
    ]
    try:
        parsed, _ = await router.invoke_structured(
            MindmapOutput,
            messages,
            agent_name="mindmap_drawer",
            prompt_version=registry.version_for("mindmap_drawer"),
            metadata={"book_id": overview.book_id},
        )
        mermaid = parsed.mermaid.strip()
        if mermaid.startswith("mindmap"):
            return mermaid
    except Exception:
        pass
    return _fallback_mindmap(overview.title, overview.chapter_summaries)
