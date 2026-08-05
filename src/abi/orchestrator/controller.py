"""Business control plane for constrained dynamic Action orchestration."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Protocol

from abi.actions.registry import ActionRegistry
from abi.orchestrator.committer import Committer
from abi.orchestrator.dispatcher import Dispatcher
from abi.orchestrator.projector import OutboxProjector
from abi.orchestrator.reconcile import Reconciler
from abi.planning.context import SnapshotBuilder
from abi.planning.policy import PolicyEngine
from abi.planning.scheduler import Scheduler
from abi.project.run_ledger import ActionRecord, LedgerClaimConflict, RunLedger
from abi.types.orchestration import (
    ActionArgument,
    ActionOutcomeEnvelope,
    ActionStatus,
    Indeterminate,
    PlanningContext,
    PlanPatch,
    ProbeActionInput,
    ProposedAction,
    RunSnapshot,
    RunStatus,
    canonical_model_json,
    sha256_canonical_json,
)


class PlanProposer(Protocol):
    """Proposal-only planner ABI consumed by the controller."""

    async def plan(self, context: PlanningContext) -> PlanPatch: ...


CompletionPredicate = Callable[[RunSnapshot], bool]
ControllerHook = Callable[[str, object], None]


class DynamicController:
    """Reconcile, plan, authorize, dispatch, validate, and commit one business cycle."""

    def __init__(
        self,
        *,
        ledger: RunLedger,
        registry: ActionRegistry,
        planner: PlanProposer,
        policy: PolicyEngine,
        snapshots: SnapshotBuilder,
        scheduler: Scheduler,
        dispatcher: Dispatcher,
        committer: Committer,
        reconciler: Reconciler,
        projector: OutboxProjector,
        complete_when: CompletionPredicate,
        test_hook: ControllerHook | None = None,
    ) -> None:
        self._ledger = ledger
        self._registry = registry
        self._planner = planner
        self._policy = policy
        self._snapshots = snapshots
        self._scheduler = scheduler
        self._dispatcher = dispatcher
        self._committer = committer
        self._reconciler = reconciler
        self._projector = projector
        self._complete_when = complete_when
        self._test_hook = test_hook

    async def tick(self, run_id: str) -> bool:
        """Perform one recoverable cycle and return whether the runtime should continue."""
        snapshot = await self._reconciler.reconcile(run_id)
        self._invoke_hook("after_reconcile", snapshot)
        await self._projector.flush(run_id)
        if snapshot.status is not RunStatus.RUNNING:
            return False
        context = await self._snapshots.build(run_id)
        snapshot = context.policy_snapshot

        await self._reserve_due_retries(run_id)
        await self._authorize_pending_probes(run_id)
        context = await self._snapshots.build(run_id)
        snapshot = context.policy_snapshot
        candidates = tuple(
            action
            for action in await self._ledger.list_actions(run_id)
            if action.status is ActionStatus.AUTHORIZED
        )
        batch = self._select_batch(candidates, snapshot)
        if not batch:
            if self._complete_when(snapshot):
                await self._complete(run_id)
                await self._projector.flush(run_id)
                return False
            if any(
                action.status is ActionStatus.RUNNING
                for action in await self._ledger.list_actions(run_id)
            ):
                await self._projector.flush(run_id)
                return True
            plan = await self._ledger.pending_plan(run_id)
            authorization_snapshot = context.policy_snapshot
            if plan is None:
                self._invoke_hook("before_planner", context)
                patch = await self._planner.plan(context)
                plan = await self._ledger.append_plan(run_id, patch)
                self._invoke_hook("after_plan_append", plan)
            else:
                patch = plan.patch
                authorization_snapshot = context.policy_snapshot.model_copy(
                    update={"plan_version": plan.version - 1}
                )
            decision = self._policy.authorize(
                authorization_snapshot, patch, next_plan_version=plan.version
            )
            if not decision.authorized:
                await self._ledger.record_plan_rejection(
                    run_id,
                    plan_version=plan.version,
                    reason_codes=decision.reason_codes,
                )
                await self._projector.flush(run_id)
                return True
            authorized = await self._ledger.authorize_actions(run_id, decision.actions)
            self._invoke_hook("after_authorization", authorized)
            current = (await self._snapshots.build(run_id)).policy_snapshot
            snapshot = current
            batch = self._select_batch(authorized, current)
            if not batch:
                await self._ledger.record_incident(
                    run_id,
                    error_code="authorized_batch_not_runnable",
                    message=(
                        "Policy authorized no currently runnable Action; repair eligibility, "
                        "dependencies, or conflict declarations before replanning"
                    ),
                )
                await self._block(run_id, "authorized_batch_not_runnable")
                await self._projector.flush(run_id)
                return False

        dispatchable: list[tuple[ActionRecord, int]] = []
        for action in batch:
            attempt = await self._authorized_attempt(action)
            if attempt is not None:
                dispatchable.append((action, attempt))
        if not dispatchable:
            await self._projector.flush(run_id)
            return (await self._ledger.get_run(run_id)).status is RunStatus.RUNNING
        results = await asyncio.gather(
            *(
                self._dispatch_one(
                    run_id=run_id,
                    action=action,
                    snapshot=snapshot,
                    attempt=attempt,
                )
                for action, attempt in dispatchable
            )
        )
        if not any(result is not None for result in results):
            await self._projector.flush(run_id)
            return (await self._ledger.get_run(run_id)).status is RunStatus.RUNNING
        # Executor return values are never a business authority. Dispatcher persists
        # an immutable receipt before returning; only reconciliation may classify it.
        current = await self._reconciler.reconcile(run_id)
        self._invoke_hook("after_reconcile", current)
        keep_running = current.status is RunStatus.RUNNING
        if current.status is RunStatus.RUNNING and self._complete_when(current):
            await self._complete(run_id)
            keep_running = False
        elif current.status is not RunStatus.RUNNING:
            keep_running = False
        await self._projector.flush(run_id)
        return keep_running

    async def _reserve_due_retries(self, run_id: str) -> None:
        """Turn each durable RETRY_WAIT into exactly one authorized successor."""
        for action in await self._ledger.list_actions(run_id):
            if action.status is not ActionStatus.RETRY_WAIT:
                continue
            attempts = await self._ledger.attempt_numbers(action.action_id)
            if not attempts:
                await self._ledger.record_incident(
                    run_id,
                    error_code="retry_attempt_missing",
                    message="RETRY_WAIT action has no durable predecessor attempt",
                    action_id=action.action_id,
                )
                await self._block(run_id, "retry_attempt_missing")
                return
            self._invoke_hook("before_create_next_attempt", action)
            successor = await self._ledger.create_next_attempt(
                action.action_id, previous_attempt=attempts[-1]
            )
            self._invoke_hook("after_create_next_attempt", successor)

    async def _dispatch_one(
        self,
        *,
        run_id: str,
        action: ActionRecord,
        snapshot: RunSnapshot,
        attempt: int,
    ) -> object | None:
        self._invoke_hook("before_dispatch", (action, attempt))
        try:
            return await self._dispatcher.execute(
                run_id=run_id,
                action=action,
                snapshot=snapshot,
                attempt=attempt,
            )
        except LedgerClaimConflict:
            return None

    async def _authorized_attempt(self, action: ActionRecord) -> int | None:
        """Resolve the one explicitly authorized attempt for dispatch."""
        attempts = await self._ledger.attempt_numbers(action.action_id)
        if not attempts:
            return 1
        attempt = await self._ledger.get_attempt(action.action_id, attempts[-1])
        if attempt.status is not ActionStatus.AUTHORIZED:
            return None
        return attempt.attempt

    async def _authorize_pending_probes(self, run_id: str) -> None:
        """Create the one typed evidence-only probe bound to each indeterminate attempt."""
        actions = await self._ledger.list_actions(run_id)
        for original in actions:
            if original.status is not ActionStatus.INDETERMINATE:
                continue
            probe_capability = self._registry.get(
                original.capability
            ).spec.probe_capability
            if probe_capability is None:
                continue
            attempts = await self._ledger.attempt_numbers(original.action_id)
            if not attempts:
                continue
            original_attempt = attempts[-1]
            receipt = await self._ledger.get_attempt_outcome(
                original.action_id, original_attempt
            )
            envelope = ActionOutcomeEnvelope.model_validate_json(
                receipt.canonical_outcome_json
            )
            if not isinstance(envelope.outcome, Indeterminate):
                continue
            binding = ProbeActionInput(
                original_action_id=original.action_id,
                original_attempt=original_attempt,
                operation_key=envelope.outcome.operation_key,
                probe_capability=probe_capability,
            )
            binding_parameters_json = binding.model_dump_json()
            if any(
                action.capability == probe_capability
                and action.parameters_json == binding_parameters_json
                for action in actions
            ):
                continue
            context = await self._snapshots.build(run_id)
            binding_digest = sha256_canonical_json(canonical_model_json(binding))[:16]
            proposal_id = (
                f"probe-{original.action_id}-{original_attempt}-{binding_digest}"
            )
            patch = PlanPatch(
                objective=f"probe indeterminate operation {binding.operation_key}",
                proposed_actions=(
                    ProposedAction(
                        proposal_id=proposal_id,
                        capability=probe_capability,
                        arguments=tuple(
                            ActionArgument(
                                name=name,
                                value_json=json.dumps(value, sort_keys=True),
                            )
                            for name, value in binding.model_dump().items()
                        ),
                    ),
                ),
                rationale=(
                    "Only the registered read-only probe may inspect an indeterminate "
                    "external operation."
                ),
            )
            decision = self._policy.authorize(
                context.policy_snapshot,
                patch,
                next_plan_version=context.policy_snapshot.plan_version + 1,
            )
            if not decision.authorized or len(decision.actions) != 1:
                await self._ledger.record_incident(
                    run_id,
                    error_code="indeterminate_probe_invalid",
                    message=(
                        f"registered probe {probe_capability} was not authorizable; "
                        "repair its exact ProbeActionInput contract"
                    ),
                    action_id=original.action_id,
                )
                await self._block(run_id, "indeterminate_probe_invalid")
                return
            await self._ledger.authorize_probe_action(
                run_id,
                binding=binding,
                patch=patch,
                action=decision.actions[0],
                expected_previous_plan_version=context.policy_snapshot.plan_version,
            )
            actions = await self._ledger.list_actions(run_id)

    async def on_cycles_exhausted(self, run_id: str, max_cycles: int) -> None:
        """Convert runtime exhaustion into an explicit recoverable business block."""
        run = await self._ledger.get_run(run_id)
        if run.status is not RunStatus.RUNNING:
            return
        await self._ledger.record_incident(
            run_id,
            error_code="controller_max_cycles_exhausted",
            message=(
                f"Controller exhausted {max_cycles} cycles; inspect repeated plans or evidence "
                "and resume after repairing the loop"
            ),
        )
        await self._block(run_id, "controller_max_cycles_exhausted")
        await self._projector.flush(run_id)

    def _select_batch(
        self, candidates: tuple[ActionRecord, ...], snapshot: RunSnapshot
    ) -> tuple[ActionRecord, ...]:
        committed = frozenset(
            action.action_id
            for action in snapshot.actions
            if action.status is ActionStatus.SUCCEEDED
        )
        eligible = frozenset(action.capability for action in snapshot.eligible_actions)
        return self._scheduler.select_batch(
            candidates,
            committed_action_ids=committed,
            eligible_capabilities=eligible,
        )

    async def _complete(self, run_id: str) -> None:
        await self._ledger.set_run_status(run_id, RunStatus.COMPLETED)

    async def _block(self, run_id: str, reason: str) -> None:
        await self._ledger.set_run_status(run_id, RunStatus.BLOCKED)

    def _invoke_hook(self, point: str, detail: object) -> None:
        if self._test_hook is not None:
            self._test_hook(point, detail)
