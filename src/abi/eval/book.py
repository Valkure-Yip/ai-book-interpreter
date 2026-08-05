"""Whole-book three-plane evaluation (eval-standard.md §2–§5).

Combines, for one finished/partial book project:

- **L1** process conformance + crosscutting metrics (delegates to :mod:`trace`),
- **L2** per-chapter deterministic translation quality (align source<->translated
  paragraphs, then :func:`score_paragraph`),
- **L3** final-product compliance (EPUB lint / asset manifest / epubcheck) and the
  random-spotcheck excellence line, replayed from on-disk artifacts.

Everything here is deterministic — no LLM calls. The optional L3 LLM-as-judge
(ABI vs baseline) lives in :mod:`judge` and is driven separately by the CLI.
"""

from __future__ import annotations

import csv
import json
from io import StringIO
from pathlib import Path
from typing import Any

from abi.eval.align import align_paragraphs, is_low_info, split_paragraphs
from abi.eval.mechanical import score_paragraph
from abi.eval.report import aggregate_mechanical
from abi.eval.run_facts import load_eval_run_facts
from abi.eval.trace import TraceReport, trace_project
from abi.eval.types import MechanicalScores
from abi.project.layout import BookProject
from abi.types._base import FrozenModel

_GLOSSARY_ENFORCED_STATUSES = {"locked", "preferred"}


# --------------------------------------------------------------------------- L2
class ChapterL2(FrozenModel):
    slug: str
    granularity: str  # "paragraph" (aligned 1:1) | "chapter" (merged/split -> whole-chapter)
    n_paragraphs: int
    score_avg: float
    score_p10: float
    score_min: float
    completeness: float
    length_ratio_avg: float
    align_failed: bool
    translated_missing: bool
    flag_counts: dict[str, int]


class BookL2Report(FrozenModel):
    source_lang: str
    target_lang: str
    glossary_terms: int
    n_chapters: int
    n_chapters_translated: int
    n_paragraphs: int
    score_avg: float
    score_p10: float
    score_min: float
    completeness: float
    length_ratio_avg: float
    flag_counts: dict[str, int]
    align_failed_chapters: list[str]
    chapters: list[ChapterL2]
    verdict: str  # PASS | WARN | FAIL


# --------------------------------------------------------------------------- L3
class EpubComplianceL3(FrozenModel):
    epub_built: bool
    publication_lint_ok: bool
    asset_manifest_ok: bool
    epubcheck_present: bool
    epubcheck_fatal: int
    epubcheck_errors: int
    epubcheck_warnings: int


class SpotcheckL3(FrozenModel):
    ran: bool
    rounds: int
    status: str | None
    release_confidence: float | None
    avg_score: float | None


class BookL3Report(FrozenModel):
    epub: EpubComplianceL3
    spotcheck: SpotcheckL3
    verdict: str  # PASS | WARN | FAIL


# ----------------------------------------------------------------------- rollup
class BookEvalReport(FrozenModel):
    book: str
    status: str
    l1: TraceReport
    l2: BookL2Report
    l3: BookL3Report
    verdict: str  # PASS | WARN | FAIL


def _load_glossary(project: BookProject) -> dict[str, str]:
    """Parse ``glossary/terms.csv`` -> ``{source_term: required_target}`` for
    enforced (locked/preferred) rows. Returns ``{}`` when absent/empty."""
    path = project.terms_csv
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    reader = csv.DictReader(StringIO(text))
    for row in reader:
        if not row:
            continue
        term = (row.get("term") or "").strip()
        target = (row.get("target") or "").strip()
        status = (row.get("status") or "").strip().lower()
        if term and target and status in _GLOSSARY_ENFORCED_STATUSES:
            out[term] = target
    return out


def _l2_verdict(report_kwargs: dict[str, object]) -> str:
    completeness = float(report_kwargs["completeness"])  # type: ignore[arg-type]
    score_avg = float(report_kwargs["score_avg"])  # type: ignore[arg-type]
    flags = report_kwargs["flag_counts"]
    assert isinstance(flags, dict)
    hard = int(flags.get("refusal_detected", 0)) + int(flags.get("untranslated_residue", 0))
    if completeness < 0.99 or hard > 0 or score_avg < 0.70:
        return "FAIL"
    if score_avg < 0.85:
        return "WARN"
    return "PASS"


def score_book_l2(
    project: BookProject,
    *,
    source_lang: str,
    target_lang: str,
) -> BookL2Report:
    """Align + mechanically score every source<->translated chapter pair."""
    glossary = _load_glossary(project)
    glossary_arg = glossary or None

    chapters: list[ChapterL2] = []
    all_scores: list[MechanicalScores] = []
    align_failed_chapters: list[str] = []
    n_translated = 0

    for slug in project.chapter_slugs():
        src_path = project.chapters_src / f"{slug}.md"
        tgt_path = project.chapters_translated / f"{slug}.md"
        source_md = src_path.read_text(encoding="utf-8") if src_path.exists() else ""
        translated_missing = not tgt_path.exists()
        target_md = "" if translated_missing else tgt_path.read_text(encoding="utf-8")
        if not translated_missing:
            n_translated += 1

        alignment = align_paragraphs(source_md, target_md)
        chapter_scores: list[MechanicalScores] = []
        granularity = "paragraph"

        if translated_missing:
            # No translation at all -> one whole-chapter completeness failure.
            granularity = "chapter"
            chapter_scores.append(
                score_paragraph(
                    source_md,
                    None,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    glossary=glossary_arg,
                )
            )
        elif alignment.chapter_align_failed:
            # Paragraph counts diverged (merging/splitting) -> robust chapter-level
            # scoring on the joined body text instead of penalising every paragraph.
            granularity = "chapter"
            src_body = "\n\n".join(split_paragraphs(source_md))
            tgt_body = "\n\n".join(split_paragraphs(target_md))
            chapter_scores.append(
                score_paragraph(
                    src_body,
                    tgt_body,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    glossary=glossary_arg,
                )
            )
        else:
            for pair in alignment.pairs:
                if is_low_info(pair.source):
                    continue
                chapter_scores.append(
                    score_paragraph(
                        pair.source,
                        pair.target,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        glossary=glossary_arg,
                    )
                )

        all_scores.extend(chapter_scores)
        agg = aggregate_mechanical(chapter_scores)
        if alignment.chapter_align_failed and not translated_missing:
            align_failed_chapters.append(slug)
        chapters.append(
            ChapterL2(
                slug=slug,
                granularity=granularity,
                n_paragraphs=agg.get("n", 0),
                score_avg=agg.get("score_avg", 0.0),
                score_p10=agg.get("score_p10", 0.0),
                score_min=agg.get("score_min", 0.0),
                completeness=agg.get("completeness", 0.0),
                length_ratio_avg=agg.get("length_ratio_avg", 0.0),
                align_failed=alignment.chapter_align_failed,
                translated_missing=translated_missing,
                flag_counts=agg.get("flag_counts", {}),
            )
        )

    overall = aggregate_mechanical(all_scores)
    kwargs: dict[str, object] = {
        "source_lang": source_lang,
        "target_lang": target_lang,
        "glossary_terms": len(glossary),
        "n_chapters": len(chapters),
        "n_chapters_translated": n_translated,
        "n_paragraphs": overall.get("n", 0),
        "score_avg": overall.get("score_avg", 0.0),
        "score_p10": overall.get("score_p10", 0.0),
        "score_min": overall.get("score_min", 0.0),
        "completeness": overall.get("completeness", 0.0),
        "length_ratio_avg": overall.get("length_ratio_avg", 0.0),
        "flag_counts": overall.get("flag_counts", {}),
        "align_failed_chapters": align_failed_chapters,
        "chapters": chapters,
    }
    kwargs["verdict"] = _l2_verdict(kwargs)
    return BookL2Report.model_validate(kwargs)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _report_clean(data: dict[str, Any] | None) -> bool:
    if not data:
        return False
    return bool(data.get("ok")) and int(data.get("hard_errors", data.get("errors", 0)) or 0) == 0


def check_book_l3(project: BookProject) -> BookL3Report:
    """Replay EPUB compliance + spotcheck excellence from on-disk artifacts."""
    epub_built = project.book_epub.exists()
    lint = _read_json(project.publication_lint_report)
    asset = _read_json(project.asset_manifest_report)

    epubcheck_summary = _read_json(project.root / "output/epubcheck_summary.json")
    details = (epubcheck_summary or {}).get("details", {}) if epubcheck_summary else {}
    epub = EpubComplianceL3(
        epub_built=epub_built,
        publication_lint_ok=_report_clean(lint),
        asset_manifest_ok=_report_clean(asset),
        epubcheck_present=epubcheck_summary is not None,
        epubcheck_fatal=int(details.get("fatal", 0) or 0),
        epubcheck_errors=int(details.get("errors", 0) or 0),
        epubcheck_warnings=int(details.get("warnings", 0) or 0),
    )

    rounds = (
        sorted(project.random_spotcheck_dir.glob("round_*"))
        if project.random_spotcheck_dir.exists()
        else []
    )
    sc_status: str | None = None
    sc_conf: float | None = None
    sc_avg: float | None = None
    if rounds:
        data = _read_json(rounds[-1] / "validation_report.json")
        if data:
            sc_status = str(data.get("status")) if data.get("status") is not None else None
            rc = data.get("release_confidence")
            sc_conf = float(rc) if rc is not None else None
            avg = data.get("avg_score", data.get("average_score"))
            sc_avg = float(avg) if avg is not None else None
    spotcheck = SpotcheckL3(
        ran=bool(rounds),
        rounds=len(rounds),
        status=sc_status,
        release_confidence=sc_conf,
        avg_score=sc_avg,
    )

    # EPUB compliance verdict.
    hard_fail = (
        not epub_built
        or epub.epubcheck_fatal > 0
        or epub.epubcheck_errors > 0
        or not epub.publication_lint_ok
        or not epub.asset_manifest_ok
    )
    if hard_fail:
        verdict = "FAIL"
    elif not epub.epubcheck_present or epub.epubcheck_warnings > 0 or not spotcheck.ran:
        verdict = "WARN"
    else:
        verdict = "PASS"

    return BookL3Report(epub=epub, spotcheck=spotcheck, verdict=verdict)


_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}


def _worst(*verdicts: str) -> str:
    return max(verdicts, key=lambda v: _RANK.get(v, 1))


def eval_book(
    project: BookProject,
    *,
    source_lang: str | None = None,
    target_lang: str | None = None,
) -> BookEvalReport:
    """Run all three planes over ``project`` and roll up a combined verdict."""
    facts = load_eval_run_facts(project)
    sl = source_lang or facts.run.source_lang
    tl = target_lang or facts.run.target_lang

    l1 = trace_project(project, facts=facts)
    l2 = score_book_l2(project, source_lang=sl, target_lang=tl)
    l3 = check_book_l3(project)

    return BookEvalReport(
        book=project.root.name,
        status=facts.run.status.value,
        l1=l1,
        l2=l2,
        l3=l3,
        verdict=_worst(l1.verdict, l2.verdict, l3.verdict),
    )


def render_book_md(report: BookEvalReport) -> str:
    """Human-readable Markdown summary of a three-plane book eval."""
    l1, l2, l3 = report.l1, report.l2, report.l3
    lines = [
        f"# Three-plane eval — {report.book}",
        "",
        f"- **总判定: {report.verdict}**  (status={report.status})",
        f"- L1 流程可信度: **{l1.verdict}**  ·  L2 译文质量: **{l2.verdict}**  ·  "
        f"L3 最终产物: **{l3.verdict}**",
        "",
        "## L1 — 流程可信度 (process conformance)",
        "",
        f"- gate_integrity: {'OK' if l1.gate_integrity_ok else 'FAIL'}  ·  "
        f"reached_states: {'OK' if l1.reached_states_ok else 'FAIL'}  ·  "
        f"path_conformance: {'OK' if l1.path_conformance_ok else 'FAIL'}",
        f"- cost=${l1.cost_usd}  tokens(in/out)={l1.tokens_in}/{l1.tokens_out}  "
        f"llm_calls={l1.llm_calls}  duration_s={l1.duration_s}",
        f"- first_pass_rate={l1.first_pass_rate}  recursion_caps={l1.recursion_caps}  "
        f"budget_stops={l1.budget_stops}",
        "",
        "| gate | produces | recorded | replay_ok | consistent |",
        "| --- | --- | --- | --- | --- |",
    ]
    for g in l1.gate_integrity:
        mark = "n/a" if not g.verifiable else ("✓" if g.consistent else "✗")
        lines.append(f"| {g.gate} | {g.produces} | {g.recorded} | {g.replay_ok} | {mark} |")

    lines += [
        "",
        "## L2 — 逐章译文质量 (deterministic)",
        "",
        f"- {l2.source_lang} → {l2.target_lang}  ·  glossary_terms={l2.glossary_terms}",
        f"- chapters={l2.n_chapters} (translated={l2.n_chapters_translated})  "
        f"paragraphs={l2.n_paragraphs}",
        f"- para_score: avg={l2.score_avg} p10={l2.score_p10} min={l2.score_min}  "
        f"completeness={l2.completeness}  length_ratio_avg={l2.length_ratio_avg}",
        f"- flags: {l2.flag_counts or '{}'}",
    ]
    if l2.align_failed_chapters:
        lines.append(f"- ⚠ align_failed_chapters: {', '.join(l2.align_failed_chapters)}")
    lines += [
        "",
        "| chapter | gran | units | avg | p10 | min | len_ratio | flags |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in l2.chapters:
        note = " ⚠missing" if c.translated_missing else ""
        lines.append(
            f"| {c.slug}{note} | {c.granularity} | {c.n_paragraphs} | {c.score_avg} | "
            f"{c.score_p10} | {c.score_min} | {c.length_ratio_avg} | {c.flag_counts or ''} |"
        )

    e, sc = l3.epub, l3.spotcheck
    lines += [
        "",
        "## L3 — 最终产物 (final product)",
        "",
        f"- EPUB compliance: **{l3.verdict}**",
        f"  - epub_built={e.epub_built}  publication_lint_ok={e.publication_lint_ok}  "
        f"asset_manifest_ok={e.asset_manifest_ok}",
        f"  - epubcheck: present={e.epubcheck_present} fatal={e.epubcheck_fatal} "
        f"errors={e.epubcheck_errors} warnings={e.epubcheck_warnings}",
        f"- random spotcheck (卓越线): ran={sc.ran} rounds={sc.rounds} "
        f"status={sc.status} release_confidence={sc.release_confidence} avg_score={sc.avg_score}",
    ]
    return "\n".join(lines) + "\n"
