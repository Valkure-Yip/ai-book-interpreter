"""Pass 2 orchestration: per-chapter serial (sliding window), inter-chapter parallel."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

from abi.providers.llm.budget import BudgetExceeded
from abi.providers.llm.factory import LLMRouter
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.translate.context_builder import build_context
from abi.translate.paragraph_translator import (
    translate_paragraph,
    translate_paragraph_batch,
)
from abi.types.book import Book, Paragraph
from abi.types.glossary import Glossary
from abi.types.run import RunConfig
from abi.types.survey import BookOverview, ChapterSummary, StyleGuide
from abi.types.translation import QualityFlag, TranslationUnit


def _load_existing(out_dir: Path, paragraph_id: str) -> TranslationUnit | None:
    f = out_dir / "translate" / "paragraphs" / f"{paragraph_id}.json"
    if not f.exists():
        return None
    try:
        return TranslationUnit.model_validate_json(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_unit(out_dir: Path, unit: TranslationUnit) -> None:
    target_dir = out_dir / "translate" / "paragraphs"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{unit.paragraph_id}.json"
    tmp = target_file.with_suffix(target_file.suffix + ".tmp")
    tmp.write_text(unit.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(target_file)


async def run_translation(
    *,
    book: Book,
    glossary: Glossary,
    overview: BookOverview,
    style_guide: StyleGuide,
    config: RunConfig,
    router: LLMRouter,
    events: EventLogger,
    metrics: MetricsAggregator,
    out_dir: Path,
) -> dict[str, TranslationUnit]:
    events.event("pass.start", pass_name="translate", book_id=book.meta.book_id)
    paragraphs = book.iter_paragraphs()
    metrics.set_paragraphs_total(len(paragraphs))

    # Group paragraphs by section to enable inter-chapter parallelism.
    by_section: dict[str, list[Paragraph]] = defaultdict(list)
    for p in paragraphs:
        by_section[p.section_id].append(p)
    for sid in by_section:
        by_section[sid].sort(key=lambda p: p.position)

    chapter_summary_by_section = {c.section_id: c for c in overview.chapter_summaries}

    units: dict[str, TranslationUnit] = {}
    units_lock = asyncio.Lock()

    flagged_path = out_dir / "translate" / "flagged.jsonl"
    flagged_path.parent.mkdir(parents=True, exist_ok=True)
    flagged_path.touch(exist_ok=True)

    sem = asyncio.Semaphore(max(1, config.concurrency))

    batch_size = max(1, config.batch_size)

    async def _commit_unit(unit: TranslationUnit, section_id: str) -> None:
        """Persist + announce a single translation unit."""
        _save_unit(out_dir, unit)
        async with units_lock:
            units[unit.paragraph_id] = unit
        flag_codes = [f.code for f in unit.flags]
        if any(f.code != "passthrough" for f in unit.flags):
            with flagged_path.open("a", encoding="utf-8") as fh:
                fh.write(unit.model_dump_json() + "\n")
        events.event(
            "paragraph.translated",
            paragraph_id=unit.paragraph_id,
            section_id=section_id,
            confidence=round(unit.confidence, 3),
            flags=flag_codes,
            retries=unit.retries,
        )
        metrics.increment_paragraph(
            flagged=any(c != "passthrough" for c in flag_codes),
            failed=any(c == "schema_error" for c in flag_codes),
            flags=flag_codes,
        )
        metrics.flush()

    def _error_unit(p: Paragraph, exc: BaseException) -> TranslationUnit:
        return TranslationUnit(
            paragraph_id=p.paragraph_id,
            kind=p.kind,
            source_text=p.source_text,
            translated_text=p.source_text,
            target_language=config.target_language,
            confidence=0.0,
            flags=[
                QualityFlag(code="schema_error", detail=str(exc)[:200]),
                QualityFlag(code="passthrough", detail="error fallback"),
            ],
            notes=f"error fallback: {type(exc).__name__}",
            model=router.model,
            provider="openai-compatible",
        )

    async def _translate_one(
        p: Paragraph, chapter_sum: ChapterSummary | None, section_id: str
    ) -> None:
        async with units_lock:
            current_translations = dict(units)
        context = build_context(
            book=book,
            target=p,
            paragraphs_in_order=paragraphs,
            translations=current_translations,
            glossary=glossary,
            overview=overview,
            style_guide=style_guide,
            chapter_summary=chapter_sum,
            window_config=config.window,
        )
        try:
            unit = await translate_paragraph(
                router=router,
                paragraph=p,
                context=context,
                glossary=glossary,
                max_revision_rounds=config.max_revision_rounds,
            )
        except BudgetExceeded:
            events.event(
                "budget.hard_cap", paragraph_id=p.paragraph_id, spent=router.total_cost
            )
            raise
        except Exception as exc:
            events.event(
                "paragraph.failed",
                paragraph_id=p.paragraph_id,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            unit = _error_unit(p, exc)
        await _commit_unit(unit, section_id)

    async def _translate_batch(
        batch: list[Paragraph],
        chapter_sum: ChapterSummary | None,
        section_id: str,
    ) -> None:
        """Try batch translation; on any failure, fall back to per-paragraph."""
        async with units_lock:
            current_translations = dict(units)
        # Use the FIRST paragraph as the anchor for window calculation. All
        # paragraphs in the batch share this surrounding context.
        context = build_context(
            book=book,
            target=batch[0],
            paragraphs_in_order=paragraphs,
            translations=current_translations,
            glossary=glossary,
            overview=overview,
            style_guide=style_guide,
            chapter_summary=chapter_sum,
            window_config=config.window,
        )
        try:
            batch_units = await translate_paragraph_batch(
                router=router,
                paragraphs=batch,
                context=context,
                glossary=glossary,
            )
        except BudgetExceeded:
            events.event(
                "budget.hard_cap",
                paragraph_id=batch[0].paragraph_id,
                spent=router.total_cost,
            )
            raise
        except Exception as exc:
            events.event(
                "batch.failed",
                first_paragraph_id=batch[0].paragraph_id,
                size=len(batch),
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            batch_units = None

        if batch_units is None:
            events.event(
                "batch.fallback_to_single",
                first_paragraph_id=batch[0].paragraph_id,
                size=len(batch),
            )
            for p in batch:
                await _translate_one(p, chapter_sum, section_id)
            return

        events.event(
            "batch.translated",
            first_paragraph_id=batch[0].paragraph_id,
            size=len(batch),
        )
        for p in batch:
            await _commit_unit(batch_units[p.paragraph_id], section_id)

    async def process_chapter(section_id: str, section_paragraphs: list[Paragraph]) -> None:
        async with sem:
            chapter_sum = chapter_summary_by_section.get(section_id)
            # Walk paragraphs in order, opportunistically grouping consecutive
            # "fresh" (non-cached) paragraphs into batches up to ``batch_size``.
            i = 0
            n = len(section_paragraphs)
            while i < n:
                p = section_paragraphs[i]

                if not config.force_rerun:
                    existing = _load_existing(out_dir, p.paragraph_id)
                    if (
                        existing
                        and existing.context_window
                        and existing.context_window.glossary_version >= glossary.version
                    ):
                        async with units_lock:
                            units[p.paragraph_id] = existing
                        metrics.increment_paragraph(
                            flagged=bool(existing.flags),
                            failed=False,
                            flags=[f.code for f in existing.flags],
                        )
                        i += 1
                        continue

                # Greedily collect a run of fresh paragraphs for this batch.
                run: list[Paragraph] = [p]
                if batch_size > 1:
                    j = i + 1
                    while j < n and len(run) < batch_size:
                        nxt = section_paragraphs[j]
                        if not config.force_rerun:
                            cached = _load_existing(out_dir, nxt.paragraph_id)
                            if (
                                cached
                                and cached.context_window
                                and cached.context_window.glossary_version
                                >= glossary.version
                            ):
                                break  # Stop the run at a cache hit.
                        run.append(nxt)
                        j += 1

                if len(run) == 1:
                    await _translate_one(run[0], chapter_sum, section_id)
                    i += 1
                else:
                    await _translate_batch(run, chapter_sum, section_id)
                    i += len(run)

    await asyncio.gather(
        *[process_chapter(sid, ps) for sid, ps in by_section.items()]
    )

    events.event("pass.end", pass_name="translate", units=len(units))
    return units
