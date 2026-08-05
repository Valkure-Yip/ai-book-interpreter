"""Eval orchestration entry points used by the CLI.

- :func:`run_trace` — L1 + crosscutting, pure (no LLM, no network).
- :func:`run_calibration` — length-ratio bands from a dataset, pure (network only
  for non-stub dataset loads).
- :func:`score_triples_mechanical` — L2 deterministic scoring of a system's
  translations against the source.
- :func:`run_judge` — L3 LLM-as-judge over triples carrying ABI + baseline.
"""

from __future__ import annotations

import random
import uuid
from pathlib import Path
from typing import Any

from abi.eval import FORMULA_VERSION
from abi.eval.book import BookEvalReport, eval_book, render_book_md
from abi.eval.calibration import bands_from_calibration, calibrate
from abi.eval.datasets import load_triples, parse_dataset_spec
from abi.eval.judge import JudgeResult, judge_triple
from abi.eval.mechanical import score_paragraph
from abi.eval.report import (
    render_calibration_md,
    render_trace_md,
    write_json,
)
from abi.eval.trace import TraceReport, trace_project
from abi.eval.types import CalibrationResult, EvalTriple, LengthBand, MechanicalScores
from abi.project.layout import BookProject


def _eval_id() -> str:
    return uuid.uuid4().hex[:12]


def run_trace(project_root: Path, *, out_dir: Path | None = None) -> TraceReport:
    """Replay L1 gate integrity + read system metrics for a book project."""
    project = BookProject(Path(project_root).expanduser().resolve())
    if not project.exists():
        raise FileNotFoundError(
            f"no durable run ledger at {project.run_db}. Run `abi make-book` first."
        )
    report = trace_project(project)
    if out_dir is not None:
        d = out_dir / project.root.name / _eval_id()
        write_json(d / "trace_report.json", report.model_dump())
        (d / "trace_report.md").write_text(render_trace_md(report), encoding="utf-8")
    return report


def run_book_eval(
    project_root: Path,
    *,
    out_dir: Path | None = None,
    source_lang: str | None = None,
    target_lang: str | None = None,
) -> BookEvalReport:
    """Run the full three-plane (L1+L2+L3) deterministic eval for a book project."""
    project = BookProject(Path(project_root).expanduser().resolve())
    if not project.exists():
        raise FileNotFoundError(
            f"no durable run ledger at {project.run_db}. Run `abi make-book` first."
        )
    report = eval_book(project, source_lang=source_lang, target_lang=target_lang)
    if out_dir is not None:
        d = out_dir / project.root.name / _eval_id()
        write_json(d / "book_eval.json", report.model_dump())
        (d / "book_eval.md").write_text(render_book_md(report), encoding="utf-8")
    return report


def run_calibration(
    spec_str: str, *, out_dir: Path, min_samples: int = 50
) -> tuple[list[CalibrationResult], dict[str, LengthBand]]:
    """Load a dataset spec, derive length-ratio bands, and write a calibration report."""
    spec = parse_dataset_spec(spec_str)
    triples = load_triples(spec)
    results = calibrate(triples)
    bands = bands_from_calibration(results, min_samples=min_samples)

    eid = _eval_id()
    d = out_dir / "calibration" / eid
    write_json(
        d / "manifest.json",
        {
            "eval_id": eid,
            "formula_version": FORMULA_VERSION,
            "dataset": spec_str,
            "config": spec.config,
            "domain": spec.domain,
            "stub": spec.stub,
            "n_triples": len(triples),
            "min_samples": min_samples,
        },
    )
    write_json(d / "calibration.json", [r.model_dump() for r in results])
    write_json(
        d / "bands.json",
        {k: v.model_dump() for k, v in bands.items()},
    )
    (d / "calibration.md").write_text(render_calibration_md(results), encoding="utf-8")
    return results, bands


def score_triples_mechanical(
    triples: list[EvalTriple],
    *,
    system: str = "reference",
    bands: dict[str, LengthBand] | None = None,
    glossary: dict[str, str] | None = None,
) -> list[MechanicalScores]:
    """Score one system's translation per triple. ``system`` selects the field
    (``reference`` | ``abi`` | ``baseline``)."""
    out: list[MechanicalScores] = []
    for t in triples:
        target = getattr(t, system)
        out.append(
            score_paragraph(
                t.source,
                target,
                source_lang=t.source_lang,
                target_lang=t.target_lang,
                bands=bands,
                glossary=glossary,
            )
        )
    return out


async def run_judge(
    router: Any,
    triples: list[EvalTriple],
    *,
    seed: int = 0,
    judge_model: str | None = None,
) -> list[JudgeResult]:
    """L3 judge over triples that carry both ABI and baseline translations."""
    rng = random.Random(seed)
    results: list[JudgeResult] = []
    for t in triples:
        res = await judge_triple(router, t, rng=rng, judge_model=judge_model)
        if res is not None:
            results.append(res)
    return results
