"""``abi`` CLI for the agentic EPUB pipeline."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from abi.config import build_run_config
from abi.config.loader import (
    default_project_config_path,
    default_user_config_path,
    load_dotenv,
)
from abi.orchestrator import make_book, resume
from abi.project.layout import BookProject
from abi.project.state import HAPPY_PATH, Status, happy_index

app = typer.Typer(
    add_completion=False,
    help="ABI — an autonomous agent that turns a public-domain book into a "
    "quality-gated, versioned EPUB.",
    no_args_is_help=True,
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _overrides(
    *, base_url: str | None, model: str | None, max_cost_usd: float | None
) -> dict[str, Any]:
    o: dict[str, Any] = {}
    if base_url:
        o.setdefault("llm", {})["base_url"] = base_url
    if model:
        o.setdefault("llm", {})["model"] = model
    if max_cost_usd is not None:
        o["cost"] = {"hard_cap_usd": max_cost_usd}
    return o


def _config(config_file: Path | None, **ov: Any):
    load_dotenv()
    return build_run_config(
        user_config_path=default_user_config_path(),
        project_config_path=config_file or default_project_config_path(),
        cli_overrides=_overrides(**ov),
    )


def _resolve_until(until: str | None) -> Status | None:
    if not until:
        return None
    try:
        return Status(until)
    except ValueError as exc:
        valid = ", ".join(s.value for s in HAPPY_PATH)
        raise typer.BadParameter(f"invalid --until {until!r}. Valid: {valid}") from exc


@app.command("make-book")
def make_book_cmd(
    source: str = typer.Argument(..., help="Source file path or URL (.txt or .epub)."),
    source_target: str = typer.Option(
        "en-zh-Hans", "--source-target", "-st",
        help="Language-pair template, e.g. 'en-zh-Hans', 'ja-es', 'fr-en'.",
    ),
    title: str | None = typer.Option(None, "--title", help="Book slug / title."),
    books_root: Path = typer.Option(
        Path("books"), "--books-root", help="Root for book projects: {root}/{target}/..."
    ),
    mode: str = typer.Option(
        "public_domain", "--mode",
        help="public_domain | licensed | private_use.",
    ),
    profile: str | None = typer.Option(None, "--profile", help="Optional book-type profile."),
    until: str | None = typer.Option(
        None, "--until", help="Stop after reaching this Status (e.g. TRANSLATED)."
    ),
    base_url: str | None = typer.Option(None, "--base-url"),
    model: str | None = typer.Option(None, "--model"),
    max_cost_usd: float | None = typer.Option(None, "--max-cost-usd"),
    config_file: Path | None = typer.Option(None, "--config"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Scaffold a new book project and run the agent to DONE (or --until)."""
    _setup_logging(verbose)
    config = _config(config_file, base_url=base_url, model=model, max_cost_usd=max_cost_usd)
    until_status = _resolve_until(until)
    if mode == "private_use":
        books_root = books_root / "private"

    console.print(f"[bold]make-book[/] [cyan]{source}[/] -> [green]{source_target}[/] ({mode})")
    console.print(f"  endpoint: {config.llm.base_url}  model: {config.llm.model}")

    project, result = asyncio.run(
        make_book(
            source=source,
            source_target=source_target,
            config=config,
            books_root=books_root,
            book_slug=title,
            publication_mode=mode,
            profile=profile,
            until=until_status,
            max_stage_attempts=config.max_stage_attempts,
        )
    )
    _print_result(project, result)


@app.command("resume")
def resume_cmd(
    project_root: Path = typer.Argument(..., help="Existing book-project directory."),
    until: str | None = typer.Option(None, "--until"),
    base_url: str | None = typer.Option(None, "--base-url"),
    model: str | None = typer.Option(None, "--model"),
    max_cost_usd: float | None = typer.Option(None, "--max-cost-usd"),
    config_file: Path | None = typer.Option(None, "--config"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Resume an existing book project from its persisted pipeline state."""
    _setup_logging(verbose)
    config = _config(config_file, base_url=base_url, model=model, max_cost_usd=max_cost_usd)
    project, result = asyncio.run(
        resume(
            project_root=project_root,
            config=config,
            until=_resolve_until(until),
            max_stage_attempts=config.max_stage_attempts,
        )
    )
    _print_result(project, result)


@app.command("state")
def state_cmd(
    project_root: Path = typer.Argument(..., help="Book-project directory."),
) -> None:
    """Show the pipeline state machine + gate status for a project."""
    project = BookProject(Path(project_root).expanduser().resolve())
    if not project.exists():
        console.print(f"[red]no project state at[/] {project.state_path}")
        raise typer.Exit(code=1)
    st = project.load_state()
    console.print(f"[bold]{project.root.name}[/]  ({st.source_target}, {st.publication_mode})")
    console.print(f"  status: [green]{st.status.value}[/]  step: {st.current_step}")
    if st.last_error:
        console.print(f"  last_error: [red]{st.last_error}[/]")

    table = Table(title="Happy path")
    table.add_column("")
    table.add_column("Status")
    cur = happy_index(st.status)
    for i, s in enumerate(HAPPY_PATH):
        mark = "[green]✓[/]" if i < cur else ("[yellow]>[/]" if i == cur else " ")
        table.add_row(mark, s.value)
    console.print(table)
    if st.gates:
        console.print("Gates: " + ", ".join(f"{k}={v}" for k, v in st.gates.items()))


eval_app = typer.Typer(
    add_completion=False,
    help="Evaluation: calibrate length-ratio bands + replay process conformance "
    "(see docs/design-docs/eval-standard.md).",
    no_args_is_help=True,
)
app.add_typer(eval_app, name="eval")


@eval_app.command("calibrate")
def eval_calibrate_cmd(
    dataset: str = typer.Option(
        ..., "--dataset", "-d",
        help="Dataset spec, e.g. 'wmt24pp:en-zh_CN:literary' (add ':stub=true' offline).",
    ),
    out_dir: Path = typer.Option(Path("eval-out"), "--out", "-o", help="Output root."),
    min_samples: int = typer.Option(
        50, "--min-samples", help="Min samples before a pair's band is emitted."
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Derive length-ratio bands from a reference dataset (no LLM calls)."""
    from abi.eval.pipeline import run_calibration

    _setup_logging(verbose)
    results, bands = run_calibration(dataset, out_dir=out_dir, min_samples=min_samples)
    if not results:
        console.print("[yellow]no reference pairs found for this spec[/]")
        raise typer.Exit(code=1)

    table = Table(title=f"Length-ratio calibration — {dataset}")
    for col in ("pair", "n", "p10", "p50", "p90", "suggested", "current"):
        table.add_column(col)
    for r in results:
        table.add_row(
            r.source_target, str(r.n), f"{r.ratio_p10}", f"{r.ratio_p50}",
            f"{r.ratio_p90}", f"[{r.suggested_lo}, {r.suggested_hi}]",
            f"[{r.current_lo}, {r.current_hi}]",
        )
    console.print(table)
    emitted = ", ".join(bands) if bands else "(none — all pairs below --min-samples)"
    console.print(f"[green]bands written[/] (>= {min_samples} samples): {emitted}")


@eval_app.command("trace")
def eval_trace_cmd(
    project_root: Path = typer.Argument(..., help="Book-project directory."),
    out_dir: Path | None = typer.Option(None, "--out", "-o", help="Write reports here."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Replay L1 gate integrity + path conformance + system metrics for a project."""
    from abi.eval.pipeline import run_trace

    _setup_logging(verbose)
    report = run_trace(project_root, out_dir=out_dir)
    color = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}.get(report.verdict, "white")
    console.print(f"[bold]{report.book}[/]  status={report.status}  "
                  f"verdict=[{color}]{report.verdict}[/]")
    console.print(
        f"  gate_integrity={report.gate_integrity_ok}  "
        f"reached_states={report.reached_states_ok}  "
        f"path_conformance={report.path_conformance_ok}"
    )
    for g in report.gate_integrity:
        if not g.consistent:
            console.print(f"  [red]✗ {g.gate}[/]: recorded={g.recorded} "
                          f"replay_ok={g.replay_ok} — {g.replay_reason}")
    if report.skipped_states:
        console.print(f"  [yellow]skipped:[/] {', '.join(report.skipped_states)}")
    console.print(
        f"  cost=${report.cost_usd} tokens(in/out)={report.tokens_in}/{report.tokens_out} "
        f"first_pass_rate={report.first_pass_rate}"
    )
    if report.verdict == "FAIL":
        raise typer.Exit(code=1)


@eval_app.command("book")
def eval_book_cmd(
    project_root: Path = typer.Argument(..., help="Book-project directory."),
    out_dir: Path | None = typer.Option(
        Path("eval-out"), "--out", "-o", help="Write reports here (set '' to skip)."
    ),
    source_lang: str | None = typer.Option(
        None, "--source-lang", help="Override source lang (default from pipeline_state)."
    ),
    target_lang: str | None = typer.Option(
        None, "--target-lang", help="Override target lang (default from pipeline_state)."
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Full three-plane eval: L1 process + L2 per-chapter quality + L3 final product."""
    from abi.eval.pipeline import run_book_eval

    _setup_logging(verbose)
    od = out_dir if out_dir and str(out_dir) else None
    report = run_book_eval(
        project_root, out_dir=od, source_lang=source_lang, target_lang=target_lang
    )
    col = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}

    def _c(v: str) -> str:
        return f"[{col.get(v, 'white')}]{v}[/]"

    console.print(
        f"[bold]{report.book}[/]  status={report.status}  总判定={_c(report.verdict)}"
    )
    console.print(
        f"  L1 流程={_c(report.l1.verdict)}  "
        f"L2 译文={_c(report.l2.verdict)}  "
        f"L3 产物={_c(report.l3.verdict)}"
    )
    l2 = report.l2
    console.print(
        f"  [bold]L2[/] {l2.source_lang}->{l2.target_lang}  "
        f"chapters={l2.n_chapters}(译={l2.n_chapters_translated})  "
        f"paras={l2.n_paragraphs}  para_score avg={l2.score_avg} p10={l2.score_p10} "
        f"min={l2.score_min}  completeness={l2.completeness}  flags={l2.flag_counts or '{}'}"
    )
    e, sc = report.l3.epub, report.l3.spotcheck
    console.print(
        f"  [bold]L3[/] epub_built={e.epub_built} lint_ok={e.publication_lint_ok} "
        f"asset_ok={e.asset_manifest_ok} epubcheck(f/e/w)="
        f"{e.epubcheck_fatal}/{e.epubcheck_errors}/{e.epubcheck_warnings}  "
        f"spotcheck(ran={sc.ran} status={sc.status} conf={sc.release_confidence})"
    )
    console.print(
        f"  [bold]system[/] cost=${report.l1.cost_usd} "
        f"tokens(in/out)={report.l1.tokens_in}/{report.l1.tokens_out} "
        f"first_pass_rate={report.l1.first_pass_rate}"
    )
    if od is not None:
        console.print(f"  [dim]reports -> {od}/{report.book}/[/]")
    if report.verdict == "FAIL":
        raise typer.Exit(code=1)


def _print_result(project: BookProject, result: Any) -> None:
    console.print()
    icon = "[bold green]✓[/]" if result.final_status == Status.DONE else "[bold yellow]…[/]"
    console.print(f"{icon} pipeline stopped at [bold]{result.final_status.value}[/]")
    console.print(f"  project: {project.root}")
    console.print(f"  cost:    ${result.cost_usd:.4f}")
    for o in result.stages_run:
        status = "[green]ok[/]" if o.ok else f"[red]FAIL: {o.reason}[/]"
        console.print(f"   - {o.stage_id}: {status} ({o.attempts} attempt(s))")
    if result.blocked_reason:
        console.print(f"  [red]blocked:[/] {result.blocked_reason}")
        console.print(f"  resume with: [dim]abi resume {project.root}[/]")


if __name__ == "__main__":  # pragma: no cover
    app()
