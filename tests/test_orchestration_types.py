"""Frozen bundle, receipt, repair, and probe contracts."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from abi.actions.builtins.inputs import SpotcheckInput
from abi.types.orchestration import (
    ActionOutcomeEnvelope,
    ArtifactBundle,
    ArtifactBundleEntry,
    ArtifactMetadata,
    AttemptOutcomeReceiptPayload,
    ExpectedArtifact,
    ExpectedArtifactManifest,
    GateArtifactIdentity,
    GateDecision,
    GateReceiptPayload,
    Indeterminate,
    ProbeResolution,
    RepairRequired,
    Succeeded,
    canonical_bundle_json,
    canonical_failure_signature,
    sha256_canonical_json,
)


def _entry(
    canonical: str = "chapters/translated/001.md",
    *,
    staged: str = "state/staging/a1/1/chapters/translated/001.md",
) -> ArtifactBundleEntry:
    return ArtifactBundleEntry(
        staged_relpath=staged,
        canonical_relpath=canonical,
        media_type="text/markdown",
        evidence_role="translation",
        metadata=(ArtifactMetadata(name="chapter", value_json='"001"'),),
    )


def _bundle() -> ArtifactBundle:
    return ArtifactBundle(action_id="a1", attempt=1, entries=(_entry(),))


@pytest.mark.parametrize(
    ("canonical", "staged"),
    (
        ("CHAPTERS/translated/001.md", "state/staging/a1/1/CHAPTERS/translated/001.md"),
        ("chapters/translated/*.md", "state/staging/a1/1/chapters/translated/*.md"),
        ("chapters/translated/001.md", "state/staging/a1/2/chapters/translated/001.md"),
        ("chapters/translated/001.md", "chapters/translated/001.md"),
    ),
)
def test_bundle_rejects_nonportable_or_cross_attempt_paths(
    canonical: str, staged: str
) -> None:
    """Catch outputs escaping the exact authorized attempt namespace."""
    with pytest.raises(ValidationError):
        ArtifactBundle(
            action_id="a1",
            attempt=1,
            entries=(
                ArtifactBundleEntry(
                    staged_relpath=staged,
                    canonical_relpath=canonical,
                    media_type="text/markdown",
                    evidence_role="translation",
                ),
            ),
        )


def test_bundle_rejects_empty_duplicate_and_caller_disordered_entries() -> None:
    """Catch old single-file effects and any implicit effect ordering repair."""
    with pytest.raises(ValidationError):
        ArtifactBundle(action_id="a1", attempt=1, entries=())
    with pytest.raises(ValidationError):
        ArtifactBundle(action_id="a1", attempt=1, entries=(_entry(), _entry()))
    second = _entry(
        canonical="chapters/translated/002.md",
        staged="state/staging/a1/1/chapters/translated/002.md",
    )
    with pytest.raises(ValidationError):
        ArtifactBundle(action_id="a1", attempt=1, entries=(second, _entry()))


def test_metadata_and_expected_manifest_require_exact_order_and_identity() -> None:
    """Catch metadata sorting or manifest duplicates hiding an effect mismatch."""
    with pytest.raises(ValidationError):
        ArtifactBundleEntry(
            staged_relpath="state/staging/a1/1/report.json",
            canonical_relpath="reports/report.json",
            media_type="application/json",
            evidence_role="report",
            metadata=(
                ArtifactMetadata(name="z", value_json="1"),
                ArtifactMetadata(name="a", value_json="2"),
            ),
        )
    with pytest.raises(ValidationError):
        ExpectedArtifactManifest(
            action_id="a1",
            entries=(
                ExpectedArtifact(
                    canonical_relpath="reports/b.json",
                    media_type="application/json",
                    evidence_role="report",
                ),
                ExpectedArtifact(
                    canonical_relpath="reports/a.json",
                    media_type="application/json",
                    evidence_role="report",
                ),
            ),
        )


def test_success_has_only_a_typed_bundle_and_envelope_identity_is_exact() -> None:
    """Catch reintroduction of Succeeded.staging_relpath or mismatched envelope identity."""
    bundle = _bundle()
    envelope = ActionOutcomeEnvelope(
        action_id="a1", attempt=1, outcome=Succeeded(artifact_bundle=bundle)
    )
    assert envelope.outcome.artifact_bundle == bundle
    with pytest.raises(ValidationError):
        Succeeded.model_validate({"staging_relpath": "legacy.md"})
    with pytest.raises(ValidationError):
        ActionOutcomeEnvelope(
            action_id="a2", attempt=1, outcome=Succeeded(artifact_bundle=bundle)
        )


def test_bundle_canonical_json_and_digest_are_stable() -> None:
    """Catch noncanonical serialization weakening durable receipt comparisons."""
    encoded = canonical_bundle_json(_bundle())
    assert encoded == (
        '{"action_id":"a1","attempt":1,"entries":[{"canonical_relpath":'
        '"chapters/translated/001.md","evidence_role":"translation","media_type":'
        '"text/markdown","metadata":[{"name":"chapter","value_json":"\\\"001\\\""}],'
        '"staged_relpath":"state/staging/a1/1/chapters/translated/001.md"}]}'
    )
    assert sha256_canonical_json(encoded) == hashlib.sha256(encoded.encode()).hexdigest()


def test_indeterminate_requires_error_code_and_canonical_failure_signature() -> None:
    """Catch ambiguous external-side-effect failures that cannot be probed safely."""
    signature = canonical_failure_signature("release.prepare", "{}", "provider_timeout")
    assert Indeterminate(
        operation_key="release:v1",
        error_code="provider_timeout",
        failure_signature=signature,
        message="commit result unknown",
    ).failure_signature == signature
    with pytest.raises(ValidationError):
        Indeterminate(
            operation_key="release:v1",
            error_code="provider_timeout",
            failure_signature="bad",
            message="commit result unknown",
        )


@pytest.mark.parametrize("disposition", ("succeeded", "absent", "unknown"))
def test_probe_resolution_parses_every_durable_disposition(disposition: str) -> None:
    """Catch probe evidence being coerced into ordinary artifact success."""
    resolution = ProbeResolution.model_validate(
        {
            "operation_key": "release:v1",
            "disposition": disposition,
            "evidence_refs": ["provider:release:v1"],
            "message": "observed",
        }
    )
    assert resolution.disposition == disposition


def test_repair_required_needs_explicit_class_source_and_reason() -> None:
    """Catch malformed repair outcomes silently defaulting to semantic replanning."""
    assert RepairRequired(
        repair_class="semantic",
        repair_source="action_outcome",
        reason_code="term_drift",
        defect_codes=("term_drift",),
        message="repair glossary",
    ).reason_code == "term_drift"
    with pytest.raises(ValidationError):
        RepairRequired.model_validate(
            {"defect_codes": ["term_drift"], "message": "repair glossary"}
        )


def test_outcome_and_gate_receipts_bind_canonical_json_and_digests() -> None:
    """Catch caller-supplied receipt digests or artifact identities drifting from facts."""
    outcome = Succeeded(artifact_bundle=_bundle(), evidence_refs=("translation",))
    envelope = ActionOutcomeEnvelope(action_id="a1", attempt=1, outcome=outcome)
    outcome_json = json.dumps(
        envelope.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    bundle_json = canonical_bundle_json(_bundle())
    receipt = AttemptOutcomeReceiptPayload(
        action_id="a1",
        attempt=1,
        canonical_outcome_json=outcome_json,
        outcome_digest=sha256_canonical_json(outcome_json),
        canonical_bundle_json=bundle_json,
        bundle_digest=sha256_canonical_json(bundle_json),
        evidence_refs=("translation",),
    )
    assert receipt.bundle_digest == sha256_canonical_json(bundle_json)
    with pytest.raises(ValidationError):
        AttemptOutcomeReceiptPayload.model_validate(
            {**receipt.model_dump(mode="json"), "bundle_digest": "0" * 64}
        )

    gate_json = json.dumps(
        {
            "artifact_checksums": ["a" * 64],
            "bundle_digest": receipt.bundle_digest,
            "evidence_refs": ["translation"],
            "message": "valid",
            "passed": True,
            "reason_code": "evidence_valid",
            "validator_id": "chapter.translate",
            "validator_version": "1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    gate = GateReceiptPayload(
        action_id="a1",
        attempt=1,
        validator_id="chapter.translate",
        validator_version="1",
        canonical_gate_decision_json=gate_json,
        gate_decision_digest=sha256_canonical_json(gate_json),
        bundle_digest=receipt.bundle_digest or "",
        artifacts=(
            GateArtifactIdentity(
                staged_relpath=_entry().staged_relpath,
                canonical_relpath=_entry().canonical_relpath,
                checksum="a" * 64,
            ),
        ),
        evidence_refs=("translation",),
    )
    assert gate.artifacts[0].canonical_relpath == "chapters/translated/001.md"


def test_outcome_receipt_rejects_skeletal_or_outer_fact_drift() -> None:
    bundle = _bundle()
    unrelated = ArtifactBundle(
        action_id="a1",
        attempt=1,
        entries=(
            _entry(
                canonical="chapters/translated/002.md",
                staged="state/staging/a1/1/chapters/translated/002.md",
            ),
        ),
    )
    unrelated_json = canonical_bundle_json(unrelated)
    skeletal = '{"kind":"succeeded"}'
    with pytest.raises(ValidationError):
        AttemptOutcomeReceiptPayload(
            action_id="a1",
            attempt=1,
            canonical_outcome_json=skeletal,
            outcome_digest=sha256_canonical_json(skeletal),
            canonical_bundle_json=unrelated_json,
            bundle_digest=sha256_canonical_json(unrelated_json),
        )

    envelope = ActionOutcomeEnvelope(
        action_id="a1",
        attempt=1,
        outcome=Succeeded(artifact_bundle=bundle, evidence_refs=("translation",)),
    )
    encoded = json.dumps(
        envelope.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(ValidationError):
        AttemptOutcomeReceiptPayload(
            action_id="a1",
            attempt=1,
            canonical_outcome_json=encoded,
            outcome_digest=sha256_canonical_json(encoded),
            canonical_bundle_json=unrelated_json,
            bundle_digest=sha256_canonical_json(unrelated_json),
            evidence_refs=("different",),
        )


def test_gate_receipt_rejects_checksum_evidence_and_staged_namespace_drift() -> None:
    bundle = _bundle()
    digest = sha256_canonical_json(canonical_bundle_json(bundle))
    decision = GateDecision(
        passed=True,
        reason_code="evidence_valid",
        message="valid",
        validator_id="chapter.translate",
        validator_version="1",
        bundle_digest=digest,
        artifact_checksums=("a" * 64,),
        evidence_refs=("translation",),
    )
    encoded = json.dumps(
        decision.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    for checksum, evidence_refs, staged in (
        ("b" * 64, ("translation",), _entry().staged_relpath),
        ("a" * 64, ("different",), _entry().staged_relpath),
        ("a" * 64, ("translation",), "state/staging/a1/2/chapters/translated/001.md"),
    ):
        with pytest.raises(ValidationError):
            GateReceiptPayload(
                action_id="a1",
                attempt=1,
                validator_id="chapter.translate",
                validator_version="1",
                canonical_gate_decision_json=encoded,
                gate_decision_digest=sha256_canonical_json(encoded),
                bundle_digest=digest,
                artifacts=(
                    GateArtifactIdentity(
                        staged_relpath=staged,
                        canonical_relpath=_entry().canonical_relpath,
                        checksum=checksum,
                    ),
                ),
                evidence_refs=evidence_refs,
            )


@pytest.mark.parametrize(
    "reviewers",
    (("agent_a",), ("agent_a", "agent_b", "agent_c"), ("reviewer_a", "reviewer_b")),
)
def test_spotcheck_requires_exact_two_reviewer_protocol(reviewers: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        SpotcheckInput(
            round_id="round_001",
            reviewers=reviewers,
            chapters=("001",),
            samples_per_agent=1,
            seed=1,
        )
