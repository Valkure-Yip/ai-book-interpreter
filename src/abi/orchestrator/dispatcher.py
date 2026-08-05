"""Typed Action dispatch with durable attempt identity and outcome receipts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from pydantic import ValidationError

from abi.actions.contracts import ActionExecutionContext
from abi.actions.registry import ActionRegistry, RegistryConfigurationError
from abi.project.layout import BookProject
from abi.project.run_ledger import ActionRecord, RunLedger
from abi.types.orchestration import (
    ActionOutcomeEnvelope,
    AttemptOutcomeReceiptPayload,
    Indeterminate,
    PermanentFailure,
    ProbeActionInput,
    ProbeResolution,
    RepairRequired,
    RetryableFailure,
    RunSnapshot,
    Succeeded,
    canonical_bundle_json,
    canonical_failure_signature,
    canonical_model_json,
    sha256_canonical_json,
)
from abi.types.tools import GateRuntimeMetadata

DispatchHook = Callable[[str, ActionOutcomeEnvelope], None]


class Dispatcher:
    """Claim one explicit authorized attempt and persist its typed handoff."""

    def __init__(
        self,
        *,
        ledger: RunLedger,
        registry: ActionRegistry,
        project: BookProject,
        runtime_metadata: GateRuntimeMetadata,
        source_lang: str,
        source_target: str,
        book_slug: str,
        timeout_s: float,
        profile: str | None = None,
        test_hook: DispatchHook | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError(
                "Action timeout_s must be positive; configure a bounded execution deadline"
            )
        self._ledger = ledger
        self._registry = registry
        self._project = project
        self._runtime_metadata = runtime_metadata
        self._source_lang = source_lang
        self._source_target = source_target
        self._book_slug = book_slug
        self._timeout_s = timeout_s
        self._profile = profile
        self._test_hook = test_hook

    async def execute(
        self,
        *,
        run_id: str,
        action: ActionRecord,
        snapshot: RunSnapshot,
        attempt: int,
    ) -> ActionOutcomeEnvelope:
        """Execute an explicit first-claim attempt and durably receipt its exact outcome."""
        durable_attempt = await self._ledger.start_attempt(
            action.action_id, attempt=attempt
        )
        try:
            resolved = self._registry.resolve_json(
                action.capability, durable_attempt.parameters_json
            )
            context = ActionExecutionContext(
                project=self._project,
                run_id=run_id,
                snapshot=snapshot,
                action_id=action.action_id,
                attempt=durable_attempt.attempt,
                source_lang=self._source_lang,
                target_lang=self._runtime_metadata.target_language,
                source_target=self._source_target,
                publication_mode=self._runtime_metadata.publication_mode,
                book_slug=self._book_slug,
                profile=self._profile,
                runtime_metadata=self._runtime_metadata,
            )
            raw = await asyncio.wait_for(
                resolved.definition.executor(context, resolved.parameters),
                timeout=self._timeout_s,
            )
            envelope = ActionOutcomeEnvelope.model_validate(raw)
        except TimeoutError:
            definition = self._registry.get(action.capability)
            if definition.spec.may_have_side_effects:
                error_code = "provider_timeout"
                envelope = ActionOutcomeEnvelope(
                    action_id=action.action_id,
                    attempt=durable_attempt.attempt,
                    outcome=Indeterminate(
                        operation_key=_stable_operation_key(
                            action, durable_attempt.attempt
                        ),
                        error_code=error_code,
                        failure_signature=canonical_failure_signature(
                            action.capability,
                            durable_attempt.parameters_json,
                            error_code,
                        ),
                        message=(
                            "Action timed out after a possible external side effect; run its "
                            "registered probe before any retry"
                        ),
                    ),
                )
            elif "provider_timeout" in durable_attempt.retry_policy.retryable_codes:
                envelope = ActionOutcomeEnvelope(
                    action_id=action.action_id,
                    attempt=durable_attempt.attempt,
                    outcome=RetryableFailure(
                        error_code="provider_timeout",
                        message=(
                            "Action exceeded its registered timeout; route the durable receipt "
                            "through the frozen retry policy"
                        ),
                    ),
                )
            else:
                envelope = ActionOutcomeEnvelope(
                    action_id=action.action_id,
                    attempt=durable_attempt.attempt,
                    outcome=PermanentFailure(
                        error_code="unregistered_timeout_classification",
                        message=(
                            "Action timed out without a registered provider_timeout classification; "
                            "repair the ActionSpec before retrying"
                        ),
                    ),
                )
        except (RegistryConfigurationError, ValidationError, TypeError, ValueError) as exc:
            envelope = self._integrity_envelope(
                action,
                durable_attempt.attempt,
                reason_code="artifact_identity_conflict",
                message=f"{exc}; repair the registered Action boundary before resuming",
            )
        except Exception as exc:
            definition = self._registry.get(action.capability)
            if definition.spec.may_have_side_effects:
                error_code = "external_side_effect_unclassified"
                envelope = ActionOutcomeEnvelope(
                    action_id=action.action_id,
                    attempt=durable_attempt.attempt,
                    outcome=Indeterminate(
                        operation_key=_stable_operation_key(
                            action, durable_attempt.attempt
                        ),
                        error_code=error_code,
                        failure_signature=canonical_failure_signature(
                            action.capability,
                            durable_attempt.parameters_json,
                            error_code,
                        ),
                        message=(
                            f"Action raised {type(exc).__name__} after a possible side effect; "
                            "run its registered probe before any retry"
                        ),
                    ),
                )
            else:
                envelope = ActionOutcomeEnvelope(
                    action_id=action.action_id,
                    attempt=durable_attempt.attempt,
                    outcome=PermanentFailure(
                        error_code="unclassified_action_failure",
                        message=(
                            f"Action raised unclassified {type(exc).__name__}; register an explicit "
                            "classification and repair instruction before retrying"
                        ),
                    ),
                )

        envelope = self._classify_boundary(action, durable_attempt.attempt, envelope)
        self._invoke_hook("before_outcome_receipt", envelope)
        await self._ledger.record_attempt_outcome(self._receipt(envelope))
        self._invoke_hook("after_action_output", envelope)
        return envelope
    def _classify_boundary(
        self,
        action: ActionRecord,
        attempt: int,
        envelope: ActionOutcomeEnvelope,
    ) -> ActionOutcomeEnvelope:
        if envelope.action_id != action.action_id or envelope.attempt != attempt:
            return self._integrity_envelope(
                action,
                attempt,
                reason_code="artifact_identity_conflict",
                message=(
                    "Executor envelope identity differs from the claimed durable attempt; "
                    "inspect the executor boundary before resuming"
                ),
            )
        outcome = envelope.outcome
        is_probe = any(
            spec.probe_capability == action.capability
            for spec in self._registry.specs()
        )
        if isinstance(outcome, ProbeResolution) and not is_probe:
            return self._integrity_envelope(
                action,
                attempt,
                reason_code="probe_resolution_conflict",
                message=(
                    "A non-probe capability returned ProbeResolution; bind a registered "
                    "evidence-only probe before resolving the operation"
                ),
            )
        if is_probe and not isinstance(outcome, ProbeResolution):
            return self._integrity_envelope(
                action,
                attempt,
                reason_code="probe_resolution_conflict",
                message=(
                    "A registered probe must return ProbeResolution; repair the probe executor "
                    "instead of treating evidence-only work as ordinary success"
                ),
            )
        if isinstance(outcome, ProbeResolution) and is_probe:
            try:
                binding = ProbeActionInput.model_validate_json(action.parameters_json)
            except (TypeError, ValueError):
                return self._integrity_envelope(
                    action,
                    attempt,
                    reason_code="probe_resolution_conflict",
                    message="Probe parameters do not contain the frozen original-attempt binding.",
                )
            if (
                binding.probe_capability != action.capability
                or binding.operation_key != outcome.operation_key
            ):
                return self._integrity_envelope(
                    action,
                    attempt,
                    reason_code="probe_resolution_conflict",
                    message=(
                        "Probe resolution operation/capability differs from its frozen binding; "
                        "preserve the receipt and block resolution."
                    ),
                )
        if isinstance(outcome, Indeterminate):
            expected = canonical_failure_signature(
                action.capability, action.parameters_json, outcome.error_code
            )
            if outcome.failure_signature != expected:
                return self._integrity_envelope(
                    action,
                    attempt,
                    reason_code="external_side_effect_unclassified",
                    message=(
                        "Indeterminate failure signature differs from the authorized capability "
                        "and parameters; preserve evidence and inspect before retrying"
                    ),
                )
        if isinstance(outcome, Succeeded):
            bundle = outcome.artifact_bundle
            expected_effects = tuple(
                (
                    item.canonical_relpath,
                    item.media_type,
                    item.evidence_role,
                    item.metadata,
                )
                for item in action.expected_artifact_manifest.entries
            )
            actual_effects = tuple(
                (
                    item.canonical_relpath,
                    item.media_type,
                    item.evidence_role,
                    item.metadata,
                )
                for item in bundle.entries
            )
            if (
                bundle.action_id != action.action_id
                or bundle.attempt != attempt
                or actual_effects != expected_effects
            ):
                return self._integrity_envelope(
                    action,
                    attempt,
                    reason_code="artifact_bundle_conflict",
                    message=(
                        "Succeeded bundle differs from the durable expected manifest; preserve "
                        "staging and inspect the executor effects"
                    ),
                )
        return envelope

    @staticmethod
    def _integrity_envelope(
        action: ActionRecord,
        attempt: int,
        *,
        reason_code: str,
        message: str,
    ) -> ActionOutcomeEnvelope:
        return ActionOutcomeEnvelope(
            action_id=action.action_id,
            attempt=attempt,
            outcome=RepairRequired(
                repair_class="integrity",
                repair_source="action_outcome",
                reason_code=reason_code,
                defect_codes=(reason_code,),
                message=message,
            ),
        )

    @staticmethod
    def _receipt(envelope: ActionOutcomeEnvelope) -> AttemptOutcomeReceiptPayload:
        outcome_json = canonical_model_json(envelope)
        outcome = envelope.outcome
        bundle_json = (
            canonical_bundle_json(outcome.artifact_bundle)
            if isinstance(outcome, Succeeded)
            else None
        )
        return AttemptOutcomeReceiptPayload(
            action_id=envelope.action_id,
            attempt=envelope.attempt,
            canonical_outcome_json=outcome_json,
            outcome_digest=sha256_canonical_json(outcome_json),
            canonical_bundle_json=bundle_json,
            bundle_digest=(
                sha256_canonical_json(bundle_json)
                if bundle_json is not None
                else None
            ),
            evidence_refs=tuple(getattr(outcome, "evidence_refs", ())),
            error_code=getattr(outcome, "error_code", None),
            failure_signature=getattr(outcome, "failure_signature", None),
        )

    def _invoke_hook(self, point: str, envelope: ActionOutcomeEnvelope) -> None:
        if self._test_hook is not None:
            self._test_hook(point, envelope)


def _stable_operation_key(action: ActionRecord, attempt: int) -> str:
    """Bind external-operation identity only to frozen invocation facts."""
    invocation_json = json.dumps(
        {
            "action_id": action.action_id,
            "attempt": attempt,
            "capability": action.capability,
            "parameters_json": action.parameters_json,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"operation:{sha256_canonical_json(invocation_json)}"
