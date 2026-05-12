"""Top-level pipeline orchestration: ingest → survey → translate → assemble."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from abi.assemble import AssembleResult, assemble
from abi.ir import ingest
from abi.providers.llm import build_llm_router
from abi.providers.llm.budget import BudgetExceeded
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.providers.observability.langfuse_client import LangfuseStatus
from abi.runtime.manifest import (
    find_run_dir,
    latest_run_for,
    new_run_id,
    run_directory,
    write_manifest,
)
from abi.runtime.selection import filter_book_by_chapters
from abi.survey import SurveyResult, run_survey
from abi.translate import run_translation
from abi.types.glossary import Glossary
from abi.types.run import RunConfig

_log = logging.getLogger(__name__)


@dataclass
class OrchestrationResult:
    run_dir: Path
    survey: SurveyResult | None
    assemble: AssembleResult | None
    units_translated: int
    flagged_count: int
    cost_usd: float
    langfuse_status: LangfuseStatus | None = None


async def run_pipeline(
    *,
    input_path: Path,
    config: RunConfig,
    output_dir: Path | None = None,
    survey_only: bool = False,
    title: str | None = None,
    authors: list[str] | None = None,
    chapter_selection: set[int] | None = None,
    resume: str | None = None,
) -> OrchestrationResult:
    book, warnings = ingest(input_path, title=title, authors=authors)
    if warnings:
        _log.warning("ingest warnings: %s", warnings)

    if chapter_selection:
        book, sel_warnings = filter_book_by_chapters(book, chapter_selection)
        warnings.extend(sel_warnings)
        for w in sel_warnings:
            _log.info("selection: %s", w)

    resumed = False
    if resume:
        if resume == "latest":
            existing = latest_run_for(book.meta.book_id)
            if existing is None:
                raise FileNotFoundError(
                    f"--resume latest: no prior run found for book_id={book.meta.book_id}"
                )
        else:
            existing = find_run_dir(run_id=resume, book_id=book.meta.book_id)
            if existing is None:
                raise FileNotFoundError(
                    f"--resume {resume}: run directory not found under runs/"
                )
        run_dir = existing
        run_id = run_dir.name
        resumed = True
        _log.info("resuming run %s at %s", run_id, run_dir)
    else:
        run_id = new_run_id()
        run_dir = run_directory(book.meta.book_id, run_id)

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "ir").mkdir(exist_ok=True)
    # Don't overwrite the pristine ir/book.json on resume — keeps audit trail.
    book_json = run_dir / "ir" / "book.json"
    if not resumed or not book_json.exists():
        book_json.write_text(book.model_dump_json(indent=2), encoding="utf-8")

    events = EventLogger(run_dir / "events.jsonl", run_id=run_id)
    metrics = MetricsAggregator(run_dir / "metrics.json", run_id=run_id,
                                  book_id=book.meta.book_id)
    write_manifest(run_dir, book_id=book.meta.book_id, run_id=run_id, config=config)
    events.event(
        "run.start",
        book_id=book.meta.book_id,
        title=book.meta.title,
        source_format=book.meta.source_format,
        target_language=config.target_language,
        modes=list(config.modes),
        warnings=warnings,
        resumed=resumed,
        run_id=run_id,
    )

    router = build_llm_router(config=config, events=events, metrics=metrics)

    survey_result: SurveyResult | None = None
    units: dict[str, Any] = {}
    assemble_result: AssembleResult | None = None
    cost = 0.0
    try:
        survey_result = await run_survey(
            book=book, config=config, router=router, events=events, out_dir=run_dir
        )
        if config.dry_run or survey_only:
            events.event("run.dry_run_stop", reason="survey-only")
        else:
            units = await run_translation(
                book=book,
                glossary=survey_result.glossary,
                overview=survey_result.overview,
                style_guide=survey_result.style_guide,
                config=config,
                router=router,
                events=events,
                metrics=metrics,
                out_dir=run_dir,
            )

        # Decide output dir: if explicit, also write there; default mirrors to run_dir/assemble.
        final_out = output_dir or (run_dir / "assemble")
        headings_map: dict[str, str] | None = (
            survey_result.headings.by_section_id
            if survey_result and survey_result.headings
            else None
        )
        assemble_result = assemble(
            book=book,
            units=units,
            overview=survey_result.overview if survey_result else None,
            glossary=survey_result.glossary if survey_result else Glossary(
                book_id=book.meta.book_id, target_language=config.target_language
            ),
            config=config,
            events=events,
            metrics=metrics,
            out_dir=final_out,
            headings_map=headings_map,
        )
    except BudgetExceeded as exc:
        events.event("run.aborted", reason="budget", detail=str(exc))
        _log.error("budget exceeded: %s", exc)
    finally:
        metrics.flush()
        cost = router.total_cost
        events.event(
            "run.end",
            cost_usd=round(cost, 6),
            paragraphs_done=len(units),
        )
        # Ensure Langfuse traces are sent before the process exits.
        router.flush()

    flagged_count = sum(
        1 for u in units.values()
        if any(f.code != "passthrough" for f in u.flags)
    )
    return OrchestrationResult(
        run_dir=run_dir,
        survey=survey_result,
        assemble=assemble_result,
        units_translated=len(units),
        flagged_count=flagged_count,
        cost_usd=cost,
        langfuse_status=router.langfuse_status,
    )
