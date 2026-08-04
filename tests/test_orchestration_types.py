"""Contracts for constrained dynamic orchestration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from abi.types.orchestration import (
    ActionArgument,
    ActionOutcomeEnvelope,
    PermanentFailure,
    PlanPatch,
    ProposedAction,
    RunStatus,
)


def test_plan_patch_is_frozen_and_rejects_unknown_fields() -> None:
    """Catch mutable or permissive planner proposals at the boundary."""
    patch = PlanPatch(
        objective="ingest source",
        proposed_actions=(
            ProposedAction(
                proposal_id="p1",
                capability="source.ingest",
                arguments=(
                    ActionArgument(name="source_relpath", value_json='"source/raw.txt"'),
                ),
                dependencies=(),
                expected_evidence=("source_manifest",),
                priority=100,
            ),
        ),
        rationale="source evidence is absent",
    )

    with pytest.raises(ValidationError):
        patch.objective = "mutated"  # type: ignore[misc]

    with pytest.raises(ValidationError):
        PlanPatch(
            objective="ingest source",
            proposed_actions=(),
            rationale="source evidence is absent",
            unexpected=True,
        )


def test_action_outcome_is_discriminated() -> None:
    """Catch outcome parsing that loses permanent-failure semantics."""
    envelope = ActionOutcomeEnvelope.model_validate(
        {
            "outcome": {
                "kind": "permanent_failure",
                "error_code": "unsupported_format",
                "message": "convert the source to txt or epub",
            }
        }
    )

    assert isinstance(envelope.outcome, PermanentFailure)
    assert RunStatus.BLOCKED.value == "BLOCKED"
