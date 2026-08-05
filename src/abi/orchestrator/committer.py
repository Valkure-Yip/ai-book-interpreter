"""Receipt-bound validation, bundle promotion, and durable success commit."""

from __future__ import annotations

from collections.abc import Callable

from abi.actions.evidence import StagingEvidenceView
from abi.actions.registry import ActionRegistry
from abi.project.artifacts import ArtifactConflictError, ArtifactStore
from abi.project.layout import BookProject
from abi.project.run_ledger import (
    ActionRecord,
    ArtifactCommit,
    CommittedAction,
    PromotionIntent,
    RunLedger,
    SuccessCommit,
)
from abi.types.orchestration import (
    ActionOutcomeEnvelope,
    GateArtifactIdentity,
    GateDecision,
    GateEvidence,
    GateReceiptPayload,
    Succeeded,
    canonical_model_json,
    sha256_canonical_json,
)

CommitHook = Callable[[str, object], None]


class CommitValidationError(RuntimeError):
    """A deterministic validator refused an Action's claimed success."""

    def __init__(self, decision: GateDecision) -> None:
        super().__init__(
            f"gate {decision.reason_code} rejected Action success: {decision.message}; "
            "route the durable validator fact through its registered repair mapping"
        )
        self.decision = decision


class Committer:
    """Own the only six-step path from a received bundle to business success."""

    def __init__(
        self,
        *,
        ledger: RunLedger,
        registry: ActionRegistry,
        project: BookProject,
        artifacts: ArtifactStore,
        test_hook: CommitHook | None = None,
    ) -> None:
        self._ledger = ledger
        self._registry = registry
        self._project = project
        self._artifacts = artifacts
        self._test_hook = test_hook

    async def commit(
        self,
        *,
        run_id: str,
        action: ActionRecord,
        attempt: int,
        outcome: Succeeded,
        cost_usd: float = 0.0,
    ) -> CommittedAction:
        """Validate receipts, persist all intents, promote all, then commit once."""
        try:
            receipt = await self._ledger.get_attempt_outcome(action.action_id, attempt)
            caller_envelope = ActionOutcomeEnvelope(
                action_id=action.action_id,
                attempt=attempt,
                outcome=outcome,
            )
            if receipt.canonical_outcome_json != canonical_model_json(caller_envelope):
                raise ValueError("caller outcome differs from the durable outcome receipt")
            bundle = outcome.artifact_bundle
            if action.run_id != run_id:
                raise ValueError("Action belongs to a different run")
            if bundle.action_id != action.action_id or bundle.attempt != attempt:
                raise ValueError("bundle identity differs from the durable attempt")
            expected = tuple(
                (
                    item.canonical_relpath,
                    item.media_type,
                    item.evidence_role,
                    item.metadata,
                )
                for item in action.expected_artifact_manifest.entries
            )
            actual = tuple(
                (
                    item.canonical_relpath,
                    item.media_type,
                    item.evidence_role,
                    item.metadata,
                )
                for item in bundle.entries
            )
            if actual != expected:
                raise ValueError("bundle does not equal the frozen expected manifest")
            if any(
                not _write_allowed(item.canonical_relpath, action.write_set)
                for item in bundle.entries
            ):
                raise ValueError("bundle contains a canonical path outside Action write permission")
            await self._artifacts.require_exact_staging(action.action_id, attempt)
            snapshot = await self._ledger.load_snapshot(run_id)
            view = StagingEvidenceView.for_bundle(
                self._project, snapshot.artifacts, bundle
            )
        except (OSError, TypeError, ValueError) as exc:
            await self._integrity_conflict(
                action,
                attempt,
                reason_code="receipt_binding_conflict",
                message=f"Commit receipt/bundle validation failed: {exc}",
            )
            raise ArtifactConflictError("commit receipt or bundle binding failed") from exc

        try:
            resolved = self._registry.resolve_json(
                action.capability, action.parameters_json
            )
            decision = resolved.definition.validator(
                view, resolved.parameters, bundle
            )
        except Exception as exc:
            await self._integrity_conflict(
                action,
                attempt,
                reason_code="gate_binding_conflict",
                message=(
                    "Validator registry/implementation failed after the durable outcome "
                    f"receipt: {type(exc).__name__}"
                ),
            )
            raise ArtifactConflictError("validator execution boundary failed") from exc
        expected_validator = resolved.definition.spec.validator
        if (
            decision.validator_id != expected_validator
            or decision.bundle_digest != view.bundle_digest
            or decision.artifact_checksums != view.artifact_checksums
        ):
            await self._integrity_conflict(
                action,
                attempt,
                reason_code="gate_binding_conflict",
                message="Validator decision is not bound to the current bundle and checksums.",
            )
            raise ArtifactConflictError("validator decision binding failed")
        if not decision.passed:
            raise CommitValidationError(decision)

        identities = tuple(
            GateArtifactIdentity(
                staged_relpath=entry.staged_relpath,
                canonical_relpath=entry.canonical_relpath,
                checksum=checksum,
            )
            for entry, checksum in zip(
                bundle.entries, view.artifact_checksums, strict=True
            )
        )
        gate_json = canonical_model_json(decision)
        gate_payload = GateReceiptPayload(
            action_id=action.action_id,
            attempt=attempt,
            validator_id=decision.validator_id,
            validator_version=decision.validator_version,
            canonical_gate_decision_json=gate_json,
            gate_decision_digest=sha256_canonical_json(gate_json),
            bundle_digest=view.bundle_digest,
            artifacts=identities,
            evidence_refs=decision.evidence_refs,
        )
        self._invoke_hook("before_gate_receipt_and_intents", gate_payload)
        _, intents = await self._ledger.create_gate_receipt_and_bundle_intents(
            gate_payload
        )
        self._invoke_hook("after_gate_receipt_and_intents", intents)

        return await self._promote_and_finalize(
            action=action,
            attempt=attempt,
            decision=decision,
            intents=intents,
            cost_usd=cost_usd,
        )

    async def finalize_from_gate(
        self,
        *,
        run_id: str,
        action: ActionRecord,
        attempt: int,
        cost_usd: float = 0.0,
    ) -> CommittedAction:
        """Resume promotion from the durable gate receipt without rerunning validation."""
        if action.run_id != run_id:
            raise ValueError("Action belongs to a different run; reconcile the owning run")
        gate, intents = await self._ledger.get_gate_receipt_and_intents(
            action.action_id, attempt
        )
        expected_paths = tuple(
            item.canonical_relpath
            for item in action.expected_artifact_manifest.entries
        )
        actual_paths = tuple(item.canonical_relpath for item in intents)
        if actual_paths != expected_paths:
            await self._integrity_conflict(
                action,
                attempt,
                reason_code="partial_intent_set",
                message="Durable promotion intents are not the complete expected bundle.",
            )
            raise ArtifactConflictError("durable promotion intent set is partial")
        decision = GateDecision.model_validate_json(
            gate.canonical_gate_decision_json
        )
        return await self._promote_and_finalize(
            action=action,
            attempt=attempt,
            decision=decision,
            intents=intents,
            cost_usd=cost_usd,
        )

    async def _promote_and_finalize(
        self,
        *,
        action: ActionRecord,
        attempt: int,
        decision: GateDecision,
        intents: tuple[PromotionIntent, ...],
        cost_usd: float,
    ) -> CommittedAction:
        promoted = []
        for index, intent in enumerate(intents):
            if intent.status == "CONFLICT":
                await self._integrity_conflict(
                    action,
                    attempt,
                    reason_code="artifact_bundle_conflict",
                    message="A durable bundle intent is CONFLICT; preserve all siblings.",
                )
                raise ArtifactConflictError("bundle contains a conflicting intent")
            self._invoke_hook("before_intent_promotion", intent)
            promoted_intent = (
                intent
                if intent.status == "COMMITTED"
                else await self._artifacts.promote(intent)
            )
            promoted.append(promoted_intent)
            self._invoke_hook("after_intent_promotion", promoted_intent)
            if index + 1 < len(intents):
                self._invoke_hook("between_bundle_entries", promoted_intent)
        self._invoke_hook("after_all_intents_committed", tuple(promoted))

        committed_intents = await self._artifacts.verify_committed_bundle(
            action.action_id, attempt
        )
        self._invoke_hook("after_unified_bundle_postcheck", committed_intents)
        artifacts = tuple(
            ArtifactCommit(
                artifact_id=(
                    f"artifact:{action.action_id}:{attempt}:{intent.ordinal}"
                ),
                relpath=intent.canonical_relpath,
                sha256=intent.checksum,
                producer_action_id=action.action_id,
                media_type=intent.media_type,
            )
            for intent in committed_intents
        )
        gate_evidence = GateEvidence(
            evidence_id=f"gate:{action.action_id}:{attempt}",
            gate=decision.validator_id,
            passed=True,
            validator_version=decision.validator_version,
            artifact_checksums=decision.artifact_checksums,
        )
        self._invoke_hook(
            "before_success_ledger_commit", (artifacts, gate_evidence)
        )
        result = await self._ledger.commit_success(
            SuccessCommit(
                action_id=action.action_id,
                attempt=attempt,
                artifacts=artifacts,
                gate_evidence=(gate_evidence,),
                cost_usd=cost_usd,
            )
        )
        self._invoke_hook("after_success_ledger_commit", result)
        return result

    async def resume_success(
        self,
        *,
        run_id: str,
        action: ActionRecord,
        attempt: int,
        cost_usd: float = 0.0,
    ) -> CommittedAction:
        """Resume only from the immutable ordinary-success receipt."""
        receipt = await self._ledger.get_attempt_outcome(action.action_id, attempt)
        envelope = ActionOutcomeEnvelope.model_validate_json(
            receipt.canonical_outcome_json
        )
        if not isinstance(envelope.outcome, Succeeded):
            raise ValueError(
                "resume_success requires an ordinary-success receipt; route the durable outcome kind"
            )
        return await self.commit(
            run_id=run_id,
            action=action,
            attempt=attempt,
            outcome=envelope.outcome,
            cost_usd=cost_usd,
        )

    async def _integrity_conflict(
        self,
        action: ActionRecord,
        attempt: int,
        *,
        reason_code: str,
        message: str,
    ) -> None:
        await self._ledger.mark_bundle_conflict(
            action.action_id,
            attempt,
            reason_code=reason_code,
            message=message,
        )

    def _invoke_hook(self, point: str, detail: object) -> None:
        if self._test_hook is not None:
            self._test_hook(point, detail)


def _write_allowed(canonical_relpath: str, write_set: tuple[str, ...]) -> bool:
    return any(
        canonical_relpath == permitted
        or canonical_relpath.startswith(permitted.rstrip("/") + "/")
        for permitted in write_set
    )
