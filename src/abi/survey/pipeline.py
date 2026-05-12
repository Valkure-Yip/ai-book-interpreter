"""Pass 1 orchestration: summaries → glossary → overview → style guide → mindmap."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from abi.providers.llm.factory import LLMRouter
from abi.providers.observability.events import EventLogger
from abi.survey.book_synthesizer import synthesize_overview
from abi.survey.chapter_summarizer import summarize_chapter
from abi.survey.glossary_builder import build_glossary
from abi.survey.heading_translator import HeadingMap, translate_headings
from abi.survey.mindmap import draw_mindmap
from abi.survey.resume import try_load_survey
from abi.survey.style_guide import derive_style_guide
from abi.types.book import Book, Section
from abi.types.glossary import Glossary
from abi.types.run import RunConfig
from abi.types.survey import BookOverview, ChapterSummary, StyleGuide


@dataclass
class SurveyResult:
    chapter_summaries: list[ChapterSummary]
    overview: BookOverview
    glossary: Glossary
    style_guide: StyleGuide
    headings: HeadingMap | None = None


_SUBSTANTIAL_PARAGRAPH_COUNT = 3


def _has_substantial_descendant_content(s: Section) -> bool:
    """True if any descendant section carries >= ``_SUBSTANTIAL_PARAGRAPH_COUNT`` paragraphs."""
    for c in s.children:
        if len(c.paragraphs) >= _SUBSTANTIAL_PARAGRAPH_COUNT:
            return True
        if _has_substantial_descendant_content(c):
            return True
    return False


def _select_chapters_for_summary(book: Book) -> list[Section]:
    """Pick the most-specific sections that carry substantial content.

    A section is a chapter candidate iff:
      - it has paragraphs of its own, AND
      - none of its descendants holds substantial paragraph content
        (so we prefer real leaf chapters over synthetic wrappers like
        ``"Front Matter"`` introduced by the TXT ingester).

    This walks the full TOC tree and emits a flat, deduplicated list.
    """
    chapters: list[Section] = []
    seen: set[str] = set()

    def walk(s: Section) -> None:
        if (
            s.paragraphs
            and not _has_substantial_descendant_content(s)
            and s.section_id not in seen
        ):
            chapters.append(s)
            seen.add(s.section_id)
        for c in s.children:
            walk(c)

    for s in book.toc:
        walk(s)
    return chapters


async def run_survey(
    *,
    book: Book,
    config: RunConfig,
    router: LLMRouter,
    events: EventLogger,
    out_dir: Path,
) -> SurveyResult:
    target_lang = config.target_language
    events.event("pass.start", pass_name="survey", book_id=book.meta.book_id)

    # Resume short-circuit: if a complete survey already exists on disk for
    # this run_dir, reuse it verbatim. We do NOT honor checkpoints when the
    # user passed --force-rerun.
    if not config.force_rerun:
        survey_dir = out_dir / "survey"
        cached = try_load_survey(survey_dir)
        if cached is not None:
            summaries, overview, glossary, style_guide, headings_map = cached
            events.event(
                "survey.resumed_from_disk",
                chapters=len(summaries),
                glossary_entries=len(glossary.entries),
                headings=len(headings_map.by_section_id),
            )
            events.event("pass.end", pass_name="survey")
            return SurveyResult(
                chapter_summaries=summaries,
                overview=overview,
                glossary=glossary,
                style_guide=style_guide,
                headings=headings_map,
            )

    chapters = _select_chapters_for_summary(book)
    events.event("survey.chapters_selected", count=len(chapters))

    # Map: per-chapter summaries, bounded concurrency.
    sem = asyncio.Semaphore(max(1, config.concurrency))

    async def run_one(idx: int, section: Section) -> ChapterSummary:
        async with sem:
            try:
                summary = await summarize_chapter(
                    router=router,
                    section=section,
                    section_index=idx + 1,
                    total_sections=len(chapters),
                    target_language=target_lang,
                )
                events.event(
                    "survey.chapter_summarized",
                    section_id=section.section_id,
                    heading=section.heading,
                )
                return summary
            except Exception as exc:  # degrade gracefully
                events.event(
                    "survey.chapter_failed",
                    section_id=section.section_id,
                    heading=section.heading,
                    error=type(exc).__name__,
                    detail=str(exc)[:200],
                )
                return ChapterSummary(
                    section_id=section.section_id,
                    heading=section.heading,
                    one_liner=section.heading,
                    abstract="",
                )

    summaries = await asyncio.gather(
        *[run_one(i, s) for i, s in enumerate(chapters)]
    )

    # Reduce: glossary + overview.
    glossary = await build_glossary(
        router=router,
        chapter_summaries=summaries,
        book_id=book.meta.book_id,
        target_language=target_lang,
    )
    events.event("survey.glossary_built", entries=len(glossary.entries))

    overview = await synthesize_overview(
        router=router,
        book=book,
        chapter_summaries=summaries,
        target_language=target_lang,
    )
    events.event("survey.overview_built", thesis_len=len(overview.thesis))

    style_guide = await derive_style_guide(
        router=router,
        overview=overview,
        style=config.style,
        target_language=target_lang,
    )

    mermaid = await draw_mindmap(
        router=router, overview=overview, target_language=target_lang
    )
    overview = overview.model_copy(update={"mindmap_mermaid": mermaid})

    headings_map = await translate_headings(
        router=router,
        book=book,
        glossary=glossary,
        overview=overview,
        target_language=target_lang,
    )
    events.event(
        "survey.headings_translated",
        count=len(headings_map.by_section_id),
    )

    # Persist artifacts.
    survey_dir = out_dir / "survey"
    survey_dir.mkdir(parents=True, exist_ok=True)
    (survey_dir / "chapters").mkdir(exist_ok=True)
    for s in summaries:
        (survey_dir / "chapters" / f"{s.section_id}.json").write_text(
            s.model_dump_json(indent=2), encoding="utf-8"
        )
    (survey_dir / "overview.json").write_text(overview.model_dump_json(indent=2), encoding="utf-8")
    (survey_dir / "glossary.json").write_text(glossary.model_dump_json(indent=2), encoding="utf-8")
    (survey_dir / "style-guide.json").write_text(
        style_guide.model_dump_json(indent=2), encoding="utf-8"
    )
    (survey_dir / "mindmap.mmd").write_text(mermaid, encoding="utf-8")
    (survey_dir / "headings.json").write_text(
        json.dumps(
            {
                "target_language": headings_map.target_language,
                "by_section_id": headings_map.by_section_id,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # Human-readable mirrors.
    (survey_dir / "overview.md").write_text(_render_overview_md(overview), encoding="utf-8")
    (survey_dir / "glossary.md").write_text(
        _render_glossary_md(glossary), encoding="utf-8"
    )

    events.event("pass.end", pass_name="survey")
    return SurveyResult(
        chapter_summaries=summaries,
        overview=overview,
        glossary=glossary,
        style_guide=style_guide,
        headings=headings_map,
    )


def _render_overview_md(overview: BookOverview) -> str:
    lines = [
        f"# {overview.title}",
        "",
        f"**Thesis**：{overview.thesis}",
        "",
        f"**Target audience**：{overview.target_audience}",
        "",
        f"**Register**：{overview.register}",
        "",
        "## Chapter summaries",
        "",
    ]
    for c in overview.chapter_summaries:
        lines.append(f"### {c.heading}")
        lines.append("")
        if c.one_liner:
            lines.append(f"> {c.one_liner}")
            lines.append("")
        if c.abstract:
            lines.append(c.abstract)
            lines.append("")
        if c.key_points:
            lines.append("**Key points**:")
            for p in c.key_points:
                lines.append(f"- {p}")
            lines.append("")
    if overview.mindmap_mermaid:
        lines.append("## Mindmap")
        lines.append("")
        lines.append("```mermaid")
        lines.append(overview.mindmap_mermaid)
        lines.append("```")
    return "\n".join(lines)


def _render_glossary_md(glossary: Glossary) -> str:
    lines = [
        f"# Glossary (v{glossary.version}, target={glossary.target_language})",
        "",
        "| Source | Target | Definition |",
        "| --- | --- | --- |",
    ]
    for e in glossary.entries:
        defn = e.definition.replace("|", "\\|") if e.definition else ""
        lines.append(f"| {e.term} | {e.target} | {defn} |")
    return "\n".join(lines)


__all__ = ["SurveyResult", "run_survey"]


# Convenience for callers that don't want pydantic to leak.
def survey_json_summary(result: SurveyResult) -> str:
    return json.dumps(
        {
            "chapters": len(result.chapter_summaries),
            "glossary_entries": len(result.glossary.entries),
            "thesis_chars": len(result.overview.thesis),
        }
    )
