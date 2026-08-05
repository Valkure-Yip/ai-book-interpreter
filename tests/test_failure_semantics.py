"""Failure classification and indeterminate-side-effect protocol tests."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from abi.actions.contracts import (
    ActionDefinition,
    ActionExecutionContext,
    ActionExecutor,
    ActionValidator,
    EffectExpander,
)
from abi.actions.evidence import StagingEvidenceView
from abi.actions.predicates import PredicateCatalog
from abi.actions.registry import ActionRegistry, RegistryConfigurationError
from abi.project.run_ledger import (
    LedgerConflictError,
    ProbeResolutionRequest,
    RunLedger,
)
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionKind,
    ActionOutcomeEnvelope,
    ActionSpec,
    ActionStatus,
    ArtifactBundle,
    EffectSpec,
    ExpectedArtifactManifest,
    GateDecision,
    Indeterminate,
    PermanentFailure,
    ProbeActionInput,
    ProbeResolution,
    RetryPolicySpec,
    RunStatus,
    canonical_failure_signature,
    canonical_model_json,
    sha256_canonical_json,
)
from tests.test_dynamic_controller import (
    BoundaryCrash,
    SuccessTemplate,
    _authorize_one,
    _controller_rig,
)


class _NoInput(FrozenModel):
    pass


def _validator(
    _view: StagingEvidenceView,
    _parameters: FrozenModel,
    _bundle: ArtifactBundle,
) -> GateDecision:
    raise AssertionError("startup validation must not execute a validator")


async def _executor(
    _context: ActionExecutionContext, _parameters: FrozenModel
) -> ActionOutcomeEnvelope:
    raise AssertionError("startup validation must not execute a probe")


def _expand_effects(
    _capability: str, _action_id: str, _parameters: FrozenModel
) -> ExpectedArtifactManifest:
    raise AssertionError("startup validation must not expand probe effects")


def _definition(
    capability: str,
    *,
    input_model: type[FrozenModel] = _NoInput,
    effects: tuple[EffectSpec, ...] = (),
    probe_capability: str | None = None,
    write_set: tuple[str, ...] = (),
    may_have_side_effects: bool = False,
) -> ActionDefinition:
    return ActionDefinition(
        spec=ActionSpec(
            capability=capability,
            description=capability,
            input_schema=input_model.__name__,
            action_kind=ActionKind.DETERMINISTIC,
            effects=effects,
            write_set=write_set,
            validator="evidence",
            probe_capability=probe_capability,
            may_have_side_effects=may_have_side_effects,
        ),
        input_model=input_model,
        executor=cast(ActionExecutor, _executor),
        validator=cast(ActionValidator, _validator),
        effect_expander=cast(EffectExpander, _expand_effects),
    )


@pytest.mark.asyncio
async def test_ledger_schema_persists_immutable_probe_resolutions(tmp_path: Path) -> None:
    """Catch successful probe handling without an immutable resolution fact."""
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        rows = await ledger._fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name", ()
        )

    assert "probe_resolutions" in {str(row["name"]) for row in rows}


@pytest.mark.parametrize(
    "unsafe_update",
    (
        {"effects": (EffectSpec(name="remote.release"),)},
        {"write_set": ("reports/probe.json",)},
        {"may_have_side_effects": True},
    ),
    ids=("effects", "write-set", "side-effects"),
)
def test_registry_rejects_probe_that_is_not_evidence_only(
    unsafe_update: dict[str, object],
) -> None:
    """Catch a probe capability that can claim business effects instead of evidence."""
    registry = ActionRegistry(
        predicates=PredicateCatalog({}),
        validators={"evidence": cast(ActionValidator, _validator)},
    )
    registry.register(
        _definition("release.publish", probe_capability="release.probe")
    )
    probe = _definition("release.probe", input_model=ProbeActionInput)
    registry.register(
        replace(probe, spec=probe.spec.model_copy(update=unsafe_update))
    )

    with pytest.raises(RegistryConfigurationError, match="evidence-only"):
        registry.validate_startup()


@pytest.mark.asyncio
@pytest.mark.parametrize("may_have_side_effects", (False, True))
async def test_unclassified_executor_exception_routes_by_side_effect_risk(
    tmp_path: Path, may_have_side_effects: bool
) -> None:
    """Catch a generic executor exception being retried or losing probe authority."""
    capability = "release.publish" if may_have_side_effects else "work.compute"
    definitions: tuple[tuple[str, tuple[SuccessTemplate, ...]], ...] = (
        (capability, (SuccessTemplate(),)),
    )
    if may_have_side_effects:
        definitions += (("release.probe", (SuccessTemplate(),)),)
    spec_options = (
        {
            capability: {
                "may_have_side_effects": True,
                "probe_capability": "release.probe",
            }
        }
        if may_have_side_effects
        else None
    )

    async def raise_unclassified(
        _context: ActionExecutionContext, _parameters: FrozenModel
    ) -> ActionOutcomeEnvelope:
        raise RuntimeError("unclassified provider crash")

    async with _controller_rig(
        tmp_path,
        definitions=definitions,
        patches=(),
        spec_options=spec_options,
        probe_capabilities=(
            frozenset({"release.probe"})
            if may_have_side_effects
            else frozenset()
        ),
    ) as rig:
        definition = rig.registry.get(capability)
        rig.registry._definitions[capability] = replace(
            definition, executor=cast(ActionExecutor, raise_unclassified)
        )
        action, snapshot = await _authorize_one(rig, capability)
        envelope = await rig.dispatcher.execute(
            run_id=rig.run_id,
            action=action,
            snapshot=snapshot,
            attempt=1,
        )

        if not may_have_side_effects:
            assert isinstance(envelope.outcome, PermanentFailure)
            assert envelope.outcome.error_code == "unclassified_action_failure"
            await rig.reconciler.reconcile(rig.run_id)
            assert await rig.ledger.action_status(action.action_id) is ActionStatus.PERMANENT_FAILED
            assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
            assert await rig.ledger.attempt_numbers(action.action_id) == (1,)
            return

        assert isinstance(envelope.outcome, Indeterminate)
        assert envelope.outcome.error_code == "external_side_effect_unclassified"
        assert envelope.outcome.failure_signature == canonical_failure_signature(
            action.capability,
            action.parameters_json,
            "external_side_effect_unclassified",
        )
        rig.executors["release.probe"]._outcomes = deque(
            (
                ProbeResolution(
                    operation_key=envelope.outcome.operation_key,
                    disposition="unknown",
                    evidence_refs=("provider:query",),
                    message="provider cannot classify operation",
                ),
            )
        )
        await rig.reconciler.reconcile(rig.run_id)
        await rig.controller.tick(rig.run_id)

        probes = tuple(
            candidate
            for candidate in await rig.ledger.list_actions(rig.run_id)
            if candidate.capability == "release.probe"
        )
        assert len(probes) == 1
        assert rig.executors[capability].attempt_ids == []
        assert rig.executors["release.probe"].attempt_ids == [1]
        assert await rig.ledger.attempt_numbers(action.action_id) == (1,)


@pytest.mark.asyncio
async def test_resolve_wrong_original_status_blocks_without_overwriting_evidence(
    tmp_path: Path,
) -> None:
    """Catch a stale resolver applying a route to an original attempt no longer indeterminate."""
    operation_key = "publish:wrong-status-1"
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "release.publish",
                (
                    Indeterminate(
                        operation_key=operation_key,
                        error_code="provider_timeout",
                        failure_signature=canonical_failure_signature(
                            "release.publish", "{}", "provider_timeout"
                        ),
                        message="remote result unknown",
                    ),
                ),
            ),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key=operation_key,
                        disposition="succeeded",
                        evidence_refs=("external:release-1",),
                        message="remote release exists",
                    ),
                ),
            ),
        ),
        patches=(),
        spec_options={
            "release.publish": {
                "probe_capability": "release.probe",
                "retryable_codes": ("provider_timeout",),
            }
        },
        probe_capabilities=frozenset({"release.probe"}),
    ) as rig:
        original, snapshot = await _authorize_one(rig, "release.publish")
        await rig.dispatcher.execute(
            run_id=rig.run_id, action=original, snapshot=snapshot, attempt=1
        )
        await rig.reconciler.reconcile(rig.run_id)
        await rig.controller._authorize_pending_probes(rig.run_id)
        probe = next(
            item
            for item in await rig.ledger.list_actions(rig.run_id)
            if item.capability == "release.probe"
        )
        await rig.dispatcher.execute(
            run_id=rig.run_id,
            action=probe,
            snapshot=await rig.ledger.load_snapshot(rig.run_id),
            attempt=1,
        )
        await rig.ledger._db.execute(
            "UPDATE actions SET status = ? WHERE action_id = ?",
            (ActionStatus.RUNNING.value, original.action_id),
        )
        await rig.ledger._db.commit()
        request = ProbeResolutionRequest(
            original_action_id=original.action_id,
            original_attempt=1,
            probe_action_id=probe.action_id,
            probe_attempt=1,
            operation_key=operation_key,
            original_idempotency_key=original.idempotency_key,
            retry_policy_fingerprint=original.retry_policy_fingerprint,
        )

        with pytest.raises(LedgerConflictError, match="probe resolution"):
            await rig.ledger.resolve_indeterminate(request)

        assert await rig.ledger.action_status(original.action_id) is ActionStatus.RUNNING
        assert await rig.ledger.action_status(probe.action_id) is ActionStatus.RUNNING
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        assert await rig.ledger.has_open_incident("probe_resolution_conflict")
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM probe_resolutions", ()
        ) == 0


@pytest.mark.asyncio
async def test_absent_resolution_ignores_current_registry_retry_policy_drift(
    tmp_path: Path,
) -> None:
    """Catch resolution consulting a later catalog instead of the attempt snapshot."""
    operation_key = "publish:registry-drift-1"
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "release.publish",
                (
                    Indeterminate(
                        operation_key=operation_key,
                        error_code="provider_timeout",
                        failure_signature=canonical_failure_signature(
                            "release.publish", "{}", "provider_timeout"
                        ),
                        message="remote result unknown",
                    ),
                ),
            ),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key=operation_key,
                        disposition="absent",
                        evidence_refs=("external:not-found",),
                        message="remote operation absent",
                    ),
                ),
            ),
        ),
        patches=(),
        spec_options={
            "release.publish": {
                "probe_capability": "release.probe",
                "retryable_codes": ("provider_timeout",),
                "max_attempts": 2,
            }
        },
        probe_capabilities=frozenset({"release.probe"}),
    ) as rig:
        original, snapshot = await _authorize_one(rig, "release.publish")
        await rig.dispatcher.execute(
            run_id=rig.run_id, action=original, snapshot=snapshot, attempt=1
        )
        await rig.reconciler.reconcile(rig.run_id)
        definition = rig.registry.get("release.publish")
        rig.registry._definitions["release.publish"] = replace(
            definition,
            spec=definition.spec.model_copy(
                update={
                    "retry_policy": RetryPolicySpec(
                        max_attempts=1,
                        retryable_codes=(),
                        base_delay_s=0,
                        max_delay_s=0,
                    )
                }
            ),
        )

        await rig.controller.tick(rig.run_id)

        assert await rig.ledger.action_status(original.action_id) is ActionStatus.RETRY_WAIT
        assert await rig.ledger.attempt_numbers(original.action_id) == (1,)
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ("probe_capability", "policy_json", "policy_fingerprint"),
)
async def test_resolve_corrupt_probe_or_policy_facts_blocks_atomically(
    tmp_path: Path, corruption: str
) -> None:
    """Catch malformed durable binding/policy facts escaping as an unrecorded exception."""
    operation_key = "publish:corrupt-facts-1"
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "release.publish",
                (
                    Indeterminate(
                        operation_key=operation_key,
                        error_code="provider_timeout",
                        failure_signature=canonical_failure_signature(
                            "release.publish", "{}", "provider_timeout"
                        ),
                        message="remote result unknown",
                    ),
                ),
            ),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key=operation_key,
                        disposition="succeeded",
                        evidence_refs=("external:release-1",),
                        message="remote release exists",
                    ),
                ),
            ),
        ),
        patches=(),
        spec_options={
            "release.publish": {
                "probe_capability": "release.probe",
                "retryable_codes": ("provider_timeout",),
            }
        },
        probe_capabilities=frozenset({"release.probe"}),
    ) as rig:
        original, snapshot = await _authorize_one(rig, "release.publish")
        await rig.dispatcher.execute(
            run_id=rig.run_id, action=original, snapshot=snapshot, attempt=1
        )
        await rig.reconciler.reconcile(rig.run_id)
        await rig.controller._authorize_pending_probes(rig.run_id)
        probe = next(
            item
            for item in await rig.ledger.list_actions(rig.run_id)
            if item.capability == "release.probe"
        )
        await rig.dispatcher.execute(
            run_id=rig.run_id,
            action=probe,
            snapshot=await rig.ledger.load_snapshot(rig.run_id),
            attempt=1,
        )
        if corruption == "probe_capability":
            await rig.ledger._db.execute(
                "UPDATE actions SET capability = ? WHERE action_id = ?",
                ("release.wrong-probe", probe.action_id),
            )
        elif corruption == "policy_json":
            await rig.ledger._db.execute(
                "UPDATE action_attempts SET retry_policy_json = ? "
                "WHERE action_id = ? AND attempt = 1",
                ('{"unexpected":true}', original.action_id),
            )
        else:
            await rig.ledger._db.execute(
                "UPDATE action_attempts SET retry_policy_fingerprint = ? "
                "WHERE action_id = ? AND attempt = 1",
                ("f" * 64, original.action_id),
            )
        await rig.ledger._db.commit()
        request = ProbeResolutionRequest(
            original_action_id=original.action_id,
            original_attempt=1,
            probe_action_id=probe.action_id,
            probe_attempt=1,
            operation_key=operation_key,
            original_idempotency_key=original.idempotency_key,
            retry_policy_fingerprint=original.retry_policy_fingerprint,
        )

        with pytest.raises(LedgerConflictError, match="probe resolution"):
            await rig.ledger.resolve_indeterminate(request)

        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        assert await rig.ledger.has_open_incident("probe_resolution_conflict")
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM probe_resolutions", ()
        ) == 0
        assert await rig.ledger.attempt_numbers(original.action_id) == (1,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "expected_original", "expected_run"),
    (
        ("succeeded", ActionStatus.SUCCEEDED, RunStatus.RUNNING),
        ("absent", ActionStatus.RETRY_WAIT, RunStatus.RUNNING),
        ("unknown", ActionStatus.INDETERMINATE, RunStatus.BLOCKED),
    ),
)
async def test_probe_resolution_atomically_routes_original_attempt(
    tmp_path: Path,
    disposition: str,
    expected_original: ActionStatus,
    expected_run: RunStatus,
) -> None:
    """Catch probe evidence committing without exactly one original-attempt route."""
    operation_key = "publish:resolved-1"
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "release.publish",
                (
                    Indeterminate(
                        operation_key=operation_key,
                        error_code="provider_timeout",
                        failure_signature=canonical_failure_signature(
                            "release.publish", "{}", "provider_timeout"
                        ),
                        message="remote result unknown",
                    ),
                ),
            ),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key=operation_key,
                        disposition=disposition,
                        evidence_refs=("external:release-1",),
                        message="remote release exists",
                    ),
                ),
            ),
        ),
        patches=(),
        spec_options={
            "release.publish": {
                "probe_capability": "release.probe",
                "retryable_codes": ("provider_timeout",),
            }
        },
        probe_capabilities=frozenset({"release.probe"}),
    ) as rig:
        original, snapshot = await _authorize_one(rig, "release.publish")
        await rig.dispatcher.execute(
            run_id=rig.run_id, action=original, snapshot=snapshot, attempt=1
        )
        await rig.reconciler.reconcile(rig.run_id)

        await rig.controller.tick(rig.run_id)

        actions = await rig.ledger.list_actions(rig.run_id)
        probe = next(item for item in actions if item.capability == "release.probe")
        assert await rig.ledger.action_status(probe.action_id) is ActionStatus.SUCCEEDED
        assert await rig.ledger.action_status(original.action_id) is expected_original
        assert (await rig.ledger.get_run(rig.run_id)).status is expected_run
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM probe_resolutions WHERE original_action_id = ?",
            (original.action_id,),
        ) == 1
        assert await rig.ledger.attempt_numbers(original.action_id) == (1,)
        resolution = await rig.ledger.get_probe_resolution(original.action_id, 1)
        replay_request = ProbeResolutionRequest(
            original_action_id=resolution.original_action_id,
            original_attempt=resolution.original_attempt,
            probe_action_id=resolution.probe_action_id,
            probe_attempt=resolution.probe_attempt,
            operation_key=resolution.operation_key,
            original_idempotency_key=resolution.original_idempotency_key,
            retry_policy_fingerprint=resolution.retry_policy_fingerprint,
        )
        assert await rig.ledger.resolve_indeterminate(replay_request) == resolution
        if disposition == "absent":
            assert await rig.ledger.resolve_indeterminate(replay_request) == resolution
            assert await rig.ledger.attempt_numbers(original.action_id) == (1,)
        if disposition == "succeeded":
            await rig.reconciler.reconcile(rig.run_id)
            assert await rig.ledger.action_status(original.action_id) is ActionStatus.SUCCEEDED
            assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retryable_codes", "max_attempts", "reason_code"),
    (
        (("provider_timeout",), 1, "probe_retry_exhausted"),
        (("different_error",), 2, "probe_retry_not_allowed"),
    ),
)
async def test_absent_probe_blocks_when_frozen_retry_policy_disallows_successor(
    tmp_path: Path,
    retryable_codes: tuple[str, ...],
    max_attempts: int,
    reason_code: str,
) -> None:
    """Catch absent evidence bypassing the original attempt's frozen retry limit."""
    operation_key = "publish:absent-denied-1"
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "release.publish",
                (
                    Indeterminate(
                        operation_key=operation_key,
                        error_code="provider_timeout",
                        failure_signature=canonical_failure_signature(
                            "release.publish", "{}", "provider_timeout"
                        ),
                        message="remote result unknown",
                    ),
                ),
            ),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key=operation_key,
                        disposition="absent",
                        evidence_refs=("external:not-found",),
                        message="remote operation is absent",
                    ),
                ),
            ),
        ),
        patches=(),
        spec_options={
            "release.publish": {
                "probe_capability": "release.probe",
                "retryable_codes": retryable_codes,
                "max_attempts": max_attempts,
            }
        },
        probe_capabilities=frozenset({"release.probe"}),
    ) as rig:
        original, snapshot = await _authorize_one(rig, "release.publish")
        await rig.dispatcher.execute(
            run_id=rig.run_id, action=original, snapshot=snapshot, attempt=1
        )
        await rig.reconciler.reconcile(rig.run_id)

        await rig.controller.tick(rig.run_id)

        actions = await rig.ledger.list_actions(rig.run_id)
        probe = next(item for item in actions if item.capability == "release.probe")
        assert await rig.ledger.action_status(original.action_id) is ActionStatus.INDETERMINATE
        assert await rig.ledger.action_status(probe.action_id) is ActionStatus.SUCCEEDED
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        assert await rig.ledger.has_open_incident(reason_code)
        assert await rig.ledger.attempt_numbers(original.action_id) == (1,)
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM probe_resolutions WHERE original_action_id = ?",
            (original.action_id,),
        ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "conflict_field",
    (
        "operation_key",
        "original_attempt",
        "original_idempotency_key",
        "retry_policy_fingerprint",
        "disposition",
        "evidence_refs",
    ),
)
async def test_probe_resolution_replay_preserves_first_fact_and_blocks_conflict(
    tmp_path: Path, conflict_field: str
) -> None:
    """Catch last-writer-wins replacement of an immutable probe resolution."""
    operation_key = "publish:replay-1"
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "release.publish",
                (
                    Indeterminate(
                        operation_key=operation_key,
                        error_code="provider_timeout",
                        failure_signature=canonical_failure_signature(
                            "release.publish", "{}", "provider_timeout"
                        ),
                        message="remote result unknown",
                    ),
                ),
            ),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key=operation_key,
                        disposition="succeeded",
                        evidence_refs=("external:release-1",),
                        message="remote release exists",
                    ),
                ),
            ),
        ),
        patches=(),
        spec_options={
            "release.publish": {
                "probe_capability": "release.probe",
                "retryable_codes": ("provider_timeout",),
            }
        },
        probe_capabilities=frozenset({"release.probe"}),
    ) as rig:
        original, snapshot = await _authorize_one(rig, "release.publish")
        await rig.dispatcher.execute(
            run_id=rig.run_id, action=original, snapshot=snapshot, attempt=1
        )
        await rig.reconciler.reconcile(rig.run_id)
        await rig.controller.tick(rig.run_id)
        probe = next(
            item
            for item in await rig.ledger.list_actions(rig.run_id)
            if item.capability == "release.probe"
        )
        request = ProbeResolutionRequest(
            original_action_id=original.action_id,
            original_attempt=1,
            probe_action_id=probe.action_id,
            probe_attempt=1,
            operation_key=operation_key,
            original_idempotency_key=original.idempotency_key,
            retry_policy_fingerprint=original.retry_policy_fingerprint,
        )
        first = await rig.ledger.resolve_indeterminate(request)
        assert await rig.ledger.resolve_indeterminate(request) == first

        if conflict_field in {"disposition", "evidence_refs"}:
            changed = ProbeResolution(
                operation_key=operation_key,
                disposition=(
                    "unknown" if conflict_field == "disposition" else "succeeded"
                ),
                evidence_refs=(
                    ("external:different",)
                    if conflict_field == "evidence_refs"
                    else ("external:release-1",)
                ),
                message="conflicting provider evidence",
            )
            envelope_json = canonical_model_json(
                ActionOutcomeEnvelope(
                    action_id=probe.action_id, attempt=1, outcome=changed
                )
            )
            await rig.ledger._db.execute(
                "UPDATE attempt_outcome_receipts SET canonical_outcome_json = ?, "
                "outcome_digest = ?, evidence_refs_json = ? "
                "WHERE action_id = ? AND attempt = 1",
                (
                    envelope_json,
                    sha256_canonical_json(envelope_json),
                    json.dumps(changed.evidence_refs, separators=(",", ":")),
                    probe.action_id,
                ),
            )
            await rig.ledger._db.commit()
            conflicting = request
        else:
            update: dict[str, object] = {
                "operation_key": "publish:different",
                "original_attempt": 2,
                "original_idempotency_key": "different-idempotency-key",
                "retry_policy_fingerprint": "f" * 64,
            }
            conflicting = request.model_copy(
                update={conflict_field: update[conflict_field]}
            )

        with pytest.raises(LedgerConflictError, match="immutable first fact"):
            await rig.ledger.resolve_indeterminate(conflicting)

        row = await rig.ledger._fetch_one(
            "SELECT * FROM probe_resolutions WHERE original_action_id = ?",
            (original.action_id,),
        )
        assert row is not None
        assert row["resolution_digest"] == first.resolution_digest
        assert await rig.ledger.action_status(original.action_id) is ActionStatus.SUCCEEDED
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
        assert await rig.ledger.has_open_incident("probe_resolution_conflict")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    (
        "malformed_probe_evidence",
        "probe_wrong_action",
        "probe_wrong_attempt",
        "original_wrong_action",
        "original_wrong_attempt",
        "probe_digest_mismatch",
        "original_digest_mismatch",
        "probe_evidence_mismatch",
        "original_error_mismatch",
        "original_failure_mismatch",
    ),
)
async def test_probe_resolution_durable_receipt_corruption_fails_closed(
    tmp_path: Path, corruption: str
) -> None:
    """Catch malformed or column-divergent receipts authorizing an external resolution."""
    operation_key = "publish:receipt-corruption-1"
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "release.publish",
                (
                    Indeterminate(
                        operation_key=operation_key,
                        error_code="provider_timeout",
                        failure_signature=canonical_failure_signature(
                            "release.publish", "{}", "provider_timeout"
                        ),
                        message="remote result unknown",
                    ),
                ),
            ),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key=operation_key,
                        disposition="succeeded",
                        evidence_refs=("external:release-1",),
                        message="remote release exists",
                    ),
                ),
            ),
        ),
        patches=(),
        spec_options={
            "release.publish": {
                "probe_capability": "release.probe",
                "retryable_codes": ("provider_timeout",),
            }
        },
        probe_capabilities=frozenset({"release.probe"}),
    ) as rig:
        original, snapshot = await _authorize_one(rig, "release.publish")
        await rig.dispatcher.execute(
            run_id=rig.run_id,
            action=original,
            snapshot=snapshot,
            attempt=1,
        )
        await rig.reconciler.reconcile(rig.run_id)
        await rig.controller._authorize_pending_probes(rig.run_id)
        probe = next(
            action
            for action in await rig.ledger.list_actions(rig.run_id)
            if action.capability == "release.probe"
        )
        await rig.dispatcher.execute(
            run_id=rig.run_id,
            action=probe,
            snapshot=await rig.ledger.load_snapshot(rig.run_id),
            attempt=1,
        )
        request = ProbeResolutionRequest(
            original_action_id=original.action_id,
            original_attempt=1,
            probe_action_id=probe.action_id,
            probe_attempt=1,
            operation_key=operation_key,
            original_idempotency_key=original.idempotency_key,
            retry_policy_fingerprint=original.retry_policy_fingerprint,
        )

        target_action = (
            probe.action_id if corruption.startswith("probe_") or corruption.startswith("malformed_probe")
            else original.action_id
        )
        receipt = await rig.ledger._fetch_one(
            "SELECT * FROM attempt_outcome_receipts WHERE action_id = ? AND attempt = 1",
            (target_action,),
        )
        assert receipt is not None
        if corruption == "malformed_probe_evidence":
            await rig.ledger._db.execute(
                "UPDATE attempt_outcome_receipts SET evidence_refs_json = ? "
                "WHERE action_id = ? AND attempt = 1",
                ("not-json", probe.action_id),
            )
        elif corruption.endswith("wrong_action") or corruption.endswith("wrong_attempt"):
            envelope = ActionOutcomeEnvelope.model_validate_json(
                receipt["canonical_outcome_json"]
            )
            changed = envelope.model_copy(
                update={
                    "action_id": (
                        "wrong-action"
                        if corruption.endswith("wrong_action")
                        else envelope.action_id
                    ),
                    "attempt": (
                        2
                        if corruption.endswith("wrong_attempt")
                        else envelope.attempt
                    ),
                }
            )
            changed_json = canonical_model_json(changed)
            await rig.ledger._db.execute(
                "UPDATE attempt_outcome_receipts SET canonical_outcome_json = ?, "
                "outcome_digest = ? WHERE action_id = ? AND attempt = 1",
                (
                    changed_json,
                    sha256_canonical_json(changed_json),
                    target_action,
                ),
            )
        elif corruption.endswith("digest_mismatch"):
            await rig.ledger._db.execute(
                "UPDATE attempt_outcome_receipts SET outcome_digest = ? "
                "WHERE action_id = ? AND attempt = 1",
                ("f" * 64, target_action),
            )
        elif corruption == "probe_evidence_mismatch":
            await rig.ledger._db.execute(
                "UPDATE attempt_outcome_receipts SET evidence_refs_json = ? "
                "WHERE action_id = ? AND attempt = 1",
                (json.dumps(("external:different",)), probe.action_id),
            )
        elif corruption == "original_error_mismatch":
            await rig.ledger._db.execute(
                "UPDATE attempt_outcome_receipts SET error_code = ? "
                "WHERE action_id = ? AND attempt = 1",
                ("different_error", original.action_id),
            )
        else:
            await rig.ledger._db.execute(
                "UPDATE attempt_outcome_receipts SET failure_signature = ? "
                "WHERE action_id = ? AND attempt = 1",
                ("f" * 64, original.action_id),
            )
        await rig.ledger._db.commit()

        with pytest.raises(LedgerConflictError):
            await rig.ledger.resolve_indeterminate(request)

        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM probe_resolutions", ()
        ) == 0
        assert await rig.ledger.action_status(original.action_id) is ActionStatus.INDETERMINATE
        assert await rig.ledger.action_status(probe.action_id) is ActionStatus.RUNNING
        final = await rig.ledger.load_snapshot(rig.run_id)
        assert final.status is RunStatus.BLOCKED
        incidents = tuple(
            incident
            for incident in final.incidents
            if incident.error_code == "probe_resolution_conflict"
        )
        assert len(incidents) == 1
        assert (
            incidents[0].repair_class,
            incidents[0].repair_source,
            incidents[0].reason_code,
        ) == ("integrity", "integrity_guard", "probe_resolution_conflict")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "disposition"),
    (
        ("after_probe_resolution_insert", "succeeded"),
        ("after_probe_success", "succeeded"),
        ("after_original_resolution", "succeeded"),
        ("after_resolution_outbox", "succeeded"),
        ("after_original_resolution", "unknown"),
        ("after_resolution_outbox", "unknown"),
    ),
)
async def test_probe_resolution_crash_rolls_back_the_whole_transaction(
    tmp_path: Path, boundary: str, disposition: str
) -> None:
    """Catch a resolution crash exposing a partial probe/original route."""
    operation_key = "publish:crash-1"
    async with _controller_rig(
        tmp_path,
        definitions=(
            (
                "release.publish",
                (
                    Indeterminate(
                        operation_key=operation_key,
                        error_code="provider_timeout",
                        failure_signature=canonical_failure_signature(
                            "release.publish", "{}", "provider_timeout"
                        ),
                        message="remote result unknown",
                    ),
                ),
            ),
            (
                "release.probe",
                (
                    ProbeResolution(
                        operation_key=operation_key,
                        disposition=disposition,
                        evidence_refs=("external:release-1",),
                        message="remote release exists",
                    ),
                ),
            ),
        ),
        patches=(),
        spec_options={
            "release.publish": {
                "probe_capability": "release.probe",
                "retryable_codes": ("provider_timeout",),
            }
        },
        probe_capabilities=frozenset({"release.probe"}),
    ) as rig:
        original, snapshot = await _authorize_one(rig, "release.publish")
        await rig.dispatcher.execute(
            run_id=rig.run_id, action=original, snapshot=snapshot, attempt=1
        )
        await rig.reconciler.reconcile(rig.run_id)
        await rig.controller._authorize_pending_probes(rig.run_id)
        probe = next(
            item
            for item in await rig.ledger.list_actions(rig.run_id)
            if item.capability == "release.probe"
        )
        await rig.dispatcher.execute(
            run_id=rig.run_id,
            action=probe,
            snapshot=await rig.ledger.load_snapshot(rig.run_id),
            attempt=1,
        )
        request = ProbeResolutionRequest(
            original_action_id=original.action_id,
            original_attempt=1,
            probe_action_id=probe.action_id,
            probe_attempt=1,
            operation_key=operation_key,
            original_idempotency_key=original.idempotency_key,
            retry_policy_fingerprint=original.retry_policy_fingerprint,
        )
        outbox_before = await rig.ledger._count(
            "SELECT COUNT(*) FROM event_outbox", ()
        )
        observed: list[str] = []

        def crash(point: str, _detail: object) -> None:
            observed.append(point)
            if point == boundary:
                raise BoundaryCrash(point)

        rig.ledger._test_hook = crash
        with pytest.raises(BoundaryCrash, match=boundary):
            await rig.ledger.resolve_indeterminate(request)

        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM probe_resolutions", ()
        ) == 0
        assert await rig.ledger.action_status(probe.action_id) is ActionStatus.RUNNING
        assert await rig.ledger.action_status(original.action_id) is ActionStatus.INDETERMINATE
        assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.RUNNING
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM incidents WHERE run_id = ?",
            (rig.run_id,),
        ) == 0
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM event_outbox WHERE event_name = 'action.resolved'",
            (),
        ) == 0
        assert await rig.ledger._count(
            "SELECT COUNT(*) FROM event_outbox", ()
        ) == outbox_before
        assert boundary in observed

        rig.ledger._test_hook = None
        resolved = await rig.ledger.resolve_indeterminate(request)
        assert resolved.disposition == disposition
        assert await rig.ledger.action_status(probe.action_id) is ActionStatus.SUCCEEDED
        assert await rig.ledger.action_status(original.action_id) is (
            ActionStatus.SUCCEEDED
            if disposition == "succeeded"
            else ActionStatus.INDETERMINATE
        )
        assert (await rig.ledger.get_run(rig.run_id)).status is (
            RunStatus.RUNNING if disposition == "succeeded" else RunStatus.BLOCKED
        )
