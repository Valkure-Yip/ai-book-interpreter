"""Receipt-driven reconciliation before every business planning cycle."""

from __future__ import annotations

import json

from abi.actions.registry import ActionRegistry
from abi.orchestrator.committer import Committer, CommitValidationError
from abi.project.artifacts import ArtifactConflictError, ArtifactStore
from abi.project.run_ledger import (
    ActionRecord,
    LedgerError,
    LedgerNotFoundError,
    LedgerTransitionError,
    ProbeResolutionRequest,
    RunLedger,
)
from abi.types.orchestration import (
    ActionOutcomeEnvelope,
    ActionStatus,
    Indeterminate,
    Paused,
    PermanentFailure,
    ProbeActionInput,
    ProbeResolution,
    RepairRequired,
    RetryableFailure,
    RunSnapshot,
    RunStatus,
    Succeeded,
)


class Reconciler:
    """Recover only from ledger receipts, frozen manifests, and artifact facts."""

    def __init__(
        self,
        *,
        ledger: RunLedger,
        artifacts: ArtifactStore,
        registry: ActionRegistry,
        committer: Committer,
    ) -> None:
        self._ledger = ledger
        self._artifacts = artifacts
        self._registry = registry
        self._committer = committer

    async def reconcile(self, run_id: str) -> RunSnapshot:
        """Reconcile post-success drift and every RUNNING attempt without reexecution."""
        actions = {action.action_id: action for action in await self._ledger.list_actions(run_id)}
        for attempt in await self._ledger.running_attempts(run_id):
            action = actions[attempt.action_id]
            try:
                receipt = await self._ledger.get_effective_attempt_outcome(
                    action.action_id, attempt.attempt
                )
            except LedgerNotFoundError:
                try:
                    await self._artifacts.rebuild_outcome_receipt(action.action_id, attempt.attempt)
                    receipt = await self._ledger.get_effective_attempt_outcome(
                        action.action_id, attempt.attempt
                    )
                except ArtifactConflictError:
                    continue
            envelope = ActionOutcomeEnvelope.model_validate_json(receipt.canonical_outcome_json)
            await self._route_receipt(run_id, action, envelope)

        if (await self._ledger.get_run(run_id)).status is RunStatus.BLOCKED:
            return await self._ledger.load_snapshot(run_id)

        # A crash may occur after an authorized replacement atomically renames the
        # canonical file but before its intent/artifact transaction commits. Finish
        # RUNNING receipts first so the predecessor is durably superseded before
        # ordinary post-success drift verification examines the current generation.
        refreshed_actions = tuple(await self._ledger.list_actions(run_id))
        snapshot = await self._ledger.load_snapshot(run_id)
        current_action_ids = {
            action.action_id for action in snapshot.actions if action.outputs_current
        }
        await self._verify_prior_successes(
            run_id, refreshed_actions, current_action_ids
        )
        if (await self._ledger.get_run(run_id)).status is RunStatus.BLOCKED:
            return await self._ledger.load_snapshot(run_id)

        await self._project_reconciled_intents(run_id)
        return await self._ledger.load_snapshot(run_id)

    async def _verify_prior_successes(
        self,
        run_id: str,
        actions: tuple[ActionRecord, ...],
        current_action_ids: set[str],
    ) -> None:
        for action in actions:
            if (
                action.status is not ActionStatus.SUCCEEDED
                or action.action_id not in current_action_ids
            ):
                continue
            attempts = await self._ledger.attempt_numbers(action.action_id)
            if not attempts:
                await self._ledger.set_run_status(run_id, RunStatus.BLOCKED)
                continue
            try:
                resolution = await self._ledger.get_probe_resolution(action.action_id, attempts[-1])
            except LedgerNotFoundError:
                pass
            else:
                if resolution.disposition == "succeeded":
                    continue
            try:
                await self._ledger.get_probe_resolution_for_probe(action.action_id, attempts[-1])
            except LedgerNotFoundError:
                pass
            else:
                continue
            try:
                await self._artifacts.verify_committed_bundle(action.action_id, attempts[-1])
            except ArtifactConflictError:
                continue

    async def _route_receipt(
        self,
        run_id: str,
        action: ActionRecord,
        envelope: ActionOutcomeEnvelope,
    ) -> None:
        attempt = envelope.attempt
        outcome = envelope.outcome
        if isinstance(outcome, Succeeded):
            try:
                await self._ledger.get_gate_receipt_and_intents(action.action_id, attempt)
            except LedgerNotFoundError:
                try:
                    await self._committer.resume_success(
                        run_id=run_id, action=action, attempt=attempt
                    )
                except CommitValidationError as exc:
                    mapped = self._registry.has_semantic_repair(exc.decision.reason_code)
                    await self._ledger.record_repair_required(
                        action_id=action.action_id,
                        attempt=attempt,
                        repair_class="semantic",
                        repair_source="validator",
                        reason_code=exc.decision.reason_code,
                        defect_codes=(exc.decision.reason_code,),
                        evidence_refs=exc.decision.evidence_refs,
                        message=exc.decision.message,
                        semantic_reason_mapped=mapped,
                        validator_decision=exc.decision,
                    )
                    return
                except ArtifactConflictError:
                    return
            else:
                try:
                    await self._committer.finalize_from_gate(
                        run_id=run_id, action=action, attempt=attempt
                    )
                except ArtifactConflictError:
                    return
            return
        if isinstance(outcome, RetryableFailure):
            try:
                await self._ledger.route_retry_from_receipt(action.action_id, attempt=attempt)
            except LedgerTransitionError:
                # A retryable receipt at the frozen attempt limit is a durable
                # terminal failure, not an indefinitely RUNNING attempt. Close the
                # attempt and Action before blocking the run so resume/reconciliation
                # observes one self-consistent state.
                await self._ledger.finish_attempt(
                    action.action_id,
                    attempt=attempt,
                    status=ActionStatus.PERMANENT_FAILED,
                )
                # A semantic replacement generation may fail operationally even
                # after its internal retries are spent. Keep the original semantic
                # incident open and let bounded replanning authorize another
                # generation; the controller's semantic-repair limit is the outer
                # non-progress guard.
                if await self._ledger.semantic_repair_incident_ids(action.action_id):
                    return
                await self._ledger.record_incident(
                    run_id,
                    error_code="retry_exhausted",
                    message=(
                        f"{action.capability} is not eligible for another frozen-policy attempt; "
                        "inspect the receipt and authorize an alternative explicitly"
                    ),
                    action_id=action.action_id,
                )
                await self._ledger.set_run_status(run_id, RunStatus.BLOCKED)
            return
        if isinstance(outcome, RepairRequired):
            mapped = outcome.repair_class == "semantic" and self._registry.has_semantic_repair(
                outcome.reason_code
            )
            await self._ledger.record_repair_required(
                action_id=action.action_id,
                attempt=attempt,
                repair_class=outcome.repair_class,
                repair_source=outcome.repair_source,
                reason_code=outcome.reason_code,
                defect_codes=outcome.defect_codes,
                evidence_refs=tuple(getattr(outcome, "evidence_refs", ())),
                message=outcome.message,
                semantic_reason_mapped=mapped,
            )
            return
        if isinstance(outcome, PermanentFailure):
            await self._ledger.finish_attempt(
                action.action_id,
                attempt=attempt,
                status=ActionStatus.PERMANENT_FAILED,
            )
            await self._ledger.record_incident(
                run_id,
                error_code=outcome.error_code,
                message=outcome.message,
                action_id=action.action_id,
            )
            await self._ledger.set_run_status(run_id, RunStatus.BLOCKED)
            return
        if isinstance(outcome, Indeterminate):
            await self._ledger.finish_attempt(
                action.action_id,
                attempt=attempt,
                status=ActionStatus.INDETERMINATE,
                failure_signature=outcome.failure_signature,
            )
            probe = self._registry.get(action.capability).spec.probe_capability
            if probe is None or not self._registry.contains(probe):
                await self._ledger.record_incident(
                    run_id,
                    error_code="indeterminate_probe_missing",
                    message=(
                        f"{action.capability} has an indeterminate side effect but no registered "
                        "probe; register a read-only evidence-only probe before resuming"
                    ),
                    action_id=action.action_id,
                )
                await self._ledger.set_run_status(run_id, RunStatus.BLOCKED)
            return
        if isinstance(outcome, Paused):
            await self._ledger.finish_attempt(
                action.action_id,
                attempt=attempt,
                status=ActionStatus.PAUSED,
            )
            await self._ledger.set_run_status(
                run_id,
                RunStatus.PAUSED_BUDGET if outcome.reason == "budget" else RunStatus.PAUSED_HITL,
            )
            return
        if isinstance(outcome, ProbeResolution):
            try:
                binding = ProbeActionInput.model_validate_json(action.parameters_json)
                original = await self._ledger.get_action(binding.original_action_id)
                if (
                    original.run_id != run_id
                    or not self._registry.contains(original.capability)
                    or self._registry.get(original.capability).spec.probe_capability
                    != action.capability
                ):
                    raise LedgerTransitionError(
                        "probe capability is not the original ActionSpec binding"
                    )
                await self._ledger.resolve_indeterminate(
                    ProbeResolutionRequest(
                        probe_action_id=action.action_id,
                        probe_attempt=attempt,
                        original_action_id=binding.original_action_id,
                        original_attempt=binding.original_attempt,
                        operation_key=binding.operation_key,
                        original_idempotency_key=original.idempotency_key,
                        retry_policy_fingerprint=original.retry_policy_fingerprint,
                    )
                )
            except (LedgerError, TypeError, ValueError):
                await self._ledger.mark_bundle_conflict(
                    action.action_id,
                    attempt,
                    reason_code="probe_resolution_conflict",
                    message=(
                        "Probe resolution differs from its original Action/attempt/operation "
                        "binding; preserve both receipts and block."
                    ),
                )
            return
        await self._ledger.mark_bundle_conflict(
            action.action_id,
            attempt,
            reason_code="probe_resolution_conflict",
            message="Unknown outcome discriminant reached reconciliation.",
        )

    async def _project_reconciled_intents(self, run_id: str) -> None:
        for intent in await self._ledger.promotion_intents(run_id):
            if intent.status not in {"COMMITTED", "CONFLICT"}:
                continue
            await self._ledger.record_event(
                run_id=run_id,
                event_name="action.reconciled",
                aggregate_id=intent.action_id,
                payload_json=json.dumps(
                    {
                        "action_id": intent.action_id,
                        "attempt": intent.attempt,
                        "intent_id": intent.intent_id,
                        "promotion_status": intent.status,
                    },
                    sort_keys=True,
                ),
                idempotency_key=(f"action.reconciled:{intent.intent_id}:{intent.status.lower()}"),
            )
