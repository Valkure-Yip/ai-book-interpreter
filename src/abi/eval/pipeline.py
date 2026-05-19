"""End-to-end eval orchestration.

Inputs:
- a prior ABI run directory (``runs/<book_id>/<run_id>/``), OR
  a dataset spec like ``wmt24pp:en-zh_CN:literary`` (+ optional ``auto_translate``)
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
import json
import logging
import os
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from abi.eval.aggregate import aggregate
from abi.eval.alignment import align
from abi.eval.baseline import (
    BaselineResult,
    generate_baseline,
    load_baseline,
    save_baseline,
)
from abi.eval.datasets import EvalDataset, load_eval_dataset, materialize_to_book_file
from abi.eval.judge import JudgeContext, judge_sample
from abi.eval.langfuse_experiment import (
    DISABLED,
    ExperimentContext,
    attach_scores,
    finalize_run,
    fresh_trace_id,
    link_sample,
    start_dataset_run,
)
from abi.eval.loader import AbiRunArtifacts, load_abi_run
from abi.eval.metrics import compute_mechanical
from abi.eval.report import render_markdown
from abi.eval.sampler import stratified_sample
from abi.providers.llm import build_llm_router
from abi.providers.llm.budget import BudgetExceeded
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.providers.observability.langfuse_client import get_langfuse_client
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


def _materialize_root() -> Path:
    """Where dataset-derived source files live (stable across runs)."""
    runs_root_env = os.environ.get("ABI_RUNS_DIR")
    root = Path(runs_root_env) if runs_root_env else Path.cwd() / "runs"
    return root / "_eval_inputs"


def _resolve_abi_run(book_id_or_run_id: str, *, abi_run_id: str | None) -> Path:
    """Find the ABI run dir to evaluate."""
    book_id = book_id_or_run_id
    if abi_run_id in (None, "latest"):
        run_dir = latest_run_for(book_id)
        if run_dir is None:
            raise FileNotFoundError(
                f"no prior ABI run for book_id={book_id}; "
                f"run `abi translate` first or pass --abi-run <run_id> / --auto-translate"
            )
        return run_dir
    assert abi_run_id is not None  # narrowed by check above
    direct = Path.cwd() / "runs" / book_id / abi_run_id
    if direct.exists():
        return direct
    raise FileNotFoundError(
        f"no ABI run found at {direct}; check --abi-run value"
    )


def _resolve_judge_model(config: RunConfig, eval_config: EvalConfig) -> str:
    """CLI > env > main ``LLM_MODEL``."""
    if eval_config.judge_model:
        return eval_config.judge_model
    env_model = os.environ.get("EVAL_JUDGE_MODEL", "").strip()
    if env_model:
        return env_model
    return config.llm.model


async def run_eval(
    *,
    source_path: Path | None = None,
    config: RunConfig,
    eval_config: EvalConfig,
    abi_run_id: str | None = None,
    output_dir: Path | None = None,
) -> EvalArtifacts:
    """Run the full eval pipeline.

    Either ``source_path`` (local book) or ``eval_config.dataset_spec`` (HF
    dataset) must be set. When a dataset is used, ``auto_translate=True``
    will run ABI translation on the materialized dataset before evaluating;
    otherwise an existing ABI run for the dataset's stable book_id is
    required.
    """
    from abi.ir import ingest  # local import to avoid circular at module load

    # ---- resolve source: dataset spec or local file ----
    dataset: EvalDataset | None = None
    references: list[str] | None = None
    document_ids: list[str] | None = None

    if eval_config.dataset_spec:
        # Apply ``limit_docs`` by injecting it into the spec if it was given
        # via the CLI as a separate flag (--limit-docs) rather than embedded
        # in the spec string.
        effective_spec = eval_config.dataset_spec
        if (
            eval_config.limit_docs
            and "limit_docs=" not in effective_spec
        ):
            sep = ":" if not effective_spec.endswith(":") else ""
            effective_spec = f"{effective_spec}{sep}limit_docs={eval_config.limit_docs}"
        dataset = load_eval_dataset(effective_spec)
        source_path = materialize_to_book_file(dataset, _materialize_root())
        references = [p.reference_text for p in dataset.paragraphs]
        document_ids = [p.document_id for p in dataset.paragraphs]

    if source_path is None:
        raise ValueError(
            "either source_path or eval_config.dataset_spec must be provided"
        )

    book_for_id, _ = ingest(source_path)
    book_id = book_for_id.meta.book_id

    # ---- guard against ingest dropping or merging dataset paragraphs ----
    # Strict positional alignment between dataset references and ingested-book
    # paragraphs is a prerequisite for the 3-way path. If they drift (e.g. a
    # rare heading-lookalike sneaks past our sanitizer), degrade to the 2-way
    # path with a loud warning rather than crash deep in `align()`.
    if references is not None:
        n_ingested = sum(
            1 for p in book_for_id.iter_paragraphs() if p.source_text.strip()
        )
        if n_ingested != len(references):
            _log.warning(
                "reference/source length mismatch (refs=%d, ingested=%d); "
                "dropping references and falling back to 2-way eval — file a bug "
                "with the dataset spec so the materializer can be hardened",
                len(references), n_ingested,
            )
            references = None
            document_ids = None

    # ---- auto-translate (only if there's no usable run yet) ----
    if eval_config.dataset_spec and eval_config.auto_translate:
        existing_run = latest_run_for(book_id)
        if existing_run is None:
            from abi.runtime import run_pipeline

            _log.info(
                "auto_translate=true; no prior run for book_id=%s, running translate first",
                book_id,
            )
            await run_pipeline(input_path=source_path, config=config)

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

    judge_model = _resolve_judge_model(config, eval_config)

    events.event(
        "eval.start",
        book_id=book_id,
        abi_run_id=abi_run_dir.name,
        eval_id=eval_id,
        samples=eval_config.samples,
        skip_baseline=eval_config.skip_baseline,
        seed=eval_config.random_seed,
        dataset_spec=eval_config.dataset_spec,
        translate_model=config.llm.model,
        judge_model=judge_model,
        has_reference=references is not None,
    )

    router = build_llm_router(config=config, events=events, metrics=metrics)
    target_language = config.target_language

    # ---- Langfuse experiment ctx (optional, degrades gracefully) ----
    experiment: ExperimentContext = DISABLED
    if eval_config.langfuse_experiment and dataset is not None:
        client = get_langfuse_client(config.langfuse)
        if client is not None:
            paragraph_id_map = {
                p.segment_id: p.paragraph_id for p in dataset.paragraphs
            }
            experiment = start_dataset_run(
                config.langfuse,
                client,
                dataset=dataset,
                run_name=eval_id,
                run_metadata={
                    "abi_run_id": abi_run_dir.name,
                    "book_id": book_id,
                    "translate_model": config.llm.model,
                    "judge_model": judge_model,
                    "samples": eval_config.samples,
                    "seed": eval_config.random_seed,
                    "dataset_spec": eval_config.dataset_spec,
                },
                paragraph_id_map=paragraph_id_map,
            )
            if experiment.enabled:
                events.event(
                    "eval.langfuse.run_started",
                    dataset_name=experiment.dataset_name,
                    run_name=experiment.run_name,
                    items=len(experiment.items_by_paragraph_id),
                    url=experiment.run_url,
                )

    notes: list[str] = []
    try:
        # ---- baseline ----
        baseline_dir = eval_dir / "baseline"
        baseline: BaselineResult | None = None
        if eval_config.skip_baseline:
            baseline = load_baseline(baseline_dir)
            if baseline is None:
                prior = _latest_other_baseline(
                    eval_output_root() / book_id, eval_id
                )
                if prior is not None:
                    baseline = prior
                    notes.append(
                        f"reused baseline from prior eval ({prior.meta.model})"
                    )
            if baseline is None:
                notes.append(
                    "skip_baseline=true but no cached baseline found; generating"
                )
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
            book=book,
            units=units,
            baseline_paragraphs=baseline.paragraphs,
            references=references,
            document_ids=document_ids,
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
            reference_paragraphs=alignment_report.reference_paragraphs,
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
        ref_score = None
        if alignment_report.reference_paragraphs > 0:
            ref_score = compute_mechanical(
                triples=triples,
                glossary=glossary,
                source_language=source_language,
                target_language=target_language,
                system="reference",
            )
        mech_report = MechanicalReport(
            abi=abi_score, baseline=base_score, reference=ref_score
        )
        (eval_dir / "mechanical.json").write_text(
            mech_report.model_dump_json(indent=2), encoding="utf-8"
        )

        # ---- sampling ----
        samples = stratified_sample(
            triples,
            n_samples=eval_config.samples,
            seed=eval_config.random_seed,
        )
        events.event(
            "eval.sampled", n=len(samples), requested=eval_config.samples
        )

        # ---- judge ----
        judge_ctx = JudgeContext(
            source_language=source_language,
            target_language=target_language,
            register=register,
            judge_model_name=judge_model,
        )
        model_override = judge_model if judge_model != config.llm.model else None
        rng = random.Random(eval_config.random_seed)

        # Same-document context: when references are document-keyed, pick
        # prev/next from the same doc only. Otherwise, fall back to flat-book
        # ordering (original 2-way behaviour).
        idx_by_pid = {t.paragraph_id: i for i, t in enumerate(triples)}
        doc_to_indices: dict[str, list[int]] = {}
        for i, t in enumerate(triples):
            doc_to_indices.setdefault(t.document_id, []).append(i)

        def _context_for(triple: AlignedTriple) -> tuple[str, str]:
            if triple.document_id and triple.document_id in doc_to_indices:
                doc_idxs = doc_to_indices[triple.document_id]
                pos = doc_idxs.index(idx_by_pid[triple.paragraph_id])
                prev_idx = doc_idxs[pos - 1] if pos > 0 else None
                next_idx = doc_idxs[pos + 1] if pos + 1 < len(doc_idxs) else None
                prev_s = triples[prev_idx].source_text if prev_idx is not None else ""
                next_s = triples[next_idx].source_text if next_idx is not None else ""
                return prev_s, next_s
            i = idx_by_pid[triple.paragraph_id]
            prev_s = triples[i - 1].source_text if i > 0 else ""
            next_s = triples[i + 1].source_text if i + 1 < len(triples) else ""
            return prev_s, next_s

        async def _one(triple: AlignedTriple) -> JudgeSampleResult | None:
            prev_s, next_s = _context_for(triple)
            trace_id = fresh_trace_id() if experiment.enabled else ""
            result = await judge_sample(
                triple=triple,
                prev_source=prev_s,
                next_source=next_s,
                router=router,
                events=events,
                ctx=judge_ctx,
                rng=rng,
                model_override=model_override,
            )
            if result is None:
                return None
            if experiment.enabled and trace_id:
                # The actual LLM-call traces are produced by the LangChain
                # CallbackHandler under their own ids; we maintain a parallel
                # "sample trace" id so per-sample scores roll up cleanly.
                link_sample(experiment, paragraph_id=triple.paragraph_id, trace_id=trace_id)
                attach_scores(
                    experiment,
                    trace_id=trace_id,
                    scores=_per_sample_scores(result),
                )
                # Stash the trace id on the result for downstream debugging.
                result = result.model_copy(update={"langfuse_trace_id": trace_id})
            return result

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
                    "likert_reference": (
                        r.likert_reference.model_dump() if r.likert_reference else None
                    ),
                    "label_mapping": r.label_mapping,
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
                    "abi_vs_ref": r.pairwise_abi_vs_ref,
                    "baseline_vs_ref": r.pairwise_baseline_vs_ref,
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
            reference_paragraphs=alignment_report.reference_paragraphs,
            alignment=alignment_report,
            baseline=baseline.meta,
            mechanical=mech_report,
            judge=judge_agg,
            judge_model=judge_model,
            translate_model=config.llm.model,
            dataset_spec=eval_config.dataset_spec,
            notes=notes,
            langfuse_dataset_name=experiment.dataset_name if experiment.enabled else None,
            langfuse_dataset_run_id=experiment.run_name if experiment.enabled else None,
            langfuse_dataset_run_url=experiment.run_url if experiment.enabled else None,
        )
        (eval_dir / "report.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        (eval_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
        events.event(
            "eval.end",
            samples=judge_agg.samples,
            abi_winrate=judge_agg.pairwise_abi_winrate,
            abi_vs_ref_winrate=judge_agg.pairwise_abi_vs_ref_winrate,
            baseline_vs_ref_winrate=judge_agg.pairwise_baseline_vs_ref_winrate,
            likert_delta_mean=judge_agg.likert_delta.get("mean", 0.0),
            cost_usd=round(router.total_cost, 6),
        )

        if experiment.enabled:
            finalize_run(
                experiment,
                aggregate_scores=_aggregate_scores(judge_agg, mech_report),
            )
            events.event(
                "eval.langfuse.run_finalized",
                dataset_name=experiment.dataset_name,
                run_name=experiment.run_name,
            )
    except BudgetExceeded as exc:
        events.event("eval.aborted", reason="budget", detail=str(exc))
        _log.error("budget exceeded: %s", exc)
        raise
    finally:
        metrics.flush()
        router.flush()
        if experiment.enabled:
            try:
                experiment.client.flush()
            except Exception as exc:  # pragma: no cover
                _log.debug("langfuse client flush ignored: %s", exc)

    return EvalArtifacts(
        eval_dir=eval_dir,
        report=report,
        triples=triples,
        samples=samples,
        judge_results=judge_results,
        baseline=baseline,
    )


def _per_sample_scores(r: JudgeSampleResult) -> list[dict[str, Any]]:
    """Map one :class:`JudgeSampleResult` to a list of Langfuse scores."""
    scores: list[dict[str, Any]] = [
        {"name": "likert.abi.mean", "value": r.likert_abi.mean()},
        {"name": "likert.baseline.mean", "value": r.likert_baseline.mean()},
        {"name": "likert.abi.adequacy", "value": float(r.likert_abi.adequacy)},
        {"name": "likert.abi.fluency", "value": float(r.likert_abi.fluency)},
        {"name": "likert.abi.coherence", "value": float(r.likert_abi.coherence)},
        {"name": "likert.abi.style", "value": float(r.likert_abi.style)},
        {
            "name": "pairwise.abi_vs_baseline",
            "value": _verdict_value(r.pairwise_verdict, abi_side="A"),
        },
    ]
    if r.likert_reference is not None:
        scores.append(
            {"name": "likert.reference.mean", "value": r.likert_reference.mean()}
        )
    if r.pairwise_abi_vs_ref is not None:
        scores.append(
            {
                "name": "pairwise.abi_vs_reference",
                "value": _verdict_value(r.pairwise_abi_vs_ref, abi_side="A"),
            }
        )
    if r.pairwise_baseline_vs_ref is not None:
        scores.append(
            {
                "name": "pairwise.baseline_vs_reference",
                "value": _verdict_value(r.pairwise_baseline_vs_ref, abi_side="A"),
            }
        )
    return scores


def _aggregate_scores(judge_agg: Any, mech_report: Any) -> list[dict[str, Any]]:
    """Run-level aggregate scores for the dataset run."""
    out: list[dict[str, Any]] = [
        {"name": "abi.winrate_vs_baseline", "value": judge_agg.pairwise_abi_winrate},
        {"name": "abi.likert_mean", "value": judge_agg.likert_abi.get("mean", 0.0)},
        {"name": "baseline.likert_mean", "value": judge_agg.likert_baseline.get("mean", 0.0)},
        {"name": "abi.mechanical.completeness", "value": mech_report.abi.completeness},
        {"name": "abi.mechanical.glossary", "value": mech_report.abi.glossary_compliance},
    ]
    if mech_report.reference is not None:
        out.append(
            {"name": "reference.likert_mean", "value": judge_agg.likert_reference.get("mean", 0.0)}
        )
        out.append(
            {"name": "abi.winrate_vs_reference", "value": judge_agg.pairwise_abi_vs_ref_winrate}
        )
        out.append(
            {
                "name": "baseline.winrate_vs_reference",
                "value": judge_agg.pairwise_baseline_vs_ref_winrate,
            }
        )
    return out


def _verdict_value(verdict: str, *, abi_side: str) -> float:
    """Map a pairwise verdict to a winrate-style score in [0, 1]."""
    if verdict == abi_side:
        return 1.0
    if verdict == "tie":
        return 0.5
    return 0.0


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
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
