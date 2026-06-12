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
