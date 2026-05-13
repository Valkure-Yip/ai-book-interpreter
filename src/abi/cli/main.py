"""``abi`` CLI."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from abi.config import build_run_config
from abi.config.loader import default_project_config_path, default_user_config_path
from abi.runtime import run_pipeline
from abi.runtime.manifest import runs_root
from abi.runtime.selection import parse_chapter_selection
from abi.types.run import OutputMode

app = typer.Typer(
    add_completion=False,
    help="AI Book Interpreter — translate academic books with sliding-window context.",
    no_args_is_help=True,
)
console = Console()


def _build_overrides(
    *,
    target: str | None,
    modes: list[OutputMode] | None,
    base_url: str | None,
    model: str | None,
    max_cost_usd: float | None,
    concurrency: int | None,
    dry_run: bool,
    force_rerun: bool,
    refine_toc: bool | None = None,
) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if target:
        overrides["target_language"] = target
    if modes:
        overrides["modes"] = list(modes)
    if base_url:
        overrides.setdefault("llm", {})["base_url"] = base_url
    if model:
        overrides.setdefault("llm", {})["model"] = model
    if concurrency is not None:
        overrides["concurrency"] = concurrency
        overrides.setdefault("llm", {})["max_concurrency"] = concurrency
    if max_cost_usd is not None:
        overrides["cost"] = {"hard_cap_usd": max_cost_usd}
    if dry_run:
        overrides["dry_run"] = True
    if force_rerun:
        overrides["force_rerun"] = True
    if refine_toc is not None:
        overrides["refine_toc"] = refine_toc
    return overrides


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@app.command()
def translate(
    input_path: Path = typer.Argument(..., exists=True, help="Input file (.txt or .epub)"),
    output: Path | None = typer.Option(None, "-o", "--output", help="Output directory."),
    target: str = typer.Option("zh", "--target", help="Target language code."),
    mode: list[str] = typer.Option(
        ["translated"],
        "--mode",
        help="Output mode(s): translated, bilingual, annotated. Repeat for multiple.",
    ),
    base_url: str | None = typer.Option(None, "--base-url", help="OpenAI-compatible base URL."),
    model: str | None = typer.Option(None, "--model", help="Model name."),
    max_cost_usd: float | None = typer.Option(None, "--max-cost-usd", help="Hard cap."),
    concurrency: int | None = typer.Option(None, "--concurrency"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run only survey pass."),
    force_rerun: bool = typer.Option(False, "--force-rerun", help="Ignore checkpoints."),
    no_refine_toc: bool = typer.Option(
        False,
        "--no-refine-toc",
        help="Skip the LLM-based TOC refiner before survey. By default the "
        "refiner runs once per book and rewrites chapter boundaries.",
    ),
    title: str | None = typer.Option(None, "--title", help="Override detected title."),
    chapters: str | None = typer.Option(
        None,
        "--chapters",
        help='Only translate selected top-level chapters, e.g. "1,3-5". 1-indexed.',
    ),
    resume: str | None = typer.Option(
        None,
        "--resume",
        help='Resume from a previous run. Pass a run_id or "latest". '
        "Survey artifacts and per-paragraph translations are reused if found.",
    ),
    config_file: Path | None = typer.Option(
        None, "--config", help="Path to project config (default ./abi.yaml)."
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Translate a book end-to-end (Pass 0/1/2/3)."""
    _setup_logging(verbose)
    try:
        chapter_selection = parse_chapter_selection(chapters or "")
    except ValueError as exc:
        console.print(f"[bold red]invalid --chapters value:[/] {exc}")
        raise typer.Exit(code=2) from exc
    # Accept both repeated --mode flags and comma-separated values in one flag.
    expanded_modes: list[str] = []
    for m in mode:
        for piece in m.split(","):
            piece = piece.strip()
            if piece:
                expanded_modes.append(piece)
    overrides = _build_overrides(
        target=target,
        modes=expanded_modes,  # type: ignore[arg-type]
        base_url=base_url,
        model=model,
        max_cost_usd=max_cost_usd,
        concurrency=concurrency,
        dry_run=dry_run,
        force_rerun=force_rerun,
        refine_toc=False if no_refine_toc else None,
    )
    config = build_run_config(
        user_config_path=default_user_config_path(),
        project_config_path=config_file or default_project_config_path(),
        cli_overrides=overrides,
    )
    console.print(f"[bold]Translating[/] [cyan]{input_path}[/] → [green]{target}[/]")
    console.print(f"  endpoint: {config.llm.base_url}")
    console.print(f"  model:    {config.llm.model}")
    console.print(f"  modes:    {', '.join(config.modes)}")
    if config.langfuse.enabled:
        payload_mode = "full" if config.langfuse.upload_full_payload else "redacted"
        console.print(
            f"  langfuse: [dim]{config.langfuse.host}[/] (payload=[bold]{payload_mode}[/])"
        )
    else:
        console.print("  langfuse: [dim]disabled[/]")

    if chapter_selection:
        console.print(
            f"  chapters: only {sorted(chapter_selection)} of top-level TOC"
        )
    if resume:
        console.print(f"  resume:   [bold]{resume}[/]")

    result = asyncio.run(
        run_pipeline(
            input_path=input_path,
            config=config,
            output_dir=output,
            title=title,
            chapter_selection=chapter_selection or None,
            resume=resume,
        )
    )
    _print_result(result)


@app.command()
def survey(
    input_path: Path = typer.Argument(..., exists=True),
    output: Path | None = typer.Option(None, "-o", "--output"),
    target: str = typer.Option("zh", "--target"),
    base_url: str | None = typer.Option(None, "--base-url"),
    model: str | None = typer.Option(None, "--model"),
    max_cost_usd: float | None = typer.Option(None, "--max-cost-usd"),
    title: str | None = typer.Option(None, "--title"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Run only Pass 0+1: produce overview, glossary, mindmap, style guide."""
    _setup_logging(verbose)
    overrides = _build_overrides(
        target=target,
        modes=["survey-only"],  # type: ignore[arg-type]
        base_url=base_url,
        model=model,
        max_cost_usd=max_cost_usd,
        concurrency=None,
        dry_run=True,
        force_rerun=False,
    )
    config = build_run_config(
        user_config_path=default_user_config_path(),
        project_config_path=default_project_config_path(),
        cli_overrides=overrides,
    )
    result = asyncio.run(
        run_pipeline(
            input_path=input_path,
            config=config,
            output_dir=output,
            survey_only=True,
            title=title,
        )
    )
    _print_result(result)


@app.command("runs")
def runs_list(book_id: str | None = typer.Argument(None)) -> None:
    """List runs, optionally for a specific book_id."""
    root = runs_root()
    if not root.exists():
        console.print("(no runs yet)")
        return
    table = Table(title="Runs")
    table.add_column("Book ID")
    table.add_column("Run ID")
    table.add_column("Path")
    books = [root / book_id] if book_id else [b for b in root.iterdir() if b.is_dir()]
    for b in books:
        if not b.exists():
            continue
        for r in sorted(b.iterdir()):
            if r.is_dir():
                table.add_row(b.name, r.name, str(r))
    console.print(table)


def _print_result(result: Any) -> None:
    console.print()
    console.print("[bold green]✓ Done[/]")
    console.print(f"  run dir:        {result.run_dir}")
    console.print(f"  paragraphs:     {result.units_translated}")
    console.print(f"  flagged:        {result.flagged_count}")
    console.print(f"  estimated cost: ${result.cost_usd:.4f}")
    if (r := getattr(result, "toc_refinement", None)) is not None:
        if r.method == "llm":
            console.print(
                f"  toc refine:     {r.top_level_before} → {r.top_level_after} "
                f"top-level chapters (from {r.candidates} candidates)"
            )
        elif r.reason:
            console.print(f"  toc refine:     fallback ({r.reason})")
    if result.assemble:
        for mode, path in result.assemble.output_paths.items():
            console.print(f"  [{mode}]: {path}")
        console.print(f"  report:         {result.assemble.report_path}")
    if result.langfuse_status is not None:
        s = result.langfuse_status
        if s.enabled:
            mode = "full" if s.full_payload else "redacted"
            console.print(
                f"  langfuse:       enabled ({mode} payload) → {s.host}"
            )
        elif s.reason:
            console.print(f"  langfuse:       disabled ({s.reason})")


if __name__ == "__main__":  # pragma: no cover
    app()
