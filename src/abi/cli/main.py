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
from abi.orchestrator.run import (
    InterruptDecisionRequest,
    approve_interrupt,
    cancel,
    inspect_run,
    make_book,
    resume,
    unblock,
)
from abi.project import BookProject, RunLedger
from abi.project.run_ledger import LedgerTransitionError
from abi.types.orchestration import (
    ActionOutcomeEnvelope,
    ActionStatus,
    CanonicalResolutionEvidence,
    Paused,
    RunResult,
    RunStatus,
    UnblockRequest,
)
from abi.types.run import RunConfig

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


def _config(config_file: Path | None, **ov: Any) -> RunConfig:
    load_dotenv()
    return build_run_config(
        user_config_path=default_user_config_path(),
        project_config_path=config_file or default_project_config_path(),
        cli_overrides=_overrides(**ov),
    )


@app.command("make-book")
def make_book_cmd(
    source: str = typer.Argument(..., help="Source file path or URL (.txt or .epub)."),
    source_target: str = typer.Option(
        "en-zh-Hans",
        "--source-target",
        "-st",
        help="Language-pair template, e.g. 'en-zh-Hans', 'ja-es', 'fr-en'.",
    ),
    title: str | None = typer.Option(None, "--title", help="Book slug / title."),
    books_root: Path = typer.Option(
        Path("books"), "--books-root", help="Root for book projects: {root}/{target}/..."
    ),
    mode: str = typer.Option(
        "public_domain",
        "--mode",
        help="public_domain | licensed | private_use.",
    ),
    profile: str | None = typer.Option(None, "--profile", help="Optional book-type profile."),
    base_url: str | None = typer.Option(None, "--base-url"),
    model: str | None = typer.Option(None, "--model"),
    max_cost_usd: float | None = typer.Option(None, "--max-cost-usd"),
    config_file: Path | None = typer.Option(None, "--config"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Scaffold a project, create one durable run, and drive it to a safe stop."""
    _setup_logging(verbose)
    config = _config(config_file, base_url=base_url, model=model, max_cost_usd=max_cost_usd)
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
        )
    )
    _print_result(project, result)


@app.command("resume")
def resume_cmd(
    project_root: Path = typer.Argument(..., help="Existing book-project directory."),
    base_url: str | None = typer.Option(None, "--base-url"),
    model: str | None = typer.Option(None, "--model"),
    max_cost_usd: float | None = typer.Option(None, "--max-cost-usd"),
    config_file: Path | None = typer.Option(None, "--config"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Resume the exact durable business run owned by a book project."""
    _setup_logging(verbose)
    config = _config(config_file, base_url=base_url, model=model, max_cost_usd=max_cost_usd)
    project, result = asyncio.run(
        resume(
            project_root=project_root,
            config=config,
        )
    )
    _print_result(project, result)


@app.command("inspect")
def inspect_cmd(
    project_root: Path = typer.Argument(..., help="Book-project directory."),
) -> None:
    """Inspect authoritative run facts and print the next safe recovery."""
    report = asyncio.run(inspect_run(project_root=project_root))
    console.print(f"[bold]{report.run.run_id}[/]  [green]{report.run.status.value}[/]")
    console.print(f"Plan version: {report.snapshot.plan_version}")
    actions = Table(title="Actions")
    actions.add_column("Capability")
    actions.add_column("Status")
    actions.add_column("Action ID")
    for action in report.actions:
        actions.add_row(action.capability, action.status.value, action.action_id)
    console.print(actions)
    console.print(f"Gates: {len(report.gate_receipts)} durable PASS receipt(s)")
    console.print(f"Outcome receipts: {len(report.outcome_receipts)}")
    console.print(f"Promotion intents: {len(report.promotion_intents)}")
    if report.current_hitl_interrupts:
        interrupts = Table(title="Current HITL interrupts")
        interrupts.add_column("Interrupt ID")
        interrupts.add_column("Action / attempt")
        interrupts.add_column("Claim")
        interrupts.add_column("Sequence")
        interrupts.add_column("Approve command")
        for item in report.current_hitl_interrupts:
            interrupts.add_row(
                item.interrupt_id,
                f"{item.action_id}:{item.attempt}",
                item.claim_status,
                "initial" if item.continuation_sequence is None else str(item.continuation_sequence),
                item.approve_command,
            )
        console.print(interrupts)
        for item in report.current_hitl_interrupts:
            console.print(item.approve_command, soft_wrap=True)
    console.print("Open incidents")
    for incident in report.open_incidents:
        console.print(f"  - {incident.error_code}: {incident.message}")
    remaining = report.snapshot.remaining_budget_usd
    console.print(
        f"Budget: spent=${report.budget_spent_usd:.4f} "
        f"remaining={'unlimited' if remaining is None else f'${remaining:.4f}'}"
    )
    console.print(f"Next safe recovery: {report.next_safe_recovery}")


@app.command("cancel")
def cancel_cmd(
    project_root: Path = typer.Argument(..., help="Book-project directory."),
) -> None:
    """Idempotently cancel a non-completed durable run."""
    run = asyncio.run(cancel(project_root=project_root))
    console.print(f"{run.run_id}: {run.status.value}")


@app.command("unblock")
def unblock_cmd(
    project_root: Path = typer.Argument(..., help="Book-project directory."),
    reason: str = typer.Option(..., "--reason"),
    evidence_refs: list[str] = typer.Option(..., "--evidence-ref"),
    source_action_id: str | None = typer.Option(None, "--source-action"),
    resolved_canonical: list[str] | None = typer.Option(
        None,
        "--resolved-canonical",
        help="PATH:removed or PATH:selected:SHA256; repeat for every conflict.",
    ),
) -> None:
    """Resume budget pause or replace one evidence-resolved integrity Action."""
    resolutions = tuple(_parse_resolution(value) for value in resolved_canonical or ())
    result = asyncio.run(
        unblock(
            project_root=project_root,
            request=UnblockRequest(
                reason=reason,
                evidence_refs=tuple(evidence_refs),
                source_action_id=source_action_id,
                canonical_resolutions=resolutions,
            ),
        )
    )
    console.print(f"{result.run_id}: {result.status.value}")
    if result.replacement_action_id is not None:
        console.print(f"replacement action: {result.replacement_action_id}")


def _parse_resolution(value: str) -> CanonicalResolutionEvidence:
    parts = value.rsplit(":", 2)
    if len(parts) == 2 and parts[1] == "removed":
        return CanonicalResolutionEvidence(
            canonical_relpath=parts[0],
            disposition="removed",
            evidence_ref="cli-canonical-resolution",
        )
    if len(parts) == 3 and parts[1] == "selected":
        return CanonicalResolutionEvidence(
            canonical_relpath=parts[0],
            disposition="selected",
            sha256=parts[2],
            evidence_ref="cli-canonical-resolution",
        )
    raise typer.BadParameter("resolved canonical must be PATH:removed or PATH:selected:SHA256")


@app.command("approve")
def approve_cmd(
    project_root: Path = typer.Argument(..., help="Book-project directory."),
    interrupt_id: str = typer.Argument(..., help="One public HITL interrupt ID."),
    decision: list[str] = typer.Option(
        ..., "--decision", help="approve | reject; repeat in checkpoint order."
    ),
    feedback: list[str] | None = typer.Option(
        None, "--feedback", help="Optional feedback; repeat one-for-one with decisions."
    ),
    base_url: str | None = typer.Option(None, "--base-url"),
    model: str | None = typer.Option(None, "--model"),
    max_cost_usd: float | None = typer.Option(None, "--max-cost-usd"),
    config_file: Path | None = typer.Option(None, "--config"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Resume one durable HITL interrupt by its public ID."""
    invalid = tuple(item for item in decision if item not in ("approve", "reject"))
    if invalid:
        raise typer.BadParameter("decision must be lowercase approve or reject")
    ordered_feedback: tuple[str | None, ...]
    if feedback is None:
        ordered_feedback = tuple(None for _ in decision)
    elif len(feedback) != len(decision):
        raise typer.BadParameter("feedback must be repeated one-for-one with decisions")
    else:
        ordered_feedback = tuple(feedback)
    _setup_logging(verbose)
    config = _config(
        config_file,
        base_url=base_url,
        model=model,
        max_cost_usd=max_cost_usd,
    )
    outcome = asyncio.run(
        approve_interrupt_by_id(
            project_root=project_root,
            interrupt_id=interrupt_id,
            decisions=tuple(decision),
            feedback=ordered_feedback,
            config=config,
        )
    )
    console.print(f"{outcome.action_id}:{outcome.attempt}: {outcome.outcome.kind}")


async def approve_interrupt_by_id(
    *,
    project_root: Path,
    interrupt_id: str,
    decisions: tuple[str, ...],
    feedback: tuple[str | None, ...],
    config: RunConfig,
) -> ActionOutcomeEnvelope:
    """Resolve one public interrupt solely from current authoritative ledger facts."""
    project = BookProject(Path(project_root).expanduser().resolve())
    report = await inspect_run(project_root=project.root)
    if report.run.status is not RunStatus.PAUSED_HITL:
        raise LedgerTransitionError(
            f"run {report.run.run_id} is not PAUSED_HITL; inspect before approving"
        )
    paused_actions = {
        action.action_id for action in report.actions if action.status is ActionStatus.PAUSED
    }
    matches: list[tuple[str, int]] = []
    async with RunLedger.open(project.run_db) as ledger:
        for receipt in report.outcome_receipts:
            if receipt.action_id not in paused_actions:
                continue
            effective = await ledger.get_effective_attempt_outcome(
                receipt.action_id, receipt.attempt
            )
            envelope = ActionOutcomeEnvelope.model_validate_json(effective.canonical_outcome_json)
            if not isinstance(envelope.outcome, Paused):
                continue
            if any(
                pending.interrupt_id == interrupt_id
                for pending in envelope.outcome.pending_hitl_interrupts
            ):
                matches.append((receipt.action_id, receipt.attempt))
    if len(matches) != 1:
        raise LedgerTransitionError(
            f"public interrupt {interrupt_id!r} matched {len(matches)} current paused "
            "attempts; inspect the durable run before approving"
        )
    action_id, attempt = matches[0]
    return await approve_interrupt(
        project_root=project.root,
        request=InterruptDecisionRequest(
            run_id=report.run.run_id,
            action_id=action_id,
            attempt=attempt,
            interrupt_id=interrupt_id,
            decisions=decisions,
            feedback=feedback,
        ),
        config=config,
    )


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
        ...,
        "--dataset",
        "-d",
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
            r.source_target,
            str(r.n),
            f"{r.ratio_p10}",
            f"{r.ratio_p50}",
            f"{r.ratio_p90}",
            f"[{r.suggested_lo}, {r.suggested_hi}]",
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
    console.print(
        f"[bold]{report.book}[/]  status={report.status}  verdict=[{color}]{report.verdict}[/]"
    )
    console.print(
        f"  gate_integrity={report.gate_integrity_ok}  "
        f"reached_states={report.reached_states_ok}  "
        f"path_conformance={report.path_conformance_ok}"
    )
    for g in report.gate_integrity:
        if not g.consistent:
            console.print(
                f"  [red]✗ {g.gate}[/]: recorded={g.recorded} "
                f"replay_ok={g.replay_ok} — {g.replay_reason}"
            )
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
        None, "--source-lang", help="Override source lang (default from durable run metadata)."
    ),
    target_lang: str | None = typer.Option(
        None, "--target-lang", help="Override target lang (default from durable run metadata)."
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

    console.print(f"[bold]{report.book}[/]  status={report.status}  总判定={_c(report.verdict)}")
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


def _print_result(project: Any, result: RunResult) -> None:
    console.print()
    icon = "[bold green]✓[/]" if result.status.value == "COMPLETED" else "[bold yellow]…[/]"
    console.print(f"{icon} run stopped at [bold]{result.status.value}[/]")
    console.print(f"  project: {project.root}")
    console.print(f"  cost:    ${result.cost_usd:.4f}")
    if result.blocked_reason:
        console.print(f"  [red]blocked:[/] {result.blocked_reason}")
        console.print(f"  resume with: [dim]abi resume {project.root}[/]")


if __name__ == "__main__":  # pragma: no cover
    app()
