"""L1 process-conformance reporting from durable dynamic-run facts."""

from __future__ import annotations

import json
from typing import Any

from abi.eval.run_facts import EvalRunFacts, load_eval_run_facts
from abi.project.layout import BookProject
from abi.project.run_ledger import ActionAttemptRecord
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionOutcomeEnvelope,
    ActionStatus,
    ArtifactMetadata,
    GateDecision,
    Indeterminate,
    Paused,
    ProbeResolution,
    RetryableFailure,
    RunStatus,
    Succeeded,
    canonical_bundle_json,
    canonical_manifest_json,
    canonical_model_json,
    sha256_canonical_json,
)


class _ArtifactMetadataList(FrozenModel):
    items: tuple[ArtifactMetadata, ...]


class GateIntegrityItem(FrozenModel):
    gate: str
    produces: str
    recorded: str
    replay_ok: bool
    replay_reason: str
    consistent: bool
    verifiable: bool


class TraceReport(FrozenModel):
    book: str
    status: str
    is_done: bool
    blocked_reason: str | None
    gate_integrity: list[GateIntegrityItem]
    gate_integrity_ok: bool
    reached_states_ok: bool
    reached_states_failures: list[str]
    path_conformance_ok: bool
    skipped_states: list[str]
    cost_usd: float | None
    tokens_in: int | None
    tokens_out: int | None
    duration_s: int | None
    llm_calls: int | None
    stage_attempts: dict[str, int]
    first_pass_rate: float | None
    recursion_caps: int
    budget_stops: int
    verdict: str


def _read_metrics(project: BookProject) -> dict[str, Any] | None:
    path = project.root / "metrics.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_events(project: BookProject) -> tuple[dict[str, int], float | None, int, int]:
    path = project.root / "events.jsonl"
    attempts: dict[str, int] = {}
    capped = 0
    budget_stops = 0
    if not path.exists():
        return attempts, None, capped, budget_stops
    completed = 0
    first_pass = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        name = event.get("event")
        if name in {"action.finished", "stage.end"}:
            action = str(event.get("action_id", event.get("stage", "?")))
            count = int(event.get("attempt", event.get("attempts", 0)) or 0)
            attempts[action] = count
            completed += 1
            if count == 1 and event.get("ok", True):
                first_pass += 1
        elif name == "agent.run.capped":
            capped += 1
        elif name in {"run.paused_budget", "pipeline.budget"}:
            budget_stops += 1
    rate = first_pass / completed if completed else None
    return attempts, rate, capped, budget_stops


def trace_project(project: BookProject, *, facts: EvalRunFacts | None = None) -> TraceReport:
    """Build L1 evidence from one exact RunLedger run and rebuildable projections."""
    durable = facts or load_eval_run_facts(project)
    run = durable.run
    actions = {action.action_id: action for action in durable.actions}
    attempts = {(item.action_id, item.attempt): item for item in durable.attempts}
    outcomes = {
        (item.action_id, item.attempt): item for item in durable.outcome_receipts
    }
    effective_outcomes = {
        (item.action_id, item.attempt): item for item in durable.effective_outcomes
    }
    intents_by_attempt = {
        key: tuple(
            sorted(
                (
                    item
                    for item in durable.promotion_intents
                    if (item.action_id, item.attempt) == key
                ),
                key=lambda item: item.ordinal,
            )
        )
        for key in {(item.action_id, item.attempt) for item in durable.promotion_intents}
    }
    integrity: list[GateIntegrityItem] = []
    policy_failures: list[str] = []
    parsed_outcomes: dict[tuple[str, int], ActionOutcomeEnvelope] = {}
    for key, outcome_receipt in outcomes.items():
        try:
            parsed_envelope = ActionOutcomeEnvelope.model_validate_json(
                outcome_receipt.canonical_outcome_json
            )
        except ValueError:
            policy_failures.append(f"{key[0]} outcome receipt is not typed canonical JSON")
            continue
        if (
            outcome_receipt.outcome_digest
            != sha256_canonical_json(outcome_receipt.canonical_outcome_json)
            or parsed_envelope.action_id != key[0]
            or parsed_envelope.attempt != key[1]
        ):
            policy_failures.append(
                f"{key[0]} outcome receipt digest or envelope identity disagrees"
            )
            continue
        parsed_outcomes[key] = parsed_envelope
    gate_receipts_by_attempt = {
        key: tuple(
            item
            for item in durable.gate_receipts
            if (item.action_id, item.attempt) == key
        )
        for key in {(item.action_id, item.attempt) for item in durable.gate_receipts}
    }
    effective_success_keys: set[tuple[str, int]] = set()
    for key, success_attempt in attempts.items():
        action = actions.get(key[0])
        effective_record = effective_outcomes.get(key)
        canonical_outcome_json = (
            effective_record.canonical_outcome_json
            if effective_record is not None
            else outcomes[key].canonical_outcome_json
            if key in outcomes
            else None
        )
        if canonical_outcome_json is None:
            continue
        try:
            effective_envelope = ActionOutcomeEnvelope.model_validate_json(
                canonical_outcome_json
            )
        except ValueError:
            continue
        if (
            isinstance(effective_envelope.outcome, Succeeded)
            and action is not None
            and action.status is ActionStatus.SUCCEEDED
            and success_attempt.status is ActionStatus.SUCCEEDED
        ):
            effective_success_keys.add(key)
    for key in effective_success_keys:
        if len(gate_receipts_by_attempt.get(key, ())) != 1:
            policy_failures.append(
                f"{key[0]} ordinary effective success requires exactly one gate receipt chain"
            )
    for receipt in durable.gate_receipts:
        key = (receipt.action_id, receipt.attempt)
        failures: list[str] = []
        action = actions.get(receipt.action_id)
        attempt = attempts.get(key)
        gate_outcome_receipt = outcomes.get(key)
        effective_outcome = effective_outcomes.get(key)
        try:
            decision = GateDecision.model_validate_json(
                receipt.canonical_gate_decision_json
            )
        except ValueError:
            decision = None
            failures.append("gate decision is not canonical typed evidence")
        if action is None or attempt is None:
            failures.append("authorization or attempt fact is missing")
        elif (
            action.expected_artifact_manifest != attempt.expected_artifact_manifest
            or action.expected_manifest_digest != attempt.expected_manifest_digest
            or action.expected_manifest_digest
            != sha256_canonical_json(
                canonical_manifest_json(action.expected_artifact_manifest)
            )
        ):
            failures.append("authorization and attempt expected manifest facts disagree")
        bundle = None
        if gate_outcome_receipt is None:
            failures.append("attempt outcome receipt is missing")
        else:
            try:
                envelope = (
                    ActionOutcomeEnvelope.model_validate_json(
                        effective_outcome.canonical_outcome_json
                    )
                    if effective_outcome is not None
                    else parsed_outcomes.get(key)
                )
            except ValueError:
                envelope = None
            if envelope is None or not isinstance(envelope.outcome, Succeeded):
                failures.append("ordinary gate is not backed by a succeeded outcome receipt")
            else:
                bundle = envelope.outcome.artifact_bundle
                bundle_json = canonical_bundle_json(bundle)
                recorded_bundle_json = (
                    bundle_json
                    if effective_outcome is not None
                    and effective_outcome.source == "hitl_continuation"
                    else gate_outcome_receipt.canonical_bundle_json
                )
                recorded_bundle_digest = (
                    sha256_canonical_json(bundle_json)
                    if effective_outcome is not None
                    and effective_outcome.source == "hitl_continuation"
                    else gate_outcome_receipt.bundle_digest
                )
                if (
                    recorded_bundle_json != bundle_json
                    or recorded_bundle_digest != sha256_canonical_json(bundle_json)
                    or receipt.bundle_digest != recorded_bundle_digest
                ):
                    failures.append("canonical bundle digest binding disagrees")
        if decision is not None and (
            not decision.passed
            or receipt.gate_decision_digest
            != sha256_canonical_json(canonical_model_json(decision))
            or decision.validator_id != receipt.validator_id
            or decision.validator_version != receipt.validator_version
            or decision.bundle_digest != receipt.bundle_digest
            or decision.artifact_checksums
            != tuple(item.checksum for item in receipt.artifacts)
            or decision.evidence_refs != receipt.evidence_refs
        ):
            failures.append("gate decision validator/checksum/evidence binding disagrees")
        if bundle is not None and action is not None:
            actual = tuple(
                (
                    item.canonical_relpath,
                    item.media_type,
                    item.evidence_role,
                    item.metadata,
                )
                for item in bundle.entries
            )
            expected = tuple(
                (
                    item.canonical_relpath,
                    item.media_type,
                    item.evidence_role,
                    item.metadata,
                )
                for item in action.expected_artifact_manifest.entries
            )
            if not actual or actual != expected:
                failures.append("ordinary success bundle is empty or not the exact manifest")
        intents = intents_by_attempt.get(key, ())
        exact_artifact_identity = bool(
            bundle is not None
            and len(bundle.entries) == len(receipt.artifacts) == len(intents)
            and all(
                intent.ordinal == ordinal
                and intent.action_id == receipt.action_id
                and intent.attempt == receipt.attempt
                and intent.staged_relpath == gate_artifact.staged_relpath
                == bundle_entry.staged_relpath
                and intent.canonical_relpath == gate_artifact.canonical_relpath
                == bundle_entry.canonical_relpath
                and intent.checksum == gate_artifact.checksum
                and intent.media_type == bundle_entry.media_type
                and intent.evidence_role == bundle_entry.evidence_role
                and intent.metadata_json
                == canonical_model_json(_ArtifactMetadataList(items=bundle_entry.metadata))
                for ordinal, (bundle_entry, gate_artifact, intent) in enumerate(
                    zip(bundle.entries, receipt.artifacts, intents, strict=True)
                )
            )
        )
        if not exact_artifact_identity:
            failures.append(
                "bundle, gate receipt, and promotion intent artifact identity disagrees"
            )
            policy_failures.append(
                f"{receipt.action_id} exact artifact identity replay failed"
            )
        if (
            len(intents) != len(receipt.artifacts)
            or tuple(intent.ordinal for intent in intents) != tuple(range(len(intents)))
            or tuple(intent.checksum for intent in intents)
            != tuple(item.checksum for item in receipt.artifacts)
            or any(intent.bundle_digest != receipt.bundle_digest for intent in intents)
            or any(intent.created_at != receipt.recorded_at for intent in intents)
        ):
            failures.append("complete ordered intent set was not created with the gate receipt")
            policy_failures.append(
                f"{receipt.action_id} ordinary success promotion intent set was missing or late"
            )
        committed_artifacts = tuple(
            sorted(
                (
                    item.relpath,
                    item.sha256,
                    item.producer_action_id,
                )
                for item in durable.snapshot.artifacts
                if item.producer_action_id == receipt.action_id
            )
        )
        intended_artifacts = tuple(
            sorted(
                (item.canonical_relpath, item.checksum, item.action_id) for item in intents
            )
        )
        if (
            not intents
            or any(item.status != "COMMITTED" or item.committed_at is None for item in intents)
            or committed_artifacts != intended_artifacts
            or action is None
            or action.status is not ActionStatus.SUCCEEDED
            or action.committed_at is None
            or any(
                item.committed_at is not None
                and action.committed_at is not None
                and item.committed_at > action.committed_at
                for item in intents
            )
        ):
            failures.append("unified committed artifact postcheck did not pass before success")
            policy_failures.append(
                f"{receipt.action_id} unified committed artifact postcheck failed"
            )
        evidence_matches = tuple(
            evidence
            for evidence in durable.snapshot.gate_evidence
            if evidence.passed
            and evidence.gate == receipt.validator_id
            and evidence.validator_version == receipt.validator_version
            and evidence.artifact_checksums
            == tuple(item.checksum for item in receipt.artifacts)
        )
        action_bound_evidence = tuple(
            evidence
            for evidence in durable.committed_gate_evidence
            if evidence.action_id == receipt.action_id
            and evidence.passed
            and evidence.gate == receipt.validator_id
            and evidence.validator_version == receipt.validator_version
            and evidence.artifact_checksums
            == tuple(item.checksum for item in receipt.artifacts)
            and evidence.gate_decision_digest == receipt.gate_decision_digest
            and evidence.bundle_digest == receipt.bundle_digest
            and evidence.evidence_refs == receipt.evidence_refs
            and action is not None
            and action.committed_at is not None
            and evidence.committed_at <= action.committed_at
        )
        if len(evidence_matches) != 1 or len(action_bound_evidence) != 1:
            failures.append("committed gate evidence artifact checksums disagree")
        if failures:
            policy_failures.append(
                f"{receipt.action_id} gate integrity replay failed: {'; '.join(failures)}"
            )
        integrity.append(
            GateIntegrityItem(
                gate=receipt.validator_id,
                produces=receipt.action_id,
                recorded="PASS",
                replay_ok=not failures,
                replay_reason=(
                    "ledger gate policy replay passed"
                    if not failures
                    else "; ".join(failures)
                ),
                consistent=not failures,
                verifiable=True,
            )
        )

    attempts_by_action: dict[str, list[ActionAttemptRecord]] = {}
    for attempt in durable.attempts:
        attempts_by_action.setdefault(attempt.action_id, []).append(attempt)
    for action_id, lineage in attempts_by_action.items():
        ordered = sorted(lineage, key=lambda item: item.attempt)
        if len({item.attempt for item in ordered}) != len(ordered):
            policy_failures.append(f"{action_id} executor attempt IDs repeat")
        for successor in (item for item in ordered if item.retry_of_attempt is not None):
            predecessors = [
                item for item in ordered if item.attempt == successor.retry_of_attempt
            ]
            action = actions.get(action_id)
            exact_successors = [
                item for item in ordered if item.retry_of_attempt == successor.retry_of_attempt
            ]
            predecessor = predecessors[0] if len(predecessors) == 1 else None
            predecessor_receipt = (
                outcomes.get((action_id, predecessor.attempt))
                if predecessor is not None
                else None
            )
            predecessor_envelope = (
                parsed_outcomes.get((action_id, predecessor.attempt))
                if predecessor is not None
                else None
            )
            predecessor_outcome = (
                predecessor_envelope.outcome
                if predecessor_envelope is not None
                else None
            )
            retry_authorized = bool(
                predecessor is not None
                and predecessor_receipt is not None
                and isinstance(predecessor_outcome, RetryableFailure)
                and predecessor_receipt.error_code == predecessor_outcome.error_code
                and predecessor_outcome.error_code in predecessor.retry_policy.retryable_codes
                and predecessor.attempt < predecessor.retry_policy.max_attempts
                and predecessor.retry_policy_fingerprint
                == sha256_canonical_json(canonical_model_json(predecessor.retry_policy))
            )
            start_events = tuple(
                event
                for event in durable.outbox_events
                if event.event_name == "action.started"
                and json.loads(event.payload_json).get("action_id") == action_id
                and json.loads(event.payload_json).get("attempt") == successor.attempt
            )
            outcome_events = tuple(
                event
                for event in durable.outbox_events
                if event.event_name == "action.outcome"
                and json.loads(event.payload_json).get("action_id") == action_id
                and json.loads(event.payload_json).get("attempt")
                == successor.retry_of_attempt
            )
            successor_chronology = bool(
                successor.status is ActionStatus.AUTHORIZED
                or (
                    len(start_events) == 1
                    and len(outcome_events) == 1
                    and outcome_events[0].sequence < start_events[0].sequence
                    and successor.started_at is not None
                )
            )
            valid = (
                retry_authorized
                and len(exact_successors) == 1
                and predecessor is not None
                and successor.attempt == predecessor.attempt + 1
                and predecessor.status is ActionStatus.RETRY_WAIT
                and successor_chronology
                and action is not None
                and action.status is successor.status
                and successor.expected_artifact_manifest
                == predecessor.expected_artifact_manifest
                and successor.expected_manifest_digest
                == predecessor.expected_manifest_digest
                and successor.parameters_json == predecessor.parameters_json
                and successor.expected_evidence_refs == predecessor.expected_evidence_refs
                and successor.retry_policy == predecessor.retry_policy
                and successor.retry_policy_fingerprint
                == predecessor.retry_policy_fingerprint
                and successor.staging_relpath
                == f"state/staging/{action_id}/{successor.attempt}"
                and successor.staging_relpath != predecessor.staging_relpath
            )
            if not valid:
                reason = (
                    "lacks eligible RetryableFailure/error/policy authority"
                    if not retry_authorized
                    else "changed frozen facts, reused staging, or violated attempt chronology"
                )
                policy_failures.append(f"{action_id} retry successor {reason}")

    for repair in durable.repair_facts:
        action = actions.get(repair.action_id)
        attempt = attempts.get((repair.action_id, repair.attempt))
        original_receipt = outcomes.get((repair.action_id, repair.attempt))
        if original_receipt is None or repair.outcome_digest != original_receipt.outcome_digest:
            policy_failures.append(
                f"{repair.action_id} repair fact outcome digest is not bound to its original receipt"
            )
        if repair.repair_class == "semantic":
            if any(
                resolution.source_action_id == repair.action_id
                for resolution in durable.unblock_resolutions
            ):
                policy_failures.append(
                    f"{repair.action_id} semantic repair used a forbidden manual unblock"
                )
            superseding_plans = tuple(
                plan
                for plan in durable.plan_versions
                if repair.action_id in plan.patch.superseded_action_ids
            )
            replacements = tuple(
                candidate
                for candidate in durable.actions
                if any(candidate.plan_version == plan.version for plan in superseding_plans)
                and candidate.action_id != repair.action_id
            )
            direct_replacements = tuple(
                candidate
                for candidate in replacements
                if any(
                    candidate.plan_version == plan.version
                    and any(
                        proposed.capability == candidate.capability
                        for proposed in plan.patch.proposed_actions
                    )
                    for plan in superseding_plans
                )
            )
            replacement_attempts = tuple(
                candidate
                for candidate in durable.attempts
                if any(candidate.action_id == item.action_id for item in direct_replacements)
            )
            valid = (
                action is not None
                and attempt is not None
                and action.status is ActionStatus.REPAIR_REQUIRED
                and attempt.status is ActionStatus.REPAIR_REQUIRED
                and action.repair_class == repair.repair_class
                and action.repair_source == repair.repair_source
                and action.reason_code == repair.reason_code
                and durable.run.status is RunStatus.RUNNING
                and len(superseding_plans) == 1
                and superseding_plans[0].version == action.plan_version + 1
                and len(direct_replacements) == 1
                and len(replacement_attempts) == 1
                and replacement_attempts[0].attempt == 1
                and replacement_attempts[0].staging_relpath
                == f"state/staging/{direct_replacements[0].action_id}/1"
                and replacement_attempts[0].staging_relpath != attempt.staging_relpath
            )
            if not valid:
                policy_failures.append(
                    f"{repair.action_id} semantic repair lacks exactly one superseding action"
                )
            continue

        superseding_plans = tuple(
            plan
            for plan in durable.plan_versions
            if repair.action_id in plan.patch.superseded_action_ids
        )
        matching_unblocks = tuple(
            resolution
            for resolution in durable.unblock_resolutions
            if resolution.source_action_id == repair.action_id
            and resolution.replacement_action_id is not None
            and resolution.plan_version is not None
            and resolution.staging_relpath
            == f"state/staging/{resolution.replacement_action_id}/1"
            and resolution.request.source_action_id == repair.action_id
            and resolution.request_digest
            == sha256_canonical_json(canonical_model_json(resolution.request))
            and any(
                candidate.action_id == resolution.replacement_action_id
                and candidate.plan_version == resolution.plan_version
                and any(
                    plan.version == resolution.plan_version
                    and any(
                        proposed.capability == candidate.capability
                        for proposed in plan.patch.proposed_actions
                    )
                    for plan in superseding_plans
                )
                for candidate in durable.actions
            )
        )
        blocked_without_replacement = (
            durable.run.status is RunStatus.BLOCKED
            and not superseding_plans
            and not matching_unblocks
        )
        unblocked_with_one_replacement = (
            durable.run.status is RunStatus.RUNNING
            and len(matching_unblocks) == 1
        )
        if not (
            action is not None
            and attempt is not None
            and action.status is ActionStatus.REPAIR_REQUIRED
            and attempt.status is ActionStatus.REPAIR_REQUIRED
            and (blocked_without_replacement or unblocked_with_one_replacement)
        ):
            policy_failures.append(
                f"{repair.action_id} integrity repair replacement lacks matching human unblock"
            )

    valid_probe_successes: set[tuple[str, int]] = set()
    probe_keys = [
        (item.original_action_id, item.original_attempt) for item in durable.probe_resolutions
    ]
    probe_action_keys = [
        (item.probe_action_id, item.probe_attempt) for item in durable.probe_resolutions
    ]
    if len(probe_keys) != len(set(probe_keys)) or len(probe_action_keys) != len(
        set(probe_action_keys)
    ):
        policy_failures.append("probe resolution lineage is not one-to-one")
    for resolution in durable.probe_resolutions:
        original_key = (resolution.original_action_id, resolution.original_attempt)
        probe_key = (resolution.probe_action_id, resolution.probe_attempt)
        original_action = actions.get(resolution.original_action_id)
        original_attempt = attempts.get(original_key)
        probe_action = actions.get(resolution.probe_action_id)
        probe_attempt = attempts.get(probe_key)
        original_receipt = outcomes.get(original_key)
        probe_receipt = outcomes.get(probe_key)
        if (
            original_action is None
            or original_attempt is None
            or probe_action is None
            or probe_attempt is None
            or original_receipt is None
            or probe_receipt is None
        ):
            policy_failures.append(
                f"{resolution.original_action_id} probe resolution violates immutable authority"
            )
            continue
        try:
            original_envelope = ActionOutcomeEnvelope.model_validate_json(
                original_receipt.canonical_outcome_json
            )
            probe_envelope = ActionOutcomeEnvelope.model_validate_json(
                probe_receipt.canonical_outcome_json
            )
        except ValueError:
            policy_failures.append(
                f"{resolution.original_action_id} probe resolution violates immutable authority"
            )
            continue
        if probe_envelope is not None and isinstance(probe_envelope.outcome, Succeeded):
            policy_failures.append(
                f"{resolution.probe_action_id} probe action masqueraded as ordinary success"
            )
        valid = bool(
            isinstance(original_envelope.outcome, Indeterminate)
            and isinstance(probe_envelope.outcome, ProbeResolution)
            and original_envelope.outcome.operation_key == resolution.operation_key
            and probe_envelope.outcome.operation_key == resolution.operation_key
            and probe_envelope.outcome.disposition == resolution.disposition
            and probe_envelope.outcome.evidence_refs == resolution.evidence_refs
            and resolution.resolution_digest
            == sha256_canonical_json(canonical_model_json(probe_envelope.outcome))
            and original_receipt.error_code == resolution.error_code
            and original_envelope.outcome.error_code == resolution.error_code
            and original_receipt.failure_signature == resolution.failure_signature
            and original_envelope.outcome.failure_signature == resolution.failure_signature
            and original_action.idempotency_key == resolution.original_idempotency_key
            and original_attempt.retry_policy == resolution.retry_policy
            and original_attempt.retry_policy_fingerprint
            == resolution.retry_policy_fingerprint
            and original_action.retry_policy_fingerprint
            == resolution.retry_policy_fingerprint
            and probe_action.status is ActionStatus.SUCCEEDED
            and probe_attempt.status is ActionStatus.SUCCEEDED
        )
        if valid and resolution.disposition == "succeeded":
            valid = bool(
                original_action.status is ActionStatus.SUCCEEDED
                and original_attempt.status is ActionStatus.SUCCEEDED
            )
            if valid:
                valid_probe_successes.add(original_key)
        elif valid and resolution.disposition == "absent":
            valid = bool(
                original_action.status is ActionStatus.RETRY_WAIT
                and original_attempt.status is ActionStatus.RETRY_WAIT
                and any(
                    item.action_id == resolution.original_action_id
                    and item.retry_of_attempt == resolution.original_attempt
                    for item in durable.attempts
                )
            )
        elif valid and resolution.disposition == "unknown":
            valid = bool(
                durable.run.status is RunStatus.BLOCKED
                and original_action.status is ActionStatus.INDETERMINATE
                and original_attempt.status is ActionStatus.INDETERMINATE
            )
        if not valid:
            policy_failures.append(
                f"{resolution.original_action_id} probe resolution violates immutable authority"
            )

    for outcome_key in outcomes:
        try:
            routed_envelope = (
                ActionOutcomeEnvelope.model_validate_json(
                    effective_outcomes[outcome_key].canonical_outcome_json
                )
                if outcome_key in effective_outcomes
                else parsed_outcomes.get(outcome_key)
            )
        except ValueError:
            routed_envelope = None
        if routed_envelope is None:
            continue
        routed_action = actions.get(outcome_key[0])
        routed_attempt = attempts.get(outcome_key)
        if (
            not isinstance(routed_envelope.outcome, (Succeeded, ProbeResolution))
            and (
                (routed_action is not None and routed_action.status is ActionStatus.SUCCEEDED)
                or (
                    routed_attempt is not None
                    and routed_attempt.status is ActionStatus.SUCCEEDED
                )
            )
            and outcome_key not in valid_probe_successes
        ):
            policy_failures.append(
                f"{outcome_key[0]} non-probe outcome has contradictory terminal status"
            )

    continuations_by_interrupt = {
        hitl_decision.interrupt_id: tuple(
            item
            for item in durable.hitl_continuations
            if item.interrupt_id == hitl_decision.interrupt_id
        )
        for hitl_decision in durable.interrupt_decisions
    }
    for hitl_decision in durable.interrupt_decisions:
        hitl_key = (hitl_decision.action_id, hitl_decision.attempt)
        initial = outcomes.get(hitl_key)
        hitl_action = actions.get(hitl_decision.action_id)
        hitl_attempt = attempts.get(hitl_key)
        continuations = tuple(
            sorted(
                continuations_by_interrupt[hitl_decision.interrupt_id],
                key=lambda item: item.sequence,
            )
        )
        valid_initial = False
        if initial is not None:
            try:
                initial_envelope = ActionOutcomeEnvelope.model_validate_json(
                    initial.canonical_outcome_json
                )
            except ValueError:
                initial_envelope = None
            valid_initial = bool(
                initial_envelope is not None
                and isinstance(initial_envelope.outcome, Paused)
                and initial_envelope.outcome.reason == "hitl"
                and initial.outcome_digest == hitl_decision.pause_outcome_digest
            )
        valid = valid_initial and hitl_action is not None and hitl_attempt is not None
        if hitl_decision.status == "STARTED":
            valid = bool(
                valid
                and not continuations
                and durable.run.status is RunStatus.BLOCKED
                and hitl_action is not None
                and hitl_action.status is ActionStatus.INDETERMINATE
                and hitl_attempt is not None
                and hitl_attempt.status is ActionStatus.INDETERMINATE
                and hitl_action.repair_class == "integrity"
            )
        elif hitl_decision.status == "RESOLVED":
            effective = effective_outcomes.get(hitl_key)
            sequences = tuple(item.sequence for item in continuations)
            valid = bool(
                valid
                and continuations
                and sequences == tuple(range(1, len(continuations) + 1))
                and effective is not None
                and effective.source == "hitl_continuation"
                and effective.sequence == continuations[-1].sequence
                and effective.outcome_digest == continuations[-1].outcome_digest
                and effective.canonical_outcome_json
                == continuations[-1].canonical_outcome_json
            )
            if valid and effective is not None:
                effective_envelope = ActionOutcomeEnvelope.model_validate_json(
                    effective.canonical_outcome_json
                )
                if isinstance(effective_envelope.outcome, Succeeded):
                    valid = bool(
                        hitl_key
                        in {(item.action_id, item.attempt) for item in durable.gate_receipts}
                        and hitl_key
                        in {
                            (item.action_id, item.attempt)
                            for item in durable.promotion_intents
                        }
                    )
        if not valid:
            policy_failures.append(
                f"{hitl_decision.action_id} HITL lineage is not immutable, ordered, BLOCKED, or gate-authorized"
            )

    for unblock_resolution in durable.unblock_resolutions:
        if unblock_resolution.request_digest != sha256_canonical_json(
            canonical_model_json(unblock_resolution.request)
        ):
            policy_failures.append(
                f"{unblock_resolution.run_id} unblock evidence digest or replacement identity drifted"
            )
        source_intents = tuple(
            item
            for item in durable.promotion_intents
            if item.action_id == unblock_resolution.source_action_id
        )
        if source_intents and any(item.status != "CONFLICT" for item in source_intents):
            policy_failures.append(
                f"{unblock_resolution.source_action_id} conflict history was reset or reused after unblock"
            )

    for action in durable.actions:
        if not action.capability.startswith("release.") or action.status is not ActionStatus.SUCCEEDED:
            continue
        prerequisites = tuple(actions.get(dependency) for dependency in action.dependencies)
        if any(
            prerequisite is None
            or prerequisite.status is not ActionStatus.SUCCEEDED
            or prerequisite.committed_at is None
            for prerequisite in prerequisites
        ):
            policy_failures.append(
                f"{action.action_id} release prerequisite was not durably committed"
            )

    for rejection in durable.snapshot.plan_rejections:
        if not rejection.reason_codes:
            policy_failures.append(
                f"plan rejection {rejection.plan_version} has no deterministic reason codes"
            )

    for event in durable.outbox_events:
        if event.event_name != "action.outcome":
            continue
        try:
            payload = json.loads(event.payload_json)
            key = (str(payload["action_id"]), int(payload["attempt"]))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            policy_failures.append("action outcome event has invalid typed identity")
            continue
        event_receipt = outcomes.get(key)
        if event_receipt is None or event_receipt.recorded_at > event.created_at:
            policy_failures.append(
                f"{key[0]} outcome receipt did not precede controller handling"
            )
    metrics = _read_metrics(project)
    stage_attempts, first_pass, capped, budget_stops = _read_events(project)
    tokens = (metrics or {}).get("tokens", {}) if metrics else {}
    blocked_reason = durable.open_incidents[-1].error_code if durable.open_incidents else None
    if run.status is RunStatus.BLOCKED or policy_failures:
        verdict = "FAIL"
    elif run.status is RunStatus.COMPLETED:
        verdict = "PASS"
    else:
        verdict = "WARN"
    return TraceReport(
        book=project.root.name,
        status=run.status.value,
        is_done=run.status is RunStatus.COMPLETED,
        blocked_reason=blocked_reason,
        gate_integrity=integrity,
        gate_integrity_ok=all(item.consistent for item in integrity),
        reached_states_ok=True,
        reached_states_failures=[],
        path_conformance_ok=not policy_failures,
        skipped_states=policy_failures,
        cost_usd=(metrics or {}).get("cost_usd") if metrics else None,
        tokens_in=tokens.get("input") if isinstance(tokens, dict) else None,
        tokens_out=tokens.get("output") if isinstance(tokens, dict) else None,
        duration_s=(metrics or {}).get("duration_s") if metrics else None,
        llm_calls=(metrics or {}).get("llm_calls") if metrics else None,
        stage_attempts=stage_attempts,
        first_pass_rate=round(first_pass, 4) if first_pass is not None else None,
        recursion_caps=capped,
        budget_stops=budget_stops,
        verdict=verdict,
    )
