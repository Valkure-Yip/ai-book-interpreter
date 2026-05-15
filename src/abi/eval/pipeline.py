"""End-to-end eval orchestration.

Inputs:
- a prior ABI run directory (``runs/<book_id>/<run_id>/``)
- a ``RunConfig`` (provides LLM endpoint, budget, langfuse, etc.)
- an :class:`EvalConfig`

Outputs (under ``eval-out/<book_id>/<eval_id>/``):
- ``baseline/translated.md`` + ``baseline/meta.json``
- ``alignment.json``
- ``samples.jsonl`` (one aligned triple per row)
- ``mechanical.json``
- ``judge/likert.jsonl`` + ``judge/pairwise.jsonl``
- ``events.jsonl``
- ``report.json`` + ``report.md``
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from abi.eval.aggregate import aggregate
from abi.eval.alignment import align
from abi.eval.baseline import (
    BaselineResult,
    generate_baseline,
    load_baseline,
    save_baseline,
)
from abi.eval.judge import JudgeContext, judge_sample
from abi.eval.loader import AbiRunArtifacts, load_abi_run
from abi.eval.metrics import compute_mechanical
from abi.eval.report import render_markdown
from abi.eval.sampler import stratified_sample
from abi.providers.llm import build_llm_router
from abi.providers.llm.budget import BudgetExceeded
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.runtime.manifest import latest_run_for, new_run_id
from abi.types.eval import (
    AlignedTriple,
    EvalConfig,
    EvalReport,
    JudgeSampleResult,
    MechanicalReport,
)
from abi.types.run import RunConfig

_log = logging.getLogger(__name__)


@dataclass
class EvalArtifacts:
    """In-memory handle for the produced eval — everything is also on disk."""

    eval_dir: Path
    report: EvalReport
    triples: list[AlignedTriple]
    samples: list[AlignedTriple]
    judge_results: list[JudgeSampleResult]
    baseline: BaselineResult


def eval_output_root() -> Path:
    return Path.cwd() / "eval-out"


def _resolve_abi_run(book_id_or_run_id: str, *, abi_run_id: str | None) -> Path:
    """Find the ABI run dir to evaluate.

    ``book_id_or_run_id`` is what the user passed (the book file). The caller
    has already extracted ``book.meta.book_id`` from it. ``abi_run_id`` is
    "latest", an explicit run_id, or ``None`` (meaning "latest").
    """
    book_id = book_id_or_run_id
    if abi_run_id in (None, "latest"):
        run_dir = latest_run_for(book_id)
        if run_dir is None:
            raise FileNotFoundError(
                f"no prior ABI run for book_id={book_id}; "
                f"run `abi translate` first or pass --abi-run <run_id>"
            )
        return run_dir
    # Try direct path under runs/<book_id>/<run_id>.
    direct = Path.cwd() / "runs" / book_id / abi_run_id
    if direct.exists():
        return direct
    raise FileNotFoundError(
        f"no ABI run found at {direct}; check --abi-run value"
    )


async def run_eval(
    *,
    source_path: Path,
    config: RunConfig,
    eval_config: EvalConfig,
    abi_run_id: str | None = None,
    output_dir: Path | None = None,
) -> EvalArtifacts:
    """Run the full eval pipeline."""
    # First resolve which ABI run to evaluate — derive book_id from the source file.
    from abi.ir import ingest  # local import to avoid circular at module load

    book_for_id, _ = ingest(source_path)
    book_id = book_for_id.meta.book_id

    abi_run_dir = _resolve_abi_run(book_id, abi_run_id=abi_run_id)
    _log.info("evaluating ABI run %s", abi_run_dir)

    artifacts: AbiRunArtifacts = load_abi_run(abi_run_dir)
    book = artifacts.book
    units = artifacts.units
    glossary = artifacts.glossary
    register = (
        artifacts.style_guide.register if artifacts.style_guide else "academic-formal"
    )
    source_language = book.meta.source_language

    eval_id = new_run_id()
    eval_dir = (output_dir or eval_output_root()) / book_id / eval_id
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "judge").mkdir(exist_ok=True)
    (eval_dir / "baseline").mkdir(exist_ok=True)

    events = EventLogger(eval_dir / "events.jsonl", run_id=eval_id)
    metrics = MetricsAggregator(
        eval_dir / "metrics.json", run_id=eval_id, book_id=book_id
    )

    events.event(
        "eval.start",
        book_id=book_id,
        abi_run_id=abi_run_dir.name,
        eval_id=eval_id,
        samples=eval_config.samples,
        skip_baseline=eval_config.skip_baseline,
        seed=eval_config.random_seed,
    )

    # Build router (same plumbing as translate pipeline).
    router = build_llm_router(config=config, events=events, metrics=metrics)
    target_language = config.target_language

    notes: list[str] = []
    try:
        # ---- baseline ----
        baseline_dir = eval_dir / "baseline"
        baseline = None
        if eval_config.skip_baseline:
            baseline = load_baseline(baseline_dir)
            if baseline is None:
                # Look at the latest prior eval for the same book and reuse.
                prior = _latest_other_baseline(eval_output_root() / book_id, eval_id)
                if prior is not None:
                    baseline = prior
                    notes.append(f"reused baseline from prior eval {prior.meta.model}")
            if baseline is None:
                notes.append("skip_baseline=true but no cached baseline found; generating")
        if baseline is None:
            baseline = await generate_baseline(
                book=book,
                router=router,
                events=events,
                source_language=source_language,
                target_language=target_language,
                chunk_token_budget=eval_config.baseline_chunk_tokens,
            )
            save_baseline(baseline, baseline_dir)

        # ---- alignment ----
        triples, alignment_report = align(
            book=book, units=units, baseline_paragraphs=baseline.paragraphs
        )
        (eval_dir / "alignment.json").write_text(
            alignment_report.model_dump_json(indent=2), encoding="utf-8"
        )
        _write_jsonl(eval_dir / "samples.jsonl", [t.model_dump() for t in triples])
        events.event(
            "eval.aligned",
            strategy=alignment_report.strategy,
            aligned=alignment_report.aligned_pairs,
            unaligned=alignment_report.unaligned_pairs,
        )

        # ---- mechanical metrics ----
        abi_score = compute_mechanical(
            triples=triples,
            glossary=glossary,
            source_language=source_language,
            target_language=target_language,
            system="abi",
        )
        base_score = compute_mechanical(
            triples=triples,
            glossary=glossary,
            source_language=source_language,
            target_language=target_language,
            system="baseline",
        )
        mech_report = MechanicalReport(abi=abi_score, baseline=base_score)
        (eval_dir / "mechanical.json").write_text(
            mech_report.model_dump_json(indent=2), encoding="utf-8"
        )

        # ---- sampling ----
        samples = stratified_sample(
            triples,
            n_samples=eval_config.samples,
            seed=eval_config.random_seed,
        )
        events.event("eval.sampled", n=len(samples), requested=eval_config.samples)

        # ---- judge ----
        judge_router = router  # default: same router (and model) as baseline
        # If user requested a separate judge model, build a second router with it.
        if eval_config.judge_model and eval_config.judge_model != config.llm.model:
            judge_config = _swap_model(config, eval_config.judge_model, eval_config.judge_base_url)
            judge_router = build_llm_router(
                config=judge_config, events=events, metrics=metrics
            )
            notes.append(f"using separate judge model: {eval_config.judge_model}")

        judge_ctx = JudgeContext(
            source_language=source_language,
            target_language=target_language,
            register=register,
            judge_model_name=judge_router.model,
        )
        rng = random.Random(eval_config.random_seed)

        # Build a map from paragraph_id → (prev_source, next_source) for coherence prompt.
        flat = [t for t in triples]
        idx_by_pid = {t.paragraph_id: i for i, t in enumerate(flat)}

        async def _one(triple: AlignedTriple) -> JudgeSampleResult | None:
            i = idx_by_pid[triple.paragraph_id]
            prev_s = flat[i - 1].source_text if i > 0 else ""
            next_s = flat[i + 1].source_text if i + 1 < len(flat) else ""
            return await judge_sample(
                triple=triple,
                prev_source=prev_s,
                next_source=next_s,
                router=judge_router,
                events=events,
                ctx=judge_ctx,
                rng=rng,
            )

        # Run judge calls concurrently bounded by router's semaphore.
        results = await asyncio.gather(*(_one(t) for t in samples))
        judge_results: list[JudgeSampleResult] = [r for r in results if r is not None]

        _write_jsonl(
            eval_dir / "judge" / "likert.jsonl",
            [
                {
                    "paragraph_id": r.paragraph_id,
                    "abi_label": r.abi_label,
                    "likert_abi": r.likert_abi.model_dump(),
                    "likert_baseline": r.likert_baseline.model_dump(),
                }
                for r in judge_results
            ],
        )
        _write_jsonl(
            eval_dir / "judge" / "pairwise.jsonl",
            [
                {
                    "paragraph_id": r.paragraph_id,
                    "abi_label": r.abi_label,
                    "verdict": r.pairwise_verdict,
                    "rationale": r.pairwise_rationale,
                }
                for r in judge_results
            ],
        )

        # ---- aggregate + report ----
        judge_agg = aggregate(judge_results)

        report = EvalReport(
            eval_id=eval_id,
            book_id=book_id,
            abi_run_id=abi_run_dir.name,
            created_at=datetime.utcnow(),
            eval_config=eval_config,
            source_paragraphs=alignment_report.source_paragraphs,
            alignment=alignment_report,
            baseline=baseline.meta,
            mechanical=mech_report,
            judge=judge_agg,
            judge_model=judge_router.model,
            notes=notes,
        )
        (eval_dir / "report.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        (eval_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
        events.event(
            "eval.end",
            samples=judge_agg.samples,
            abi_winrate=judge_agg.pairwise_abi_winrate,
            likert_delta_mean=judge_agg.likert_delta.get("mean", 0.0),
            cost_usd=round(router.total_cost, 6),
        )
    except BudgetExceeded as exc:
        events.event("eval.aborted", reason="budget", detail=str(exc))
        _log.error("budget exceeded: %s", exc)
        raise
    finally:
        metrics.flush()
        router.flush()

    return EvalArtifacts(
        eval_dir=eval_dir,
        report=report,
        triples=triples,
        samples=samples,
        judge_results=judge_results,
        baseline=baseline,
    )


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str))
            f.write("\n")


def _latest_other_baseline(book_eval_dir: Path, exclude_eval_id: str) -> BaselineResult | None:
    if not book_eval_dir.exists():
        return None
    candidates = sorted(
        p for p in book_eval_dir.iterdir() if p.is_dir() and p.name != exclude_eval_id
    )
    for p in reversed(candidates):
        b = load_baseline(p / "baseline")
        if b is not None:
            return b
    return None


def _swap_model(config: RunConfig, model: str, base_url: str | None) -> RunConfig:
    llm = config.llm.model_copy(
        update={"model": model, **({"base_url": base_url} if base_url else {})}
    )
    return dataclasses.replace(config, llm=llm) if dataclasses.is_dataclass(config) else config.model_copy(update={"llm": llm})
