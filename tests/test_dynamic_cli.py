"""Public CLI surface for durable dynamic runs."""

from __future__ import annotations

import asyncio
import hashlib
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from abi.actions.builtins import build_action_registry
from abi.cli.main import app
from abi.orchestrator import run as lifecycle
from abi.planning.context import SnapshotBuilder
from abi.planning.policy import PolicyEngine
from abi.project import (
    ArtifactStore,
    BookProject,
    RunLedger,
    RunSeed,
    ScaffoldRequest,
    scaffold_project,
)
from abi.project.run_ledger import (
    ArtifactCommit,
    LedgerConflictError,
    LedgerTransitionError,
    SuccessCommit,
)
from abi.types.orchestration import (
    ActionArgument,
    ActionOutcomeEnvelope,
    ActionStatus,
    AgentRunResult,
    ArtifactBundle,
    ArtifactBundleEntry,
    AttemptOutcomeReceiptPayload,
    AuthorizedAction,
    ExpectedArtifact,
    ExpectedArtifactManifest,
    GateArtifactIdentity,
    GateDecision,
    GateEvidence,
    GateReceiptPayload,
    Paused,
    PendingHitlActionReview,
    PendingHitlInterrupt,
    PermanentFailure,
    PlanPatch,
    ProposedAction,
    RepairRequired,
    RetryableFailure,
    RetryPolicySpec,
    RunStatus,
    Succeeded,
    canonical_bundle_json,
    canonical_manifest_json,
    canonical_model_json,
    sha256_canonical_json,
)
from abi.types.run import OrchestrationConfig, RunConfig


def test_root_help_lists_dynamic_lifecycle_commands() -> None:
    """Catch regressions that expose the old state table instead of run operations."""
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("make-book", "resume", "inspect", "approve", "unblock", "cancel"):
        assert command in result.stdout
    assert "state" not in result.stdout


def test_make_book_and_resume_remove_fixed_pipeline_until_option() -> None:
    """Catch compatibility shims that let callers target a fixed Status path."""
    runner = CliRunner()

    make_book_help = runner.invoke(app, ["make-book", "--help"])
    resume_help = runner.invoke(app, ["resume", "--help"])

    assert make_book_help.exit_code == 0
    assert resume_help.exit_code == 0
    assert "--until" not in make_book_help.stdout
    assert "--until" not in resume_help.stdout


def test_approve_cli_routes_ordered_decisions_by_public_interrupt_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch placeholder commands or CLI-side guesses of durable Action identity."""
    project_root = asyncio.run(_seed_hitl_pause(tmp_path))
    captured: dict[str, object] = {}

    async def _approve(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return ActionOutcomeEnvelope(
            action_id="hitl-action",
            attempt=1,
            outcome=PermanentFailure(
                error_code="operator_rejected",
                message="decision applied",
            ),
        )

    monkeypatch.setattr("abi.cli.main.approve_interrupt", _approve)
    result = CliRunner().invoke(
        app,
        [
            "approve",
            str(project_root),
            "public-interrupt-1",
            "--decision",
            "reject",
            "--feedback",
            "operator denied",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["project_root"] == project_root
    request = captured["request"]
    assert request.run_id == "hitl-run"
    assert request.action_id == "hitl-action"
    assert request.attempt == 1
    assert request.interrupt_id == "public-interrupt-1"
    assert request.decisions == ("reject",)
    assert request.feedback == ("operator denied",)
    assert isinstance(captured["config"], RunConfig)
    assert "hitl-action:1" in result.stdout


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (("--decision", "Approve"), "lowercase approve or reject"),
        (
            (
                "--decision",
                "approve",
                "--decision",
                "reject",
                "--feedback",
                "only one",
            ),
            "one-for-one",
        ),
    ),
)
def test_approve_cli_rejects_invalid_ordered_decision_input(
    tmp_path: Path, arguments: tuple[str, ...], message: str
) -> None:
    result = CliRunner().invoke(
        app,
        ["approve", str(tmp_path), "public-interrupt-1", *arguments],
    )

    assert result.exit_code != 0
    assert message in result.output


@pytest.mark.asyncio
async def test_default_composition_reaches_dynamic_controller_and_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch lifecycle defaults that require a hidden factory instead of real assembly."""
    planner_calls: list[str] = []

    class _Router:
        async def invoke_structured(self, schema, messages, **kwargs):  # type: ignore[no-untyped-def]
            planner_calls.append(kwargs["metadata"]["logical_invocation_id"])
            return (
                PlanPatch(
                    objective="ingest through default composition",
                    proposed_actions=(
                        ProposedAction(
                            proposal_id="ingest",
                            capability="source.ingest",
                            arguments=(
                                ActionArgument(
                                    name="source_relpath",
                                    value_json='"source/source_text_raw.txt"',
                                ),
                            ),
                        ),
                    ),
                    rationale="exercise Planner/Policy/Controller/DurableLoopRuntime",
                ),
                object(),
            )

    services = SimpleNamespace(
        router=_Router(),
        agent=object(),
        total_cost=0.0,
        flush=lambda: None,
    )
    monkeypatch.setattr(
        "abi.providers.services.build_run_services",
        lambda **kwargs: services,
    )
    source = tmp_path / "source.txt"
    source.write_text("A short public-domain source.", encoding="utf-8")
    config = RunConfig(orchestration=OrchestrationConfig(max_cycles=1, max_parallel_actions=1))

    project, first = await lifecycle.make_book(
        source=str(source),
        source_target="en-zh-hans",
        config=config,
        books_root=tmp_path / "books",
        project_root=tmp_path / "default-composition",
    )
    _, replay = await lifecycle.resume(project_root=project.root, config=config)

    assert len(planner_calls) == 1
    assert replay.run_id == first.run_id
    async with RunLedger.open(project.run_db) as ledger:
        actions = await ledger.list_actions(first.run_id)
        receipt = await ledger.get_attempt_outcome(actions[0].action_id, 1)
    assert actions[0].capability == "source.ingest"
    assert receipt.outcome_digest
    assert first.status is replay.status is RunStatus.BLOCKED


async def _seed_inspectable_project(root: Path) -> Path:
    project = scaffold_project(
        ScaffoldRequest(
            target_root=root,
            book_slug="inspectable",
            source_lang="en",
            target_lang="zh-hans",
            source_target="en-zh-hans",
        ),
        root=root / "project",
    )
    async with RunLedger.open(project.run_db) as ledger:
        run_id = await ledger.create_run(
            RunSeed(run_id="inspect-run", book_slug="inspectable", budget_usd=10)
        )
        registry = build_action_registry()
        patch = PlanPatch(
            objective="ingest",
            proposed_actions=(ProposedAction(proposal_id="ingest", capability="source.ingest"),),
            rationale="seed an inspectable authorized action",
        )
        context = await SnapshotBuilder(ledger=ledger, registry=registry).build(run_id)
        plan = await ledger.append_plan(run_id, patch)
        decision = PolicyEngine(registry).authorize(
            context.policy_snapshot, patch, next_plan_version=plan.version
        )
        assert decision.authorized
        await ledger.authorize_actions(run_id, decision.actions)
        await ledger.record_incident(
            run_id,
            error_code="waiting_for_fixture",
            message="fixture needs external evidence",
        )
    return project.root


def test_inspect_prints_ledger_facts_and_next_safe_recovery(tmp_path: Path) -> None:
    """Catch inspect implementations that read projections or fixed statuses."""
    project_root = asyncio.run(_seed_inspectable_project(tmp_path))

    result = CliRunner().invoke(app, ["inspect", str(project_root)])

    assert result.exit_code == 0
    for expected in (
        "inspect-run",
        "RUNNING",
        "Plan version: 1",
        "source.ingest",
        "AUTHORIZED",
        "Gates",
        "Outcome receipts",
        "Open incidents",
        "waiting_for_fixture",
        "Budget",
        "Next safe recovery",
        "abi resume",
    ):
        assert expected in result.stdout


def test_inspect_exposes_current_public_hitl_ids_and_copyable_approve_command(
    tmp_path: Path,
) -> None:
    """Catch a PAUSED_HITL run whose public interrupt is only discoverable in SQLite."""
    project_root = asyncio.run(_seed_hitl_pause(tmp_path))

    report = asyncio.run(lifecycle.inspect_run(project_root=project_root))
    assert len(report.current_hitl_interrupts) == 1
    current = report.current_hitl_interrupts[0]
    assert current.run_id == "hitl-run"
    assert current.action_id == "hitl-action"
    assert current.attempt == 1
    assert current.interrupt_id == "public-interrupt-1"
    assert current.claim_status == "UNCLAIMED"
    assert current.continuation_sequence is None
    assert current.approve_command.endswith(
        "public-interrupt-1 --decision approve"
    )
    assert "INTERRUPT_ID" not in report.next_safe_recovery

    rendered = CliRunner().invoke(app, ["inspect", str(project_root)])
    assert rendered.exit_code == 0, rendered.output
    assert "public-interrupt-1" in rendered.stdout
    assert "UNCLAIMED" in rendered.stdout
    assert current.approve_command in rendered.stdout
    assert "INTERRUPT_ID" not in rendered.stdout


async def _run_status(project_root: Path) -> RunStatus:
    async with RunLedger.open(project_root / "state/run.db") as ledger:
        (run,) = await ledger.list_runs()
        return run.status


def test_cancel_is_idempotent_and_completed_run_stays_completed(tmp_path: Path) -> None:
    """Catch cancel paths that reject replay or mutate a terminal completed run."""
    cancelled_root = asyncio.run(_seed_inspectable_project(tmp_path / "cancelled"))
    runner = CliRunner()

    first = runner.invoke(app, ["cancel", str(cancelled_root)])
    second = runner.invoke(app, ["cancel", str(cancelled_root)])

    assert first.exit_code == second.exit_code == 0
    assert asyncio.run(_run_status(cancelled_root)) is RunStatus.CANCELLED

    completed = scaffold_project(
        ScaffoldRequest(
            target_root=tmp_path,
            book_slug="completed",
            source_lang="en",
            target_lang="zh-hans",
            source_target="en-zh-hans",
        ),
        root=tmp_path / "completed",
    )

    async def seed_completed() -> None:
        async with RunLedger.open(completed.run_db) as ledger:
            run_id = await ledger.create_run(RunSeed(run_id="completed-run"))
            await ledger.append_plan(
                run_id,
                PlanPatch(
                    objective="complete fixture",
                    proposed_actions=(
                        ProposedAction(proposal_id="done", capability="source.ingest"),
                    ),
                    rationale="create one committed Action before completion",
                ),
            )
            await ledger.authorize_actions(run_id, (_authorized_action(action_id="done-action"),))
            await ledger.start_attempt("done-action")
            bundle = ArtifactBundle(
                action_id="done-action",
                attempt=1,
                entries=(
                    ArtifactBundleEntry(
                        staged_relpath=("state/staging/done-action/1/source/conflicted.json"),
                        canonical_relpath="source/conflicted.json",
                        media_type="application/json",
                        evidence_role="source_manifest",
                    ),
                ),
            )
            outcome = Succeeded(artifact_bundle=bundle, evidence_refs=("source_manifest",))
            envelope_json = canonical_model_json(
                ActionOutcomeEnvelope(action_id="done-action", attempt=1, outcome=outcome)
            )
            bundle_json = canonical_bundle_json(bundle)
            bundle_digest = sha256_canonical_json(bundle_json)
            await ledger.record_attempt_outcome(
                AttemptOutcomeReceiptPayload(
                    action_id="done-action",
                    attempt=1,
                    canonical_outcome_json=envelope_json,
                    outcome_digest=sha256_canonical_json(envelope_json),
                    canonical_bundle_json=bundle_json,
                    bundle_digest=bundle_digest,
                    evidence_refs=("source_manifest",),
                )
            )
            checksum = "a" * 64
            decision = GateDecision(
                passed=True,
                reason_code="fixture_pass",
                message="fixture complete",
                validator_id="source.ingest",
                validator_version="1",
                bundle_digest=bundle_digest,
                artifact_checksums=(checksum,),
                evidence_refs=("source_manifest",),
            )
            decision_json = canonical_model_json(decision)
            _, intents = await ledger.create_gate_receipt_and_bundle_intents(
                GateReceiptPayload(
                    action_id="done-action",
                    attempt=1,
                    validator_id="source.ingest",
                    validator_version="1",
                    canonical_gate_decision_json=decision_json,
                    gate_decision_digest=sha256_canonical_json(decision_json),
                    bundle_digest=bundle_digest,
                    artifacts=(
                        GateArtifactIdentity(
                            staged_relpath=("state/staging/done-action/1/source/conflicted.json"),
                            canonical_relpath="source/conflicted.json",
                            checksum=checksum,
                        ),
                    ),
                    evidence_refs=("source_manifest",),
                )
            )
            await ledger.commit_promotion_intent(intents[0].intent_id)
            await ledger.commit_success(
                SuccessCommit(
                    action_id="done-action",
                    artifacts=(
                        ArtifactCommit(
                            artifact_id="done-artifact",
                            relpath="source/conflicted.json",
                            sha256=checksum,
                            producer_action_id="done-action",
                            media_type="application/json",
                        ),
                    ),
                    gate_evidence=(
                        GateEvidence(
                            evidence_id="done-gate",
                            gate="source.ingest",
                            passed=True,
                            validator_version="1",
                            artifact_checksums=(checksum,),
                        ),
                    ),
                )
            )
            await ledger.set_run_status(run_id, RunStatus.COMPLETED)

    asyncio.run(seed_completed())
    terminal = runner.invoke(app, ["cancel", str(completed.root)])

    assert terminal.exit_code == 0
    assert "COMPLETED" in terminal.stdout
    assert asyncio.run(_run_status(completed.root)) is RunStatus.COMPLETED


def _authorized_action(
    action_id: str = "broken-action", *, retry_policy: RetryPolicySpec | None = None
) -> AuthorizedAction:
    manifest = ExpectedArtifactManifest(
        action_id=action_id,
        entries=(
            ExpectedArtifact(
                canonical_relpath="source/conflicted.json",
                media_type="application/json",
                evidence_role="source_manifest",
            ),
        ),
    )
    retry = retry_policy or RetryPolicySpec(max_attempts=1)
    return AuthorizedAction(
        action_id=action_id,
        proposal_id="broken",
        plan_version=1,
        capability="source.ingest",
        parameters_json='{"source_relpath":"source/source_text_raw.txt"}',
        write_set=("source",),
        idempotency_key=f"action:{action_id}",
        expected_artifact_manifest=manifest,
        expected_artifact_manifest_digest=sha256_canonical_json(canonical_manifest_json(manifest)),
        expected_evidence_refs=("source_manifest",),
        retry_policy=retry,
        retry_policy_fingerprint=sha256_canonical_json(canonical_model_json(retry)),
    )


async def _seed_integrity_conflict(root: Path) -> tuple[Path, str, str, bytes]:
    project = scaffold_project(_request_for(root), root=root / "integrity")
    async with RunLedger.open(project.run_db) as ledger:
        run_id = await ledger.create_run(RunSeed(run_id="integrity-run"))
        await ledger.append_plan(
            run_id,
            PlanPatch(
                objective="seed conflict",
                proposed_actions=(
                    ProposedAction(proposal_id="broken", capability="source.ingest"),
                ),
                rationale="exercise immutable manual recovery",
            ),
        )
        await ledger.authorize_actions(run_id, (_authorized_action(),))
        await ledger.start_attempt("broken-action")
        store = ArtifactStore(project, ledger)
        try:
            writer = store.writer("broken-action", 1)
            writer.write_text(
                "source/conflicted.json",
                '{"old":true}\n',
                media_type="application/json",
                evidence_role="source_manifest",
            )
            bundle = writer.artifact_bundle()
            old_bytes = writer.read_bytes("source/conflicted.json")
        finally:
            store.close()
        outcome = Succeeded(artifact_bundle=bundle, evidence_refs=("source_manifest",))
        envelope_json = canonical_model_json(
            ActionOutcomeEnvelope(action_id="broken-action", attempt=1, outcome=outcome)
        )
        bundle_json = canonical_bundle_json(bundle)
        bundle_digest = sha256_canonical_json(bundle_json)
        await ledger.record_attempt_outcome(
            AttemptOutcomeReceiptPayload(
                action_id="broken-action",
                attempt=1,
                canonical_outcome_json=envelope_json,
                outcome_digest=sha256_canonical_json(envelope_json),
                canonical_bundle_json=bundle_json,
                bundle_digest=bundle_digest,
                evidence_refs=("source_manifest",),
            )
        )
        checksum = hashlib.sha256(old_bytes).hexdigest()
        decision = GateDecision(
            passed=True,
            reason_code="fixture_pass",
            message="fixture passed before promotion conflict",
            validator_id="source.ingest",
            validator_version="1",
            bundle_digest=bundle_digest,
            artifact_checksums=(checksum,),
            evidence_refs=("source_manifest",),
        )
        decision_json = canonical_model_json(decision)
        _, intents = await ledger.create_gate_receipt_and_bundle_intents(
            GateReceiptPayload(
                action_id="broken-action",
                attempt=1,
                validator_id="source.ingest",
                validator_version="1",
                canonical_gate_decision_json=decision_json,
                gate_decision_digest=sha256_canonical_json(decision_json),
                bundle_digest=bundle_digest,
                artifacts=(
                    GateArtifactIdentity(
                        staged_relpath=("state/staging/broken-action/1/source/conflicted.json"),
                        canonical_relpath="source/conflicted.json",
                        checksum=checksum,
                    ),
                ),
                evidence_refs=("source_manifest",),
            )
        )
        await ledger.conflict_promotion_intent(
            intents[0].intent_id,
            error_code="artifact_checksum_conflict",
            message="operator must select or remove the canonical conflict",
        )
        return project.root, envelope_json, intents[0].intent_id, old_bytes


def _request_for(root: Path) -> ScaffoldRequest:
    return ScaffoldRequest(
        target_root=root,
        book_slug="lifecycle",
        source_lang="en",
        target_lang="zh-hans",
        source_target="en-zh-hans",
    )


@pytest.mark.asyncio
async def test_integrity_unblock_preserves_history_and_creates_replacement(
    tmp_path: Path,
) -> None:
    """Catch unblock paths that reset a REPAIR_REQUIRED Action or old evidence."""
    project_root, old_outcome_json, intent_id, old_staged = await _seed_integrity_conflict(tmp_path)
    request_type = lifecycle.UnblockRequest
    evidence_type = lifecycle.CanonicalResolutionEvidence

    result = await lifecycle.unblock(
        project_root=project_root,
        request=request_type(
            reason="operator removed the conflicting canonical candidate",
            evidence_refs=("ticket-42",),
            source_action_id="broken-action",
            canonical_resolutions=(
                evidence_type(
                    canonical_relpath="source/conflicted.json",
                    disposition="removed",
                    evidence_ref="ticket-42",
                ),
            ),
        ),
    )

    async with RunLedger.open(project_root / "state/run.db") as ledger:
        (run,) = await ledger.list_runs()
        actions = await ledger.list_actions(run.run_id)
        old_action = await ledger.get_action("broken-action")
        old_receipt = await ledger.get_attempt_outcome("broken-action", 1)
        old_intent = await ledger.get_promotion_intent(intent_id)
        replacement = await ledger.get_action(result.replacement_action_id)
        replacement_attempt = await ledger.get_attempt(replacement.action_id, 1)
    assert run.status is RunStatus.RUNNING
    assert old_action.status.value == "REPAIR_REQUIRED"
    assert old_receipt.canonical_outcome_json == old_outcome_json
    assert old_intent.status == "CONFLICT"
    assert len(actions) == 2
    assert replacement.action_id != old_action.action_id
    assert replacement.plan_version == 2
    assert replacement_attempt.staging_relpath == (f"state/staging/{replacement.action_id}/1")
    assert (
        project_root / "state/staging/broken-action/1/source/conflicted.json"
    ).read_bytes() == old_staged


@pytest.mark.asyncio
async def test_unblock_rejects_semantic_repair_and_budget_resume_needs_no_replacement(
    tmp_path: Path,
) -> None:
    """Catch manual bypass of automatic semantic repair or needless budget replans."""
    semantic = scaffold_project(_request_for(tmp_path), root=tmp_path / "semantic")
    async with RunLedger.open(semantic.run_db) as ledger:
        run_id = await ledger.create_run(RunSeed(run_id="semantic-run"))
        await ledger.append_plan(
            run_id,
            PlanPatch(
                objective="semantic fixture",
                proposed_actions=(
                    ProposedAction(proposal_id="broken", capability="source.ingest"),
                ),
                rationale="semantic repair remains automatic",
            ),
        )
        await ledger.authorize_actions(run_id, (_authorized_action(),))
        await ledger.start_attempt("broken-action")
        outcome = RepairRequired(
            repair_class="semantic",
            repair_source="action_outcome",
            reason_code="term_drift",
            defect_codes=("term_drift",),
            message="terminology drift",
        )
        envelope_json = canonical_model_json(
            ActionOutcomeEnvelope(action_id="broken-action", attempt=1, outcome=outcome)
        )
        await ledger.record_attempt_outcome(
            AttemptOutcomeReceiptPayload(
                action_id="broken-action",
                attempt=1,
                canonical_outcome_json=envelope_json,
                outcome_digest=sha256_canonical_json(envelope_json),
            )
        )
        await ledger.record_repair_required(
            action_id="broken-action",
            attempt=1,
            repair_class="semantic",
            repair_source="action_outcome",
            reason_code="term_drift",
            defect_codes=("term_drift",),
            evidence_refs=(),
            message="terminology drift",
            semantic_reason_mapped=True,
        )

    request_type = lifecycle.UnblockRequest
    with pytest.raises(LedgerTransitionError, match=r"semantic.*automatic"):
        await lifecycle.unblock(
            project_root=semantic.root,
            request=request_type(
                reason="try to bypass planner",
                evidence_refs=("ticket-99",),
                source_action_id="broken-action",
            ),
        )

    budget = scaffold_project(_request_for(tmp_path), root=tmp_path / "budget")
    async with RunLedger.open(budget.run_db) as ledger:
        budget_run_id = await ledger.create_run(RunSeed(run_id="budget-run"))
        await ledger.set_run_status(budget_run_id, RunStatus.PAUSED_BUDGET)
    resumed = await lifecycle.unblock(
        project_root=budget.root,
        request=request_type(
            reason="operator raised the budget cap",
            evidence_refs=("budget-change-7",),
        ),
    )
    async with RunLedger.open(budget.run_db) as ledger:
        (budget_run,) = await ledger.list_runs()
        budget_actions = await ledger.list_actions(budget_run.run_id)
    assert resumed.replacement_action_id is None
    assert budget_run.status is RunStatus.RUNNING
    assert budget_actions == ()


async def _seed_hitl_pause(
    root: Path,
    *,
    paused_run: bool = True,
    retry_policy: RetryPolicySpec | None = None,
    authorized_action: AuthorizedAction | None = None,
) -> Path:
    project = scaffold_project(_request_for(root), root=root / "hitl")
    async with RunLedger.open(project.run_db) as ledger:
        run_id = await ledger.create_run(RunSeed(run_id="hitl-run"))
        await ledger.append_plan(
            run_id,
            PlanPatch(
                objective="pause for approval",
                proposed_actions=(
                    ProposedAction(proposal_id="review", capability="source.ingest"),
                ),
                rationale="exercise the public interrupt boundary",
            ),
        )
        await ledger.authorize_actions(
            run_id,
            (
                authorized_action
                or _authorized_action(action_id="hitl-action", retry_policy=retry_policy),
            ),
        )
        await ledger.start_attempt("hitl-action")
        paused = Paused(
            reason="hitl",
            message="approve the side effect",
            pending_hitl_interrupts=(
                PendingHitlInterrupt(
                    interrupt_id="public-interrupt-1",
                    action_reviews=(
                        PendingHitlActionReview(
                            tool_name="publish",
                            arguments_json="{}",
                            allowed_decisions=("approve", "reject"),
                        ),
                    ),
                ),
            ),
        )
        receipt_json = canonical_model_json(
            ActionOutcomeEnvelope(action_id="hitl-action", attempt=1, outcome=paused)
        )
        await ledger.record_attempt_outcome(
            AttemptOutcomeReceiptPayload(
                action_id="hitl-action",
                attempt=1,
                canonical_outcome_json=receipt_json,
                outcome_digest=sha256_canonical_json(receipt_json),
            )
        )
        await ledger.finish_attempt("hitl-action", attempt=1, status=ActionStatus.PAUSED)
        if paused_run:
            await ledger.set_run_status(run_id, RunStatus.PAUSED_HITL)
    return project.root


@pytest.mark.asyncio
async def test_approve_resumes_exact_public_interrupt_once_and_records_decision(
    tmp_path: Path,
) -> None:
    """Catch saver inspection, rotated thread IDs, or duplicate checkpoint resume."""
    project_root = await _seed_hitl_pause(tmp_path)
    decision_type = lifecycle.InterruptDecisionRequest
    calls: list[object] = []

    class _Continuation:
        async def resume_hitl(self, request):  # type: ignore[no-untyped-def]
            calls.append(request)
            return ActionOutcomeEnvelope(
                action_id=request.action_id,
                attempt=request.attempt,
                outcome=PermanentFailure(
                    error_code="operator_rejected",
                    message="human decision routed through resumed typed outcome",
                ),
            )

    request = decision_type(
        run_id="hitl-run",
        action_id="hitl-action",
        attempt=1,
        interrupt_id="public-interrupt-1",
        decisions=("reject",),
        feedback=("not authorized",),
    )
    first = await lifecycle.approve_interrupt(
        project_root=project_root,
        request=request,
        continuation=_Continuation(),
    )
    replay = await lifecycle.approve_interrupt(
        project_root=project_root,
        request=request,
        continuation=_Continuation(),
    )

    assert first == replay
    assert len(calls) == 1
    continuation_request = calls[0]
    assert continuation_request.run_id == "hitl-run"
    assert continuation_request.action_id == "hitl-action"
    assert continuation_request.attempt == 1
    assert continuation_request.thread_id == "hitl-run/hitl-action/1"
    assert continuation_request.interrupt_id == "public-interrupt-1"
    assert continuation_request.decisions == ("reject",)
    async with RunLedger.open(project_root / "state/run.db") as ledger:
        durable = await ledger.get_interrupt_decision("public-interrupt-1")
        original = await ledger.get_attempt_outcome("hitl-action", 1)
        effective = await ledger.get_effective_attempt_outcome("hitl-action", 1)
        continuations = await ledger.list_hitl_continuation_receipts("hitl-action", 1)
        run = await ledger.get_run("hitl-run")
        action = await ledger.get_action("hitl-action")
    assert durable.decisions == ("reject",)
    assert durable.feedback == ("not authorized",)
    assert durable.status == "RESOLVED"
    assert isinstance(
        ActionOutcomeEnvelope.model_validate_json(original.canonical_outcome_json).outcome,
        Paused,
    )
    assert effective.canonical_outcome_json == continuations[-1].canonical_outcome_json
    assert isinstance(
        ActionOutcomeEnvelope.model_validate_json(effective.canonical_outcome_json).outcome,
        PermanentFailure,
    )
    assert run.status is RunStatus.BLOCKED
    assert action.status is ActionStatus.PERMANENT_FAILED

    with pytest.raises(LedgerConflictError, match=r"decision.*durable"):
        await lifecycle.approve_interrupt(
            project_root=project_root,
            request=request.model_copy(update={"decisions": ("approve",)}),
            continuation=_Continuation(),
        )
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update", "paused_run"),
    (
        ({"interrupt_id": "stale-interrupt"}, True),
        ({"run_id": "wrong-run"}, True),
        ({"action_id": "wrong-action"}, True),
        ({"attempt": 2}, True),
        ({}, False),
    ),
)
async def test_approve_rejects_wrong_or_nonpaused_identity_without_resume(
    tmp_path: Path,
    update: dict[str, object],
    paused_run: bool,
) -> None:
    """Catch best-effort matching of stale or foreign checkpoint identities."""
    project_root = await _seed_hitl_pause(tmp_path, paused_run=paused_run)
    decision_type = lifecycle.InterruptDecisionRequest
    calls = 0

    class _Continuation:
        async def resume_hitl(self, request):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            raise AssertionError(f"unexpected resume: {request}")

    request = decision_type(
        run_id="hitl-run",
        action_id="hitl-action",
        attempt=1,
        interrupt_id="public-interrupt-1",
        decisions=("approve",),
        feedback=(None,),
    ).model_copy(update=update)
    with pytest.raises(LedgerTransitionError, match=r"interrupt|PAUSED_HITL|identity"):
        await lifecycle.approve_interrupt(
            project_root=project_root,
            request=request,
            continuation=_Continuation(),
        )
    assert calls == 0


@pytest.mark.asyncio
async def test_approve_recovers_claim_and_resumes_sequential_interrupt_history(
    tmp_path: Path,
) -> None:
    """Catch overwritten pause receipts or crash claims that cannot resume safely."""
    project_root = await _seed_hitl_pause(tmp_path)
    decision_type = lifecycle.InterruptDecisionRequest
    calls: list[object] = []

    class _Continuation:
        recovered: ActionOutcomeEnvelope | None = None

        async def inspect_hitl(self, request):  # type: ignore[no-untyped-def]
            if self.recovered is None:
                return lifecycle.HitlRecoveryInspection(disposition="indeterminate")
            return lifecycle.HitlRecoveryInspection(disposition="outcome", outcome=self.recovered)

        async def resume_hitl(self, request):  # type: ignore[no-untyped-def]
            calls.append(request)
            if len(calls) == 1:
                self.recovered = ActionOutcomeEnvelope(
                    action_id=request.action_id,
                    attempt=request.attempt,
                    outcome=Paused(
                        reason="hitl",
                        message="approve the second side effect",
                        pending_hitl_interrupts=(
                            PendingHitlInterrupt(
                                interrupt_id="public-interrupt-2",
                                action_reviews=(
                                    PendingHitlActionReview(
                                        tool_name="deliver",
                                        arguments_json='{"chapter":"002"}',
                                        allowed_decisions=("approve", "reject"),
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
                raise RuntimeError("crash after durable decision claim")
            if request.interrupt_id == "public-interrupt-1":
                raise AssertionError("claimed replay must inspect before any resume")
            return ActionOutcomeEnvelope(
                action_id=request.action_id,
                attempt=request.attempt,
                outcome=PermanentFailure(
                    error_code="operator_rejected_second",
                    message="second decision reached controller authority",
                ),
            )

    first = decision_type(
        run_id="hitl-run",
        action_id="hitl-action",
        attempt=1,
        interrupt_id="public-interrupt-1",
        decisions=("approve",),
        feedback=(None,),
    )
    continuation = _Continuation()
    with pytest.raises(RuntimeError, match="crash after durable"):
        await lifecycle.approve_interrupt(
            project_root=project_root,
            request=first,
            continuation=continuation,
        )
    async with RunLedger.open(project_root / "state/run.db") as ledger:
        claimed = await ledger.get_interrupt_decision("public-interrupt-1")
    assert claimed.status == "STARTED"
    assert claimed.decisions == ("approve",)
    assert claimed.resume_invocation_id
    assert claimed.resume_started_at is not None

    await lifecycle.approve_interrupt(
        project_root=project_root,
        request=first,
        continuation=continuation,
    )
    second = decision_type(
        run_id="hitl-run",
        action_id="hitl-action",
        attempt=1,
        interrupt_id="public-interrupt-2",
        decisions=("reject",),
        feedback=("second denied",),
    )
    await lifecycle.approve_interrupt(
        project_root=project_root,
        request=second,
        continuation=continuation,
    )

    assert [request.interrupt_id for request in calls] == [
        "public-interrupt-1",
        "public-interrupt-2",
    ]
    assert all(request.thread_id == "hitl-run/hitl-action/1" for request in calls)
    async with RunLedger.open(project_root / "state/run.db") as ledger:
        original = await ledger.get_attempt_outcome("hitl-action", 1)
        continuations = await ledger.list_hitl_continuation_receipts("hitl-action", 1)
    assert isinstance(
        ActionOutcomeEnvelope.model_validate_json(original.canonical_outcome_json).outcome,
        Paused,
    )
    assert len(continuations) == 2
    assert isinstance(
        ActionOutcomeEnvelope.model_validate_json(continuations[0].canonical_outcome_json).outcome,
        Paused,
    )
    assert isinstance(
        ActionOutcomeEnvelope.model_validate_json(continuations[1].canonical_outcome_json).outcome,
        PermanentFailure,
    )


@pytest.mark.asyncio
async def test_started_hitl_replay_blocks_old_pending_without_second_resume(
    tmp_path: Path,
) -> None:
    """Catch an approved side effect being re-entered before its next checkpoint."""
    project_root = await _seed_hitl_pause(tmp_path)
    calls = 0

    class _Continuation:
        async def resume_hitl(self, request):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            raise RuntimeError("approved tool started before checkpoint advanced")

        async def inspect_hitl(self, request):  # type: ignore[no-untyped-def]
            return lifecycle.HitlRecoveryInspection(disposition="not_started")

    request = lifecycle.InterruptDecisionRequest(
        run_id="hitl-run",
        action_id="hitl-action",
        attempt=1,
        interrupt_id="public-interrupt-1",
        decisions=("approve",),
        feedback=(None,),
    )
    continuation = _Continuation()
    with pytest.raises(RuntimeError, match="tool started"):
        await lifecycle.approve_interrupt(
            project_root=project_root,
            request=request,
            continuation=continuation,
        )
    async with RunLedger.open(project_root / "state/run.db") as ledger:
        started = await ledger.get_interrupt_decision("public-interrupt-1")
    assert started.status == "STARTED"

    with pytest.raises(LedgerTransitionError, match=r"indeterminate|blind resume"):
        await lifecycle.approve_interrupt(
            project_root=project_root,
            request=request,
            continuation=continuation,
        )
    async with RunLedger.open(project_root / "state/run.db") as ledger:
        run = await ledger.get_run("hitl-run")
        incidents = await ledger.list_incidents("hitl-run", open_only=True)
    assert calls == 1
    assert run.status is RunStatus.BLOCKED
    assert incidents[-1].error_code == "hitl_continuation_indeterminate"
    assert incidents[-1].repair_class == "integrity"
    assert incidents[-1].repair_source == "integrity_guard"
    assert incidents[-1].reason_code == "hitl_resume_indeterminate"

    inspection = await lifecycle.inspect_run(project_root=project_root)
    assert "--source-action hitl-action" in inspection.next_safe_recovery
    assert "--resolved-canonical" not in inspection.next_safe_recovery
    copied_command = shlex.split(inspection.next_safe_recovery)
    assert copied_command[:2] == ["abi", "unblock"]
    copied_command[copied_command.index("REASON")] = "operator-verified-side-effect"
    copied_command[copied_command.index("SIDE_EFFECT_EVIDENCE")] = (
        "operator-side-effect-check-1"
    )
    executed = await asyncio.to_thread(CliRunner().invoke, app, copied_command[1:])
    assert executed.exit_code == 0, executed.output

    async with RunLedger.open(project_root / "state/run.db") as ledger:
        actions = await ledger.list_actions("hitl-run")
        replacement = next(action for action in actions if action.action_id != "hitl-action")
        old_action = await ledger.get_action("hitl-action")
        old_attempt = await ledger.get_attempt("hitl-action", 1)
        old_receipt = await ledger.get_attempt_outcome("hitl-action", 1)
        old_claim = await ledger.get_interrupt_decision("public-interrupt-1")
        continuations = await ledger.list_hitl_continuation_receipts(
            "hitl-action", 1
        )
    assert old_action.status is ActionStatus.INDETERMINATE
    assert old_attempt.status is ActionStatus.INDETERMINATE
    assert isinstance(
        ActionOutcomeEnvelope.model_validate_json(
            old_receipt.canonical_outcome_json
        ).outcome,
        Paused,
    )
    assert old_claim.status == "STARTED"
    assert continuations == ()
    assert replacement.status is ActionStatus.AUTHORIZED
    assert replacement.action_id in executed.output


@pytest.mark.asyncio
async def test_resolved_hitl_replay_routes_cached_outcome_after_reconcile_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch a continuation receipt becoming RESOLVED but never reaching Task 8."""
    project_root = await _seed_hitl_pause(tmp_path)
    resumes = 0
    reconciliations = 0
    real_reconcile = lifecycle._reconcile_effective_outcome

    class _Continuation:
        async def resume_hitl(self, request):  # type: ignore[no-untyped-def]
            nonlocal resumes
            resumes += 1
            return ActionOutcomeEnvelope(
                action_id=request.action_id,
                attempt=request.attempt,
                outcome=PermanentFailure(
                    error_code="operator_rejected",
                    message="cached outcome still requires deterministic routing",
                ),
            )

    async def crash_once(project, ledger, run):  # type: ignore[no-untyped-def]
        nonlocal reconciliations
        reconciliations += 1
        if reconciliations == 1:
            raise RuntimeError("crash after continuation commit")
        await real_reconcile(project, ledger, run)

    monkeypatch.setattr(lifecycle, "_reconcile_effective_outcome", crash_once)
    request = lifecycle.InterruptDecisionRequest(
        run_id="hitl-run",
        action_id="hitl-action",
        attempt=1,
        interrupt_id="public-interrupt-1",
        decisions=("reject",),
        feedback=(None,),
    )
    continuation = _Continuation()
    with pytest.raises(RuntimeError, match="continuation commit"):
        await lifecycle.approve_interrupt(
            project_root=project_root,
            request=request,
            continuation=continuation,
        )

    replay = await lifecycle.approve_interrupt(
        project_root=project_root,
        request=request,
        continuation=continuation,
    )
    async with RunLedger.open(project_root / "state/run.db") as ledger:
        action = await ledger.get_action("hitl-action")
        run = await ledger.get_run("hitl-run")
    assert isinstance(replay.outcome, PermanentFailure)
    assert resumes == 1
    assert reconciliations == 2
    assert action.status is ActionStatus.PERMANENT_FAILED
    assert run.status is RunStatus.BLOCKED


@pytest.mark.asyncio
async def test_effective_hitl_retry_uses_frozen_retry_authority(
    tmp_path: Path,
) -> None:
    """Catch retry routing that consults the original Paused receipt instead of continuation."""
    project_root = await _seed_hitl_pause(
        tmp_path,
        retry_policy=RetryPolicySpec(max_attempts=2, retryable_codes=("transient_provider_error",)),
    )
    decision_type = lifecycle.InterruptDecisionRequest

    class _Continuation:
        async def resume_hitl(self, request):  # type: ignore[no-untyped-def]
            return ActionOutcomeEnvelope(
                action_id=request.action_id,
                attempt=request.attempt,
                outcome=RetryableFailure(
                    error_code="transient_provider_error",
                    message="retry through the frozen original action policy",
                ),
            )

    await lifecycle.approve_interrupt(
        project_root=project_root,
        request=decision_type(
            run_id="hitl-run",
            action_id="hitl-action",
            attempt=1,
            interrupt_id="public-interrupt-1",
            decisions=("approve",),
            feedback=(None,),
        ),
        continuation=_Continuation(),
    )
    async with RunLedger.open(project_root / "state/run.db") as ledger:
        original = await ledger.get_attempt_outcome("hitl-action", 1)
        action = await ledger.get_action("hitl-action")
        successor = await ledger.create_next_attempt("hitl-action", previous_attempt=1)
    assert isinstance(
        ActionOutcomeEnvelope.model_validate_json(original.canonical_outcome_json).outcome,
        Paused,
    )
    assert action.status is ActionStatus.RETRY_WAIT
    assert successor.attempt == 2
    assert successor.status is ActionStatus.AUTHORIZED
    assert successor.staging_relpath == "state/staging/hitl-action/2"


@pytest.mark.asyncio
async def test_effective_hitl_semantic_repair_uses_repair_authority(
    tmp_path: Path,
) -> None:
    """Catch semantic repair classification that reads only the original pause."""
    project_root = await _seed_hitl_pause(tmp_path)
    decision_type = lifecycle.InterruptDecisionRequest

    class _Continuation:
        async def resume_hitl(self, request):  # type: ignore[no-untyped-def]
            return ActionOutcomeEnvelope(
                action_id=request.action_id,
                attempt=request.attempt,
                outcome=RepairRequired(
                    repair_class="semantic",
                    repair_source="action_outcome",
                    reason_code="term_drift",
                    defect_codes=("term_drift",),
                    message="mapped terminology repair",
                ),
            )

    await lifecycle.approve_interrupt(
        project_root=project_root,
        request=decision_type(
            run_id="hitl-run",
            action_id="hitl-action",
            attempt=1,
            interrupt_id="public-interrupt-1",
            decisions=("approve",),
            feedback=(None,),
        ),
        continuation=_Continuation(),
    )
    async with RunLedger.open(project_root / "state/run.db") as ledger:
        original = await ledger.get_attempt_outcome("hitl-action", 1)
        action = await ledger.get_action("hitl-action")
        run = await ledger.get_run("hitl-run")
        incidents = await ledger.list_incidents("hitl-run", open_only=True)
    assert isinstance(
        ActionOutcomeEnvelope.model_validate_json(original.canonical_outcome_json).outcome,
        Paused,
    )
    assert action.status is ActionStatus.REPAIR_REQUIRED
    assert action.repair_class == "semantic"
    assert run.status is RunStatus.RUNNING
    assert incidents[-1].reason_code == "term_drift"


@pytest.mark.asyncio
async def test_effective_hitl_success_uses_gate_and_commit_authority(
    tmp_path: Path,
) -> None:
    """Catch success routing that gates the immutable original Paused receipt."""
    manifest = ExpectedArtifactManifest(
        action_id="hitl-action",
        entries=(
            ExpectedArtifact(
                canonical_relpath="source/source_manifest.json",
                media_type="application/json",
                evidence_role="source_manifest",
            ),
            ExpectedArtifact(
                canonical_relpath="source/source_text.txt",
                media_type="text/plain",
                evidence_role="source_text",
            ),
        ),
    )
    retry = RetryPolicySpec(max_attempts=1)
    authorized = AuthorizedAction(
        action_id="hitl-action",
        proposal_id="review",
        plan_version=1,
        capability="source.ingest",
        parameters_json='{"source_relpath":"source/source_text_raw.txt"}',
        write_set=("source", "metadata"),
        idempotency_key="action:hitl-action",
        expected_artifact_manifest=manifest,
        expected_artifact_manifest_digest=sha256_canonical_json(canonical_manifest_json(manifest)),
        expected_evidence_refs=("source_manifest", "source_text"),
        retry_policy=retry,
        retry_policy_fingerprint=sha256_canonical_json(canonical_model_json(retry)),
    )
    project_root = await _seed_hitl_pause(tmp_path, authorized_action=authorized)
    decision_type = lifecycle.InterruptDecisionRequest

    class _Continuation:
        async def resume_hitl(self, request):  # type: ignore[no-untyped-def]
            store = ArtifactStore(BookProject(project_root), None)
            try:
                writer = store.writer(request.action_id, request.attempt)
                writer.write_text(
                    "source/source_manifest.json",
                    '{"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
                    media_type="application/json",
                    evidence_role="source_manifest",
                )
                writer.write_text(
                    "source/source_text.txt",
                    "approved clean source",
                    media_type="text/plain",
                    evidence_role="source_text",
                )
                bundle = writer.artifact_bundle()
            finally:
                store.close()
            return ActionOutcomeEnvelope(
                action_id=request.action_id,
                attempt=request.attempt,
                outcome=Succeeded(
                    artifact_bundle=bundle,
                    evidence_refs=(
                        "source/source_manifest.json",
                        "source/source_text.txt",
                    ),
                ),
            )

    await lifecycle.approve_interrupt(
        project_root=project_root,
        request=decision_type(
            run_id="hitl-run",
            action_id="hitl-action",
            attempt=1,
            interrupt_id="public-interrupt-1",
            decisions=("approve",),
            feedback=(None,),
        ),
        continuation=_Continuation(),
    )
    async with RunLedger.open(project_root / "state/run.db") as ledger:
        original = await ledger.get_attempt_outcome("hitl-action", 1)
        action = await ledger.get_action("hitl-action")
        gate, intents = await ledger.get_gate_receipt_and_intents("hitl-action", 1)
    assert isinstance(
        ActionOutcomeEnvelope.model_validate_json(original.canonical_outcome_json).outcome,
        Paused,
    )
    assert action.status is ActionStatus.SUCCEEDED
    assert gate.bundle_digest
    assert all(intent.status == "COMMITTED" for intent in intents)
    assert (project_root / "source/source_text.txt").read_text() == "approved clean source"


@pytest.mark.asyncio
async def test_default_hitl_continuation_reconstructs_exact_action_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch default approve paths that cannot reach Task 6 without test injection."""
    manifest = ExpectedArtifactManifest(
        action_id="hitl-action",
        entries=(
            ExpectedArtifact(
                canonical_relpath="qa/benchmark/global_research_ack.md",
                media_type="text/markdown",
                evidence_role="research",
            ),
        ),
    )
    retry = RetryPolicySpec(max_attempts=1)
    authorized = AuthorizedAction(
        action_id="hitl-action",
        proposal_id="review",
        plan_version=1,
        capability="research.global",
        parameters_json='{"focus":""}',
        read_set=("references",),
        write_set=("qa/benchmark",),
        idempotency_key="action:hitl-action",
        expected_artifact_manifest=manifest,
        expected_artifact_manifest_digest=sha256_canonical_json(canonical_manifest_json(manifest)),
        expected_evidence_refs=("qa/benchmark",),
        retry_policy=retry,
        retry_policy_fingerprint=sha256_canonical_json(canonical_model_json(retry)),
    )
    project_root = await _seed_hitl_pause(tmp_path, authorized_action=authorized)
    seen: list[object] = []

    class _Agent:
        async def run_action(self, request):  # type: ignore[no-untyped-def]
            seen.append(request)
            return AgentRunResult(
                outcome=PermanentFailure(
                    error_code="operator_rejected",
                    message="default continuation reached Task 6",
                ),
                llm_calls=0,
                tool_calls=0,
                cost_usd=0,
                stopped_reason="completed",
            )

        async def inspect_hitl_checkpoint(self, request):  # type: ignore[no-untyped-def]
            raise AssertionError("new claim must resume, not inspect")

    services = SimpleNamespace(
        router=object(),
        agent=_Agent(),
        total_cost=0.0,
        flush=lambda: None,
    )
    monkeypatch.setattr("abi.providers.services.build_run_services", lambda **kwargs: services)
    decision_type = lifecycle.InterruptDecisionRequest
    await lifecycle.approve_interrupt(
        project_root=project_root,
        request=decision_type(
            run_id="hitl-run",
            action_id="hitl-action",
            attempt=1,
            interrupt_id="public-interrupt-1",
            decisions=("reject",),
            feedback=("operator denied",),
        ),
        config=RunConfig(),
    )

    assert len(seen) == 1
    agent_request = seen[0]
    assert agent_request.thread_id == "hitl-run/hitl-action/1"
    assert agent_request.checkpoint_path == BookProject(project_root).graph_checkpoints
    assert agent_request.resume.interrupts[0].interrupt_id == "public-interrupt-1"
    assert agent_request.resume.interrupts[0].decisions[0].decision == "reject"
    assert agent_request.resume.interrupts[0].decisions[0].feedback == "operator denied"
    assert {tool.name for tool in agent_request.tools} == {"read_file", "write_file", "grep"}
