"""Offline tests for the eval pipeline (no network, no LLM)."""

from __future__ import annotations

import asyncio
import random
from datetime import timedelta

import pytest

from abi.eval.align import align_paragraphs, split_paragraphs
from abi.eval.book import (
    _load_glossary,
    check_book_l3,
    eval_book,
    score_book_l2,
)
from abi.eval.calibration import bands_from_calibration, calibrate
from abi.eval.datasets import load_triples, parse_dataset_spec
from abi.eval.judge import LikertOutput, PairwiseOutput, SlotScore, judge_triple
from abi.eval.mechanical import length_ratio_ok, resolve_band, score_paragraph
from abi.eval.report import aggregate_mechanical
from abi.eval.run_facts import load_eval_run_facts
from abi.eval.trace import trace_project
from abi.eval.types import EvalTriple
from abi.project.layout import BookProject
from abi.project.run_ledger import (
    ArtifactCommit,
    ProbeResolutionRequest,
    RunLedger,
    RunSeed,
    SuccessCommit,
)
from abi.types.orchestration import (
    ActionOutcomeEnvelope,
    ActionStatus,
    ArtifactBundle,
    ArtifactBundleEntry,
    AttemptOutcomeReceiptPayload,
    AuthorizedAction,
    CanonicalResolutionEvidence,
    ExpectedArtifact,
    ExpectedArtifactManifest,
    GateArtifactIdentity,
    GateDecision,
    GateEvidence,
    GateReceiptPayload,
    Indeterminate,
    Paused,
    PendingHitlActionReview,
    PendingHitlInterrupt,
    PermanentFailure,
    PlanPatch,
    ProbeActionInput,
    ProbeResolution,
    ProposedAction,
    RepairRequired,
    RetryableFailure,
    RetryPolicySpec,
    RunStatus,
    Succeeded,
    UnblockRequest,
    canonical_bundle_json,
    canonical_failure_signature,
    canonical_manifest_json,
    canonical_model_json,
    sha256_canonical_json,
)


# --- datasets / spec ---
def test_parse_dataset_spec_basic():
    spec = parse_dataset_spec("wmt24pp:en-zh_CN:literary")
    assert spec.adapter == "wmt24pp"
    assert spec.config == "en-zh_CN"
    assert spec.domain == "literary"
    assert spec.source_lang == "en"
    assert spec.target_lang == "zh-CN"


def test_parse_dataset_spec_options():
    spec = parse_dataset_spec("wmt24pp:en-ja_JP:literary:stub=true:limit=2")
    assert spec.stub is True
    assert spec.limit == 2


def test_load_stub_triples():
    spec = parse_dataset_spec("wmt24pp:en-zh_CN:literary:stub=true")
    triples = load_triples(spec)
    assert len(triples) == 5
    assert all(t.reference for t in triples)
    assert triples[0].source_lang == "en"


# --- mechanical ---
def test_length_ratio_ok_in_band():
    # en-zh resolves to the committed calibrated band (WMT24++ literary), which
    # takes precedence over the QUALITY_SCORE default.
    band = resolve_band("en", "zh-Hans")
    assert band.method == "calibrated"
    assert 0.26 <= band.lo <= 0.28 and 0.42 <= band.hi <= 0.44
    assert length_ratio_ok(0.35, band) == 1.0
    assert length_ratio_ok(0.10, band) == 0.2  # well below shoulder
    assert 0.5 <= length_ratio_ok(0.23, band) <= 1.0  # shoulder


def test_packaged_calibrated_bands_loaded():
    from abi.eval.mechanical import packaged_calibrated_bands

    bands = packaged_calibrated_bands()
    assert {"en-zh", "en-ja", "en-es", "en-fr", "en-de"} <= set(bands)
    assert all(b.method == "calibrated" and b.n > 0 for b in bands.values())


def test_explicit_bands_override_packaged():
    from abi.eval.types import LengthBand

    override = {"en-zh": LengthBand(source_target="en-zh", lo=0.1, hi=0.9, method="x")}
    band = resolve_band("en", "zh-Hans", override)
    assert band.lo == 0.1 and band.hi == 0.9


def test_score_paragraph_good_translation():
    s = score_paragraph(
        "All happy families are alike; each unhappy family is unhappy in its own way.",
        "幸福的家庭都是相似的，不幸的家庭各有各的不幸。",
        source_lang="en",
        target_lang="zh-Hans",
    )
    assert s.completeness == 1.0
    assert s.length_ratio_ok == 1.0  # ~0.30 ratio, inside the calibrated en-zh band
    assert s.para_score > 0.9
    assert "low_score" not in s.flags


def test_score_paragraph_preserves_anchors():
    s = score_paragraph(
        "In 1925 he sold 4,000 copies, far more than the 200 he expected to move.",
        "1925 年，他售出了 4,000 册，远超他原本预期的 200 册。",
        source_lang="en",
        target_lang="zh-Hans",
    )
    assert s.anchor_preservation == 1.0  # 1925 / 4,000 / 200 preserved


def test_score_paragraph_empty_is_completeness_fail():
    s = score_paragraph("hello world", None, source_lang="en", target_lang="zh-Hans")
    assert s.completeness == 0.0
    assert s.para_score == 0.0
    assert "completeness_fail" in s.flags


def test_score_paragraph_residue_flagged():
    s = score_paragraph(
        "The cat sat on the mat in the warm afternoon sun.",
        "The cat sat on the mat in the warm afternoon sun.",  # untranslated
        source_lang="en",
        target_lang="zh-Hans",
    )
    assert s.no_refusal_no_residue == 0.0
    assert "untranslated_residue" in s.flags


def test_term_compliance_violation():
    s = score_paragraph(
        "The Party controls the state.",
        "该组织控制国家。",  # "Party" should be 党 per glossary
        source_lang="en",
        target_lang="zh-Hans",
        glossary={"Party": "党"},
    )
    assert s.term_compliance is not None and s.term_compliance < 1.0
    assert "term_drift" in s.flags


# --- alignment ---
def test_align_equal_length():
    src = "Para one.\n\nPara two is here."
    tgt = "段落一。\n\n第二段在这里。"
    al = align_paragraphs(src, tgt)
    assert not al.chapter_align_failed
    assert len(al.pairs) == 2
    assert al.pairs[1].target is not None


def test_align_skips_headings():
    paras = split_paragraphs("# Title\n\nReal paragraph content here.")
    assert paras == ["Real paragraph content here."]


def test_align_large_gap_degrades():
    # 10 source paras vs 1 target: match rate far below threshold -> chapter-level
    # fallback (caller scores the whole chapter), even though NW matches one pair.
    src = "\n\n".join(f"Source paragraph number {i} with text." for i in range(10))
    tgt = "Only one paragraph."
    al = align_paragraphs(src, tgt)
    assert al.chapter_align_failed
    assert sum(p.target is not None for p in al.pairs) <= 1


def test_align_merged_paragraphs_stays_per_paragraph():
    # A realistic small merge (5 -> 4 paras) keeps a high match rate, so per-
    # paragraph alignment is retained (chapter_align_failed stays False).
    src = "\n\n".join(f"Source sentence number {i} with some words." for i in range(5))
    tgt = "\n\n".join(f"目标句子第 {i} 句包含若干词语内容。" for i in range(4))
    al = align_paragraphs(src, tgt)
    assert sum(p.target is not None for p in al.pairs) >= 4


# --- calibration ---
def test_calibrate_stub():
    triples = load_triples(parse_dataset_spec("wmt24pp:en-zh_CN:literary:stub=true"))
    results = calibrate(triples)
    assert len(results) == 1
    r = results[0]
    assert r.band_key == "en-zh"
    assert r.n == 5
    assert r.suggested_lo <= r.ratio_p50 <= r.suggested_hi


def test_bands_min_samples_gate():
    triples = load_triples(parse_dataset_spec("wmt24pp:en-zh_CN:literary:stub=true"))
    results = calibrate(triples)
    assert bands_from_calibration(results, min_samples=50) == {}  # only 5 samples
    bands = bands_from_calibration(results, min_samples=3)
    assert "en-zh" in bands and bands["en-zh"].method == "calibrated"


# --- aggregation ---
def test_aggregate_mechanical():
    triples = load_triples(parse_dataset_spec("wmt24pp:en-zh_CN:literary:stub=true"))
    scores = [
        score_paragraph(t.source, t.reference, source_lang=t.source_lang, target_lang=t.target_lang)
        for t in triples
    ]
    agg = aggregate_mechanical(scores)
    assert agg["n"] == 5
    assert 0.0 <= agg["score_avg"] <= 1.0
    assert agg["completeness"] == 1.0


# --- judge (fake router) ---
class _FakeRouter:
    """Returns canned structured outputs; records call count."""

    def __init__(self) -> None:
        self.calls = 0

    async def invoke_structured(self, schema, messages, **kwargs):
        self.calls += 1
        if schema is LikertOutput:
            return LikertOutput(
                a=SlotScore(adequacy=5, fluency=5, coherence=5, style=5),
                b=SlotScore(adequacy=3, fluency=3, coherence=3, style=3),
            ), None
        return PairwiseOutput(prefer="A", rationale="A is better"), None


@pytest.mark.asyncio
async def test_judge_triple_maps_slots_back():
    router = _FakeRouter()
    triple = EvalTriple(
        paragraph_id="p1",
        source="hello",
        source_lang="en",
        target_lang="zh-Hans",
        abi="你好（abi）",
        baseline="你好（baseline）",
    )
    # Force ABI into slot A by seeding so slot A (the '5' scores, preferred) maps to abi.
    rng = random.Random(0)
    res = await judge_triple(router, triple, rng=rng)
    assert res is not None
    assert router.calls == 2
    # Whichever slot ABI landed in, the higher score + 'A' preference must map
    # consistently: the preferred system gets the slot-A (5,5,5,5) score.
    if res.prefer_system == "abi":
        assert res.abi.adequacy == 5
    else:
        assert res.baseline.adequacy == 5


@pytest.mark.asyncio
async def test_judge_skips_when_missing_side():
    router = _FakeRouter()
    triple = EvalTriple(
        paragraph_id="p1",
        source="hi",
        source_lang="en",
        target_lang="zh-Hans",
        abi="你好",  # no baseline
    )
    assert await judge_triple(router, triple, rng=random.Random(0)) is None
    assert router.calls == 0


# --- whole-book three-plane eval ---
_SRC_CH1 = (
    "All happy families are alike; each unhappy family is unhappy in its own way.\n\n"
    "Everything was in confusion in the Oblonskys' house. The wife had found out "
    "that the husband was carrying on an intrigue with a French girl.\n"
)
_TGT_CH1_GOOD = (
    "幸福的家庭都是相似的，不幸的家庭各有各的不幸。\n\n"
    "奥布隆斯基家里一片混乱。妻子发现丈夫和家里的一个法国女人有暧昧关系。\n"
)


def _make_project(tmp_path, *, translated: bool = True, glossary: bool = True):
    root = tmp_path / "0001_book"
    proj = BookProject(root)
    for d in ("state", "chapters/src", "chapters/translated", "glossary"):
        (root / d).mkdir(parents=True, exist_ok=True)

    async def seed_run() -> None:
        async with RunLedger.open(proj.run_db) as ledger:
            await ledger.create_run(
                RunSeed(
                    run_id="eval-run",
                    book_slug="book",
                    source_lang="en",
                    target_lang="zh-Hans",
                    source_target="en-zh-Hans",
                )
            )

    asyncio.run(seed_run())
    (proj.chapters_src / "001_intro.md").write_text(_SRC_CH1, encoding="utf-8")
    if translated:
        (proj.chapters_translated / "001_intro.md").write_text(_TGT_CH1_GOOD, encoding="utf-8")
    if glossary:
        proj.terms_csv.write_text(
            "term,target,status,display_policy,forbidden_body_renderings,note\n"
            "family,家庭,locked,inline,,\n"
            "intrigue,暧昧,preferred,inline,,\n"
            "ignore_me,X,avoid,inline,,\n",
            encoding="utf-8",
        )
    return proj


def _eval_authorized_action(
    *,
    action_id: str,
    proposal_id: str,
    capability: str,
    artifact: ExpectedArtifact | None = None,
    plan_version: int = 1,
    policy: RetryPolicySpec | None = None,
    parameters_json: str = "{}",
    write_set: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    idempotency_key: str | None = None,
) -> AuthorizedAction:
    """Build only shared legal authorization setup; negative facts stay literal in tests."""
    manifest = ExpectedArtifactManifest(
        action_id=action_id, entries=() if artifact is None else (artifact,)
    )
    frozen_policy = policy or RetryPolicySpec(max_attempts=1)
    return AuthorizedAction(
        action_id=action_id,
        proposal_id=proposal_id,
        plan_version=plan_version,
        capability=capability,
        parameters_json=parameters_json,
        write_set=write_set,
        idempotency_key=idempotency_key or action_id,
        expected_artifact_manifest=manifest,
        expected_artifact_manifest_digest=sha256_canonical_json(
            canonical_manifest_json(manifest)
        ),
        expected_evidence_refs=evidence_refs,
        retry_policy=frozen_policy,
        retry_policy_fingerprint=sha256_canonical_json(
            canonical_model_json(frozen_policy)
        ),
    )


async def _create_authorized_attempt(
    ledger: RunLedger,
    *,
    run_id: str,
    authorized: AuthorizedAction,
    objective: str,
    rationale: str,
) -> object:
    """Create the shared legal run/plan/authorization/start baseline via public APIs."""
    await ledger.create_run(RunSeed(run_id=run_id))
    plan = await ledger.append_plan(
        run_id,
        PlanPatch(
            objective=objective,
            proposed_actions=(
                ProposedAction(
                    proposal_id=authorized.proposal_id,
                    capability=authorized.capability,
                ),
            ),
            rationale=rationale,
        ),
    )
    await ledger.authorize_actions(run_id, (authorized,))
    await ledger.start_attempt(authorized.action_id)
    return plan


async def _seed_committed_gate(proj: BookProject) -> None:
    action_id = "eval-action"
    checksum = "a" * 64
    authorized = _eval_authorized_action(
        action_id=action_id,
        proposal_id="eval-proposal",
        capability="report.build",
        artifact=ExpectedArtifact(
            canonical_relpath="reports/result.json",
            media_type="application/json",
            evidence_role="report",
        ),
        write_set=("reports",),
        evidence_refs=("report",),
    )
    bundle = ArtifactBundle(
        action_id=action_id,
        attempt=1,
        entries=(
            ArtifactBundleEntry(
                staged_relpath="state/staging/eval-action/1/reports/result.json",
                canonical_relpath="reports/result.json",
                media_type="application/json",
                evidence_role="report",
            ),
        ),
    )
    succeeded = Succeeded(artifact_bundle=bundle, evidence_refs=("report",))
    outcome_json = canonical_model_json(
        ActionOutcomeEnvelope(action_id=action_id, attempt=1, outcome=succeeded)
    )
    bundle_json = canonical_bundle_json(bundle)
    bundle_digest = sha256_canonical_json(bundle_json)
    decision = GateDecision(
        passed=True,
        reason_code="evidence_valid",
        message="valid",
        validator_id="report.build",
        validator_version="1",
        bundle_digest=bundle_digest,
        artifact_checksums=(checksum,),
        evidence_refs=("report",),
    )
    decision_json = canonical_model_json(decision)
    async with RunLedger.open(proj.run_db) as ledger:
        await _create_authorized_attempt(
            ledger,
            run_id="eval-run",
            authorized=authorized,
            objective="build report",
            rationale="report is required",
        )
        await ledger.record_attempt_outcome(
            AttemptOutcomeReceiptPayload(
                action_id=action_id,
                attempt=1,
                canonical_outcome_json=outcome_json,
                outcome_digest=sha256_canonical_json(outcome_json),
                canonical_bundle_json=bundle_json,
                bundle_digest=bundle_digest,
                evidence_refs=("report",),
            )
        )
        _, intents = await ledger.create_gate_receipt_and_bundle_intents(
            GateReceiptPayload(
                action_id=action_id,
                attempt=1,
                validator_id="report.build",
                validator_version="1",
                canonical_gate_decision_json=decision_json,
                gate_decision_digest=sha256_canonical_json(decision_json),
                bundle_digest=bundle_digest,
                artifacts=(
                    GateArtifactIdentity(
                        staged_relpath=bundle.entries[0].staged_relpath,
                        canonical_relpath=bundle.entries[0].canonical_relpath,
                        checksum=checksum,
                    ),
                ),
                evidence_refs=("report",),
            )
        )
        await ledger.commit_promotion_intent(intents[0].intent_id)
        await ledger.commit_success(
            SuccessCommit(
                action_id=action_id,
                artifacts=(
                    ArtifactCommit(
                        artifact_id="artifact:result",
                        relpath="reports/result.json",
                        sha256=checksum,
                        producer_action_id=action_id,
                        media_type="application/json",
                    ),
                ),
                gate_evidence=(
                    GateEvidence(
                        evidence_id="gate:report",
                        gate="report.build",
                        passed=True,
                        validator_version="1",
                        artifact_checksums=(checksum,),
                    ),
                ),
            )
        )


def test_l1_replays_committed_gate_evidence_against_ledger_policy(tmp_path) -> None:
    """Catch L1 falsely passing gate evidence whose ordered checksum binding drifted."""
    root = tmp_path / "0001_book"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_committed_gate(proj))
    facts = load_eval_run_facts(proj)
    bad_evidence = facts.snapshot.gate_evidence[0].model_copy(
        update={"artifact_checksums": ("b" * 64,)}
    )
    facts = facts.model_copy(
        update={
            "run": facts.run.model_copy(update={"status": RunStatus.COMPLETED}),
            "snapshot": facts.snapshot.model_copy(
                update={"gate_evidence": (bad_evidence,)}
            )
        }
    )

    report = trace_project(proj, facts=facts)

    assert report.gate_integrity_ok is False
    assert report.gate_integrity[0].consistent is False
    assert "artifact checksums" in report.gate_integrity[0].replay_reason
    assert report.verdict == "FAIL"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("staged_relpath", "state/staging/eval-action/1/reports/other.json"),
        ("media_type", "text/plain"),
        ("evidence_role", "other"),
        ("metadata_json", '{"items":[{"name":"other","value_json":"1"}]}'),
    ),
)
def test_l1_replays_exact_zipped_bundle_gate_and_intent_identity(
    tmp_path, field: str, value: str
) -> None:
    """Catch L1 accepting an intent that drifted from its bundle and gate identity."""
    root = tmp_path / f"intent-{field}"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_committed_gate(proj))
    facts = load_eval_run_facts(proj)
    bad_intent = facts.promotion_intents[0].model_copy(update={field: value})

    report = trace_project(
        proj, facts=facts.model_copy(update={"promotion_intents": (bad_intent,)})
    )

    assert report.path_conformance_ok is False
    assert any("artifact identity" in item for item in report.skipped_states)


def test_l1_requires_passed_committed_gate_evidence(tmp_path) -> None:
    """Catch L1 accepting a committed evidence row whose PASS bit was cleared."""
    root = tmp_path / "gate-evidence-passed"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_committed_gate(proj))
    facts = load_eval_run_facts(proj)
    failed = facts.snapshot.gate_evidence[0].model_copy(update={"passed": False})

    report = trace_project(
        proj,
        facts=facts.model_copy(
            update={
                "snapshot": facts.snapshot.model_copy(update={"gate_evidence": (failed,)})
            }
        ),
    )

    assert report.path_conformance_ok is False
    assert any("committed gate evidence" in item for item in report.skipped_states)


@pytest.mark.asyncio
async def test_run_ledger_exposes_action_bound_committed_gate_evidence(tmp_path) -> None:
    """Catch eval losing the producer Action identity from committed gate evidence."""
    root = tmp_path / "gate-evidence-producer"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    await _seed_committed_gate(proj)
    async with RunLedger.open(proj.run_db) as ledger:
        method = getattr(ledger, "list_committed_gate_evidence", None)
        assert callable(method), "RunLedger needs a typed committed gate-evidence read"
        records = await method("eval-run")
    assert len(records) == 1
    assert records[0].action_id == "eval-action"
    assert records[0].passed is True
    assert len(records[0].gate_decision_digest) == 64
    assert len(records[0].bundle_digest) == 64
    assert records[0].evidence_refs == ("report",)


@pytest.mark.parametrize("tamper", ("missing", "duplicate"))
def test_l1_rejects_completed_ordinary_success_without_exactly_one_gate_chain(
    tmp_path, tamper: str
) -> None:
    """Catch completed ordinary success without one exact gate/intent/commit chain."""
    root = tmp_path / f"ordinary-gate-{tamper}"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_committed_gate(proj))
    facts = load_eval_run_facts(proj)
    receipts = () if tamper == "missing" else facts.gate_receipts * 2

    report = trace_project(
        proj,
        facts=facts.model_copy(
            update={
                "run": facts.run.model_copy(update={"status": RunStatus.COMPLETED}),
                "gate_receipts": receipts,
                "promotion_intents": () if tamper == "missing" else facts.promotion_intents,
            }
        ),
    )

    assert report.path_conformance_ok is False
    assert report.verdict == "FAIL"
    assert any("exactly one gate" in item for item in report.skipped_states)


def test_l1_rejects_succeeded_receipt_with_non_succeeded_attempt_and_missing_gate(
    tmp_path,
) -> None:
    """A succeeded receipt remains gate-authoritative even if a status projection drifts."""
    root = tmp_path / "ordinary-gate-status-drift"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_committed_gate(proj))
    facts = load_eval_run_facts(proj)
    bad_attempt = facts.attempts[0].model_copy(update={"status": ActionStatus.RUNNING})

    report = trace_project(
        proj,
        facts=facts.model_copy(
            update={
                "run": facts.run.model_copy(update={"status": RunStatus.COMPLETED}),
                "attempts": (bad_attempt,),
                "gate_receipts": (),
                "promotion_intents": (),
            }
        ),
    )

    assert report.path_conformance_ok is False
    assert report.verdict == "FAIL"
    assert any("exactly one gate" in item for item in report.skipped_states)


async def _seed_retry_lineage(proj: BookProject) -> None:
    action_id = "retry-action"
    policy = RetryPolicySpec(max_attempts=3, retryable_codes=("provider_timeout",))
    authorized = _eval_authorized_action(
        action_id=action_id,
        proposal_id="retry-proposal",
        capability="report.build",
        artifact=ExpectedArtifact(
            canonical_relpath="reports/retry.json",
            media_type="application/json",
            evidence_role="report",
        ),
        write_set=("reports",),
        evidence_refs=("report",),
        policy=policy,
    )
    failure = RetryableFailure(error_code="provider_timeout", message="retry")
    outcome_json = canonical_model_json(
        ActionOutcomeEnvelope(action_id=action_id, attempt=1, outcome=failure)
    )
    async with RunLedger.open(proj.run_db) as ledger:
        await _create_authorized_attempt(
            ledger,
            run_id="retry-run",
            authorized=authorized,
            objective="retry report",
            rationale="report is required",
        )
        await ledger.record_attempt_outcome(
            AttemptOutcomeReceiptPayload(
                action_id=action_id,
                attempt=1,
                canonical_outcome_json=outcome_json,
                outcome_digest=sha256_canonical_json(outcome_json),
                error_code="provider_timeout",
            )
        )
        await ledger.route_retry_from_receipt(action_id, attempt=1)
        await ledger.create_next_attempt(action_id, previous_attempt=1)


async def _seed_repair_lineage(
    proj: BookProject, *, repair_class: str
) -> tuple[object, object, object | None]:
    run_id = f"{repair_class}-run"
    action_id = f"{repair_class}-action"
    authorized = _eval_authorized_action(
        action_id=action_id,
        proposal_id=f"{repair_class}-proposal",
        capability="report.build",
        artifact=ExpectedArtifact(
            canonical_relpath=f"reports/{repair_class}.json",
            media_type="application/json",
            evidence_role="report",
        ),
        write_set=("reports",),
        evidence_refs=("report",),
    )
    source = "action_outcome" if repair_class == "semantic" else "integrity_guard"
    reason = "term_drift" if repair_class == "semantic" else "artifact_bundle_conflict"
    outcome = RepairRequired(
        repair_class=repair_class,
        repair_source=source,
        reason_code=reason,
        defect_codes=(reason,),
        message="preserve and repair",
    )
    outcome_json = canonical_model_json(
        ActionOutcomeEnvelope(action_id=action_id, attempt=1, outcome=outcome)
    )
    async with RunLedger.open(proj.run_db) as ledger:
        first_plan = await _create_authorized_attempt(
            ledger,
            run_id=run_id,
            authorized=authorized,
            objective="build report",
            rationale="report is required",
        )
        await ledger.record_attempt_outcome(
            AttemptOutcomeReceiptPayload(
                action_id=action_id,
                attempt=1,
                canonical_outcome_json=outcome_json,
                outcome_digest=sha256_canonical_json(outcome_json),
            )
        )
        repair_fact = await ledger.record_repair_required(
            action_id=action_id,
            attempt=1,
            repair_class=repair_class,
            repair_source=source,
            reason_code=reason,
            defect_codes=(reason,),
            evidence_refs=(),
            message="preserve and repair",
            semantic_reason_mapped=repair_class == "semantic",
        )
        if repair_class == "semantic":
            repair_id = "semantic-replacement"
            second_plan = await ledger.append_plan(
                run_id,
                PlanPatch(
                    objective="repair report",
                    proposed_actions=(
                        ProposedAction(
                            proposal_id="semantic-replacement-proposal",
                            capability="report.repair",
                        ),
                    ),
                    superseded_action_ids=(action_id,),
                    rationale="repair the durable semantic defect",
                ),
            )
            await ledger.authorize_actions(
                run_id,
                (
                    _eval_authorized_action(
                        action_id=repair_id,
                        proposal_id="semantic-replacement-proposal",
                        plan_version=2,
                        capability="report.repair",
                        artifact=ExpectedArtifact(
                            canonical_relpath=f"reports/{repair_class}.json",
                            media_type="application/json",
                            evidence_role="report",
                        ),
                        write_set=("reports",),
                        evidence_refs=("report",),
                    ),
                ),
            )
            await ledger.start_attempt(repair_id)
            return (first_plan, second_plan), repair_fact, None
        unblock = await ledger.unblock_run(
            run_id,
            UnblockRequest(
                reason="operator verified the integrity recovery",
                evidence_refs=("ticket-42",),
                source_action_id=action_id,
            ),
        )
        return (first_plan,), repair_fact, unblock


@pytest.mark.parametrize("tamper", ("missing", "late"))
def test_l1_rejects_ordinary_success_without_atomic_promotion_intents(
    tmp_path, tamper: str
) -> None:
    """Catch commit evaluation accepting a missing or post-receipt promotion intent."""
    root = tmp_path / "ordinary"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_committed_gate(proj))
    facts = load_eval_run_facts(proj)
    intents = ()
    if tamper == "late":
        intents = (
            facts.promotion_intents[0].model_copy(
                update={
                    "created_at": facts.gate_receipts[0].recorded_at
                    + timedelta(microseconds=1)
                }
            ),
        )

    report = trace_project(proj, facts=facts.model_copy(update={"promotion_intents": intents}))

    assert report.path_conformance_ok is False
    assert any("promotion intent" in item for item in report.skipped_states)


def test_l1_rejects_retry_successor_with_mutated_frozen_facts_and_reused_staging(
    tmp_path,
) -> None:
    """Catch retry evaluation accepting changed manifest authority or attempt-one staging reuse."""
    root = tmp_path / "retry"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_retry_lineage(proj))
    facts = load_eval_run_facts(proj)
    predecessor, successor = facts.attempts
    bad_successor = successor.model_copy(
        update={
            "expected_manifest_digest": "0" * 64,
            "staging_relpath": predecessor.staging_relpath,
        }
    )

    report = trace_project(
        proj, facts=facts.model_copy(update={"attempts": (predecessor, bad_successor)})
    )

    assert report.path_conformance_ok is False
    assert any("retry successor" in item for item in report.skipped_states)


def test_l1_accepts_valid_retry_successor_after_attempt_two_started(tmp_path) -> None:
    """Keep immutable retry authority valid after its successor advances to RUNNING."""
    root = tmp_path / "retry-started"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_retry_lineage(proj))

    async def start_successor() -> None:
        async with RunLedger.open(proj.run_db) as ledger:
            await ledger.start_attempt("retry-action", attempt=2)

    asyncio.run(start_successor())

    report = trace_project(proj)

    assert report.path_conformance_ok is True, report.skipped_states


def test_l1_accepts_valid_multi_retry_history_after_attempt_three_authorized(
    tmp_path,
) -> None:
    """Replay historical retry successors without equating each to current Action status."""
    root = tmp_path / "retry-multiple"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_retry_lineage(proj))

    async def add_second_retry() -> None:
        failure = RetryableFailure(error_code="provider_timeout", message="retry again")
        outcome_json = canonical_model_json(
            ActionOutcomeEnvelope(
                action_id="retry-action", attempt=2, outcome=failure
            )
        )
        async with RunLedger.open(proj.run_db) as ledger:
            await ledger.start_attempt("retry-action", attempt=2)
            await ledger.record_attempt_outcome(
                AttemptOutcomeReceiptPayload(
                    action_id="retry-action",
                    attempt=2,
                    canonical_outcome_json=outcome_json,
                    outcome_digest=sha256_canonical_json(outcome_json),
                    error_code="provider_timeout",
                )
            )
            await ledger.route_retry_from_receipt("retry-action", attempt=2)
            await ledger.create_next_attempt("retry-action", previous_attempt=2)

    asyncio.run(add_second_retry())

    report = trace_project(proj)

    assert report.path_conformance_ok is True, report.skipped_states


def test_l1_accepts_semantic_repair_history_after_run_later_completed(tmp_path) -> None:
    """A later terminal run projection must not rewrite valid repair history."""
    root = tmp_path / "semantic-later-completed"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_repair_lineage(proj, repair_class="semantic"))
    facts = load_eval_run_facts(proj)

    report = trace_project(
        proj,
        facts=facts.model_copy(
            update={"run": facts.run.model_copy(update={"status": RunStatus.COMPLETED})}
        ),
    )

    assert report.path_conformance_ok is True, report.skipped_states


def test_l1_rejects_malformed_retry_outbox_payload_without_raising(tmp_path) -> None:
    """Malformed durable event JSON must fail closed instead of escaping trace evaluation."""
    root = tmp_path / "retry-malformed-event"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_retry_lineage(proj))
    facts = load_eval_run_facts(proj)
    events = tuple(
        item.model_copy(update={"payload_json": "{"})
        if item.event_name == "action.outcome"
        else item
        for item in facts.outbox_events
    )

    report = trace_project(
        proj, facts=facts.model_copy(update={"outbox_events": events})
    )

    assert report.path_conformance_ok is False
    assert any("outbox" in item and "canonical" in item for item in report.skipped_states)


def test_l1_accepts_explicit_unblock_replacement_with_unrelated_downstream_action(
    tmp_path,
) -> None:
    """Bind integrity recovery to the unblock replacement, not every later Action."""
    root = tmp_path / "integrity-downstream"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_repair_lineage(proj, repair_class="integrity"))

    async def add_downstream() -> None:
        async with RunLedger.open(proj.run_db) as ledger:
            plan = await ledger.append_plan(
                "integrity-run",
                PlanPatch(
                    objective="continue unrelated work",
                    proposed_actions=(
                        ProposedAction(
                            proposal_id="downstream-proposal",
                            capability="report.downstream",
                        ),
                    ),
                    rationale="the explicit replacement already owns recovery",
                ),
            )
            await ledger.authorize_actions(
                "integrity-run",
                (
                    _eval_authorized_action(
                        action_id="downstream-action",
                        proposal_id="downstream-proposal",
                        plan_version=plan.version,
                        capability="report.downstream",
                    ),
                ),
            )

    asyncio.run(add_downstream())

    report = trace_project(proj)

    assert report.path_conformance_ok is True


@pytest.mark.parametrize("tamper", ("digest", "envelope_identity"))
def test_l1_rejects_outcome_receipt_digest_or_envelope_identity_tamper(
    tmp_path, tamper: str
) -> None:
    """Recompute every outcome receipt digest and bind its envelope to the row key."""
    root = tmp_path / f"outcome-{tamper}"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_committed_gate(proj))
    facts = load_eval_run_facts(proj)
    receipt = facts.outcome_receipts[0]
    if tamper == "digest":
        bad_receipt = receipt.model_copy(update={"outcome_digest": "0" * 64})
    else:
        envelope = ActionOutcomeEnvelope.model_validate_json(receipt.canonical_outcome_json)
        bad_json = canonical_model_json(
            envelope.model_copy(update={"action_id": "different-action"})
        )
        bad_receipt = receipt.model_copy(
            update={
                "canonical_outcome_json": bad_json,
                "outcome_digest": sha256_canonical_json(bad_json),
            }
        )

    report = trace_project(
        proj, facts=facts.model_copy(update={"outcome_receipts": (bad_receipt,)})
    )

    assert report.path_conformance_ok is False
    assert any("outcome receipt" in item for item in report.skipped_states)


@pytest.mark.parametrize(
    "tamper", ("ineligible_retry", "repair_digest", "terminal_status")
)
def test_l1_rejects_invalid_outcome_authority_bindings(tmp_path, tamper: str) -> None:
    """Reject ineligible retry, unbound repair, and one-sided terminal success facts."""
    root = tmp_path / f"authority-{tamper}"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    if tamper == "ineligible_retry":
        asyncio.run(_seed_retry_lineage(proj))
        facts = load_eval_run_facts(proj)
        predecessor = facts.outcome_receipts[0]
        failure_json = canonical_model_json(
            ActionOutcomeEnvelope(
                action_id="retry-action",
                attempt=1,
                outcome=PermanentFailure(error_code="fatal", message="not retryable"),
            )
        )
        bad_facts = facts.model_copy(
            update={
                "outcome_receipts": (
                    predecessor.model_copy(
                        update={
                            "canonical_outcome_json": failure_json,
                            "outcome_digest": sha256_canonical_json(failure_json),
                            "error_code": "fatal",
                        }
                    ),
                )
            }
        )
        expected = "eligible RetryableFailure"
    elif tamper == "repair_digest":
        asyncio.run(_seed_repair_lineage(proj, repair_class="semantic"))
        facts = load_eval_run_facts(proj)
        bad_facts = facts.model_copy(
            update={
                "repair_facts": (
                    facts.repair_facts[0].model_copy(
                        update={"outcome_digest": "0" * 64}
                    ),
                )
            }
        )
        expected = "repair fact outcome digest"
    else:
        action_id = "terminal-action"
        authorized = _eval_authorized_action(
            action_id=action_id,
            proposal_id="terminal-proposal",
            capability="report.fail",
        )
        failure_json = canonical_model_json(
            ActionOutcomeEnvelope(
                action_id=action_id,
                attempt=1,
                outcome=PermanentFailure(error_code="fatal", message="failed"),
            )
        )

        async def seed_terminal_failure() -> None:
            async with RunLedger.open(proj.run_db) as ledger:
                await _create_authorized_attempt(
                    ledger,
                    run_id="terminal-run",
                    authorized=authorized,
                    objective="record failure",
                    rationale="exercise terminal authority",
                )
                await ledger.record_attempt_outcome(
                    AttemptOutcomeReceiptPayload(
                        action_id=action_id,
                        attempt=1,
                        canonical_outcome_json=failure_json,
                        outcome_digest=sha256_canonical_json(failure_json),
                        error_code="fatal",
                    )
                )
                await ledger.finish_attempt(
                    action_id, attempt=1, status=ActionStatus.PERMANENT_FAILED
                )

        asyncio.run(seed_terminal_failure())
        facts = load_eval_run_facts(proj)
        bad_facts = facts.model_copy(
            update={
                "actions": (
                    facts.actions[0].model_copy(update={"status": ActionStatus.SUCCEEDED}),
                )
            }
        )
        expected = "terminal status"

    report = trace_project(proj, facts=bad_facts)

    assert report.path_conformance_ok is False
    assert any(expected in item for item in report.skipped_states)


def test_l1_rejects_semantic_repair_without_exactly_one_superseding_action(tmp_path) -> None:
    """Catch semantic repair evaluation accepting zero replacement Actions for its source."""
    root = tmp_path / "semantic"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    plans, repair_fact, _ = asyncio.run(
        _seed_repair_lineage(proj, repair_class="semantic")
    )
    facts = load_eval_run_facts(proj)
    original = next(action for action in facts.actions if action.repair_class == "semantic")
    original_attempt = next(
        attempt for attempt in facts.attempts if attempt.action_id == original.action_id
    )
    broken = facts.model_copy(
        update={
            "actions": (original,),
            "attempts": (original_attempt,),
            "plan_versions": plans,
            "repair_facts": (repair_fact,),
        }
    )

    report = trace_project(proj, facts=broken)

    assert report.path_conformance_ok is False
    assert any("semantic repair" in item for item in report.skipped_states)


def test_l1_rejects_integrity_replacement_without_matching_unblock(tmp_path) -> None:
    """Catch integrity evaluation accepting a replacement Action without human unblock evidence."""
    root = tmp_path / "integrity"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    plans, repair_fact, unblock = asyncio.run(
        _seed_repair_lineage(proj, repair_class="integrity")
    )
    assert unblock is not None
    facts = load_eval_run_facts(proj).model_copy(
        update={
            "plan_versions": plans,
            "repair_facts": (repair_fact,),
            "unblock_resolutions": (),
        }
    )

    report = trace_project(proj, facts=facts)

    assert report.path_conformance_ok is False
    assert any("human unblock" in item for item in report.skipped_states)


@pytest.mark.asyncio
async def test_run_ledger_exposes_typed_eval_lineage_reads(tmp_path) -> None:
    """Catch eval reaching around RunLedger or losing typed plan/repair/unblock lineage facts."""
    root = tmp_path / "typed-reads"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    await _seed_repair_lineage(proj, repair_class="integrity")
    async with RunLedger.open(proj.run_db) as ledger:
        for method_name in (
            "list_plan_versions",
            "list_repair_facts",
            "list_unblock_resolutions",
        ):
            method = getattr(ledger, method_name, None)
            assert callable(method), f"RunLedger.{method_name} must be a public typed read"
            records = await method("integrity-run")
            assert records, f"RunLedger.{method_name} dropped durable lineage records"
            assert all(hasattr(record, "model_dump") for record in records)


@pytest.mark.parametrize("tamper", ("pending_intent", "missing_committed_artifact"))
def test_l1_requires_committed_intents_and_unified_canonical_postcheck(
    tmp_path, tamper: str
) -> None:
    """Catch success accepted before every intent and canonical artifact pass postcheck."""
    root = tmp_path / f"postcheck-{tamper}"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_committed_gate(proj))
    facts = load_eval_run_facts(proj)
    if tamper == "pending_intent":
        facts = facts.model_copy(
            update={
                "promotion_intents": (
                    facts.promotion_intents[0].model_copy(
                        update={"status": "PENDING", "committed_at": None}
                    ),
                )
            }
        )
    else:
        facts = facts.model_copy(
            update={"snapshot": facts.snapshot.model_copy(update={"artifacts": ()})}
        )

    report = trace_project(proj, facts=facts)

    assert report.path_conformance_ok is False
    assert any("postcheck" in item for item in report.skipped_states)


def test_l1_requires_outcome_receipt_before_controller_event(tmp_path) -> None:
    """Catch controller outcome handling recorded before the immutable executor receipt."""
    root = tmp_path / "outcome-order"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_retry_lineage(proj))
    facts = load_eval_run_facts(proj)

    async def load_events():
        async with RunLedger.open(proj.run_db) as ledger:
            return await ledger.undelivered_events("retry-run")

    events = asyncio.run(load_events())
    outcome_event = next(item for item in events if item.event_name == "action.outcome")
    late_receipt = facts.outcome_receipts[0].model_copy(
        update={"recorded_at": outcome_event.created_at + timedelta(microseconds=1)}
    )

    report = trace_project(
        proj,
        facts=facts.model_copy(
            update={"outcome_receipts": (late_receipt,), "outbox_events": events}
        ),
    )

    assert report.path_conformance_ok is False
    assert any("outcome receipt" in item for item in report.skipped_states)


def test_l1_rejects_plan_rejection_without_reason_codes(tmp_path) -> None:
    """Catch Planner rejection history losing deterministic policy feedback."""
    root = tmp_path / "rejection-reasons"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)

    async def seed_rejection() -> None:
        async with RunLedger.open(proj.run_db) as ledger:
            await ledger.create_run(RunSeed(run_id="rejection-run"))
            plan = await ledger.append_plan(
                "rejection-run",
                PlanPatch(
                    objective="reject unsafe action",
                    proposed_actions=(
                        ProposedAction(proposal_id="unsafe", capability="unsafe.action"),
                    ),
                    rationale="exercise policy feedback",
                ),
            )
            await ledger.record_plan_rejection(
                "rejection-run", plan_version=plan.version, reason_codes=("unsafe",)
            )

    asyncio.run(seed_rejection())
    facts = load_eval_run_facts(proj)
    bad_rejection = facts.snapshot.plan_rejections[0].model_copy(
        update={"reason_codes": ()}
    )

    report = trace_project(
        proj,
        facts=facts.model_copy(
            update={
                "snapshot": facts.snapshot.model_copy(
                    update={"plan_rejections": (bad_rejection,)}
                )
            }
        ),
    )

    assert report.path_conformance_ok is False
    assert any("plan rejection" in item for item in report.skipped_states)


def test_l1_rejects_reset_or_reuse_of_conflict_history_after_unblock(tmp_path) -> None:
    """Catch an unblock resetting an immutable CONFLICT intent for reuse."""
    root = tmp_path / "conflict-history"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_committed_gate(proj))

    async def block_and_unblock() -> None:
        async with RunLedger.open(proj.run_db) as ledger:
            await ledger.mark_bundle_conflict(
                "eval-action",
                1,
                reason_code="artifact_checksum_conflict",
                message="canonical artifact drifted",
            )
            await ledger.unblock_run(
                "eval-run",
                UnblockRequest(
                    reason="operator removed the conflicting canonical artifact",
                    evidence_refs=("ticket-42",),
                    source_action_id="eval-action",
                    canonical_resolutions=(
                        CanonicalResolutionEvidence(
                            canonical_relpath="reports/result.json",
                            disposition="removed",
                            evidence_ref="ticket-42",
                        ),
                    ),
                ),
            )

    asyncio.run(block_and_unblock())
    facts = load_eval_run_facts(proj)
    conflict = facts.promotion_intents[0]
    assert conflict.status == "CONFLICT"
    reset = conflict.model_copy(update={"status": "PENDING", "committed_at": None})

    report = trace_project(
        proj,
        facts=facts.model_copy(update={"promotion_intents": (reset,)}),
    )

    assert report.path_conformance_ok is False
    assert any("conflict history" in item for item in report.skipped_states)


def test_l1_rejects_successful_release_with_uncommitted_prerequisite(tmp_path) -> None:
    """Catch release success when a declared prerequisite Action was never committed."""
    root = tmp_path / "release-prerequisite"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_committed_gate(proj))
    facts = load_eval_run_facts(proj)
    release = facts.actions[0].model_copy(
        update={"capability": "release.publish", "dependencies": ("missing-prerequisite",)}
    )

    report = trace_project(proj, facts=facts.model_copy(update={"actions": (release,)}))

    assert report.path_conformance_ok is False
    assert any("release prerequisite" in item for item in report.skipped_states)


def test_l1_rejects_non_probe_failure_mislabeled_as_success(tmp_path) -> None:
    """Catch a failed non-probe outcome being rewritten to SUCCEEDED."""
    root = tmp_path / "failed-as-success"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    action_id = "failed-action"
    manifest = ExpectedArtifactManifest(action_id=action_id)
    policy = RetryPolicySpec(max_attempts=1)
    authorized = AuthorizedAction(
        action_id=action_id,
        proposal_id="failed-proposal",
        plan_version=1,
        capability="report.fail",
        parameters_json="{}",
        idempotency_key=action_id,
        expected_artifact_manifest=manifest,
        expected_artifact_manifest_digest=sha256_canonical_json(
            canonical_manifest_json(manifest)
        ),
        retry_policy=policy,
        retry_policy_fingerprint=sha256_canonical_json(canonical_model_json(policy)),
    )
    failure = PermanentFailure(error_code="fatal_provider_error", message="failed")
    outcome_json = canonical_model_json(
        ActionOutcomeEnvelope(action_id=action_id, attempt=1, outcome=failure)
    )

    async def seed_failure() -> None:
        async with RunLedger.open(proj.run_db) as ledger:
            await ledger.create_run(RunSeed(run_id="failed-run"))
            await ledger.append_plan(
                "failed-run",
                PlanPatch(
                    objective="exercise failure routing",
                    proposed_actions=(
                        ProposedAction(
                            proposal_id="failed-proposal", capability="report.fail"
                        ),
                    ),
                    rationale="failure is expected",
                ),
            )
            await ledger.authorize_actions("failed-run", (authorized,))
            await ledger.start_attempt(action_id)
            await ledger.record_attempt_outcome(
                AttemptOutcomeReceiptPayload(
                    action_id=action_id,
                    attempt=1,
                    canonical_outcome_json=outcome_json,
                    outcome_digest=sha256_canonical_json(outcome_json),
                    error_code="fatal_provider_error",
                )
            )
            await ledger.finish_attempt(
                action_id, attempt=1, status=ActionStatus.PERMANENT_FAILED
            )

    asyncio.run(seed_failure())
    facts = load_eval_run_facts(proj)
    bad_action = facts.actions[0].model_copy(update={"status": ActionStatus.SUCCEEDED})
    bad_attempt = facts.attempts[0].model_copy(update={"status": ActionStatus.SUCCEEDED})

    report = trace_project(
        proj,
        facts=facts.model_copy(update={"actions": (bad_action,), "attempts": (bad_attempt,)}),
    )

    assert report.path_conformance_ok is False
    assert any("non-probe outcome" in item for item in report.skipped_states)


async def _seed_probe_lineage(proj: BookProject, disposition: str):
    run_id = f"probe-{disposition}-run"
    original_id = f"probe-{disposition}-original"
    operation_key = f"remote:{disposition}"
    policy = RetryPolicySpec(max_attempts=2, retryable_codes=("provider_timeout",))
    authorized = _eval_authorized_action(
        action_id=original_id,
        proposal_id="remote-proposal",
        capability="remote.publish",
        policy=policy,
        idempotency_key=f"operation:{disposition}",
    )
    signature = canonical_failure_signature("remote.publish", "{}", "provider_timeout")
    indeterminate = Indeterminate(
        operation_key=operation_key,
        error_code="provider_timeout",
        failure_signature=signature,
        message="remote result unknown",
    )
    indeterminate_json = canonical_model_json(
        ActionOutcomeEnvelope(action_id=original_id, attempt=1, outcome=indeterminate)
    )
    binding = ProbeActionInput(
        original_action_id=original_id,
        original_attempt=1,
        operation_key=operation_key,
        probe_capability="remote.probe",
    )
    probe_id = f"probe-{disposition}-action"
    probe_action = _eval_authorized_action(
        action_id=probe_id,
        proposal_id="probe-proposal",
        plan_version=2,
        capability="remote.probe",
        parameters_json=binding.model_dump_json(),
    )
    async with RunLedger.open(proj.run_db) as ledger:
        await _create_authorized_attempt(
            ledger,
            run_id=run_id,
            authorized=authorized,
            objective="publish remotely",
            rationale="publication is required",
        )
        await ledger.record_attempt_outcome(
            AttemptOutcomeReceiptPayload(
                action_id=original_id,
                attempt=1,
                canonical_outcome_json=indeterminate_json,
                outcome_digest=sha256_canonical_json(indeterminate_json),
                error_code="provider_timeout",
                failure_signature=signature,
            )
        )
        await ledger.finish_attempt(
            original_id,
            attempt=1,
            status=ActionStatus.INDETERMINATE,
            failure_signature=signature,
        )
        await ledger.authorize_probe_action(
            run_id,
            binding=binding,
            patch=PlanPatch(
                objective="resolve remote result",
                proposed_actions=(
                    ProposedAction(proposal_id="probe-proposal", capability="remote.probe"),
                ),
                rationale="probe before retry",
            ),
            action=probe_action,
            expected_previous_plan_version=1,
        )
        await ledger.start_attempt(probe_id)
        probe_outcome = ProbeResolution(
            operation_key=operation_key,
            disposition=disposition,
            evidence_refs=(f"probe:{disposition}",),
            message=f"remote operation {disposition}",
        )
        probe_json = canonical_model_json(
            ActionOutcomeEnvelope(action_id=probe_id, attempt=1, outcome=probe_outcome)
        )
        await ledger.record_attempt_outcome(
            AttemptOutcomeReceiptPayload(
                action_id=probe_id,
                attempt=1,
                canonical_outcome_json=probe_json,
                outcome_digest=sha256_canonical_json(probe_json),
                evidence_refs=(f"probe:{disposition}",),
            )
        )
        resolution = await ledger.resolve_indeterminate(
            ProbeResolutionRequest(
                original_action_id=original_id,
                original_attempt=1,
                probe_action_id=probe_id,
                probe_attempt=1,
                operation_key=operation_key,
                original_idempotency_key=f"operation:{disposition}",
                retry_policy_fingerprint=authorized.retry_policy_fingerprint,
            )
        )
        if disposition == "absent":
            await ledger.create_next_attempt(original_id, previous_attempt=1)
        return resolution


@pytest.mark.parametrize("disposition", ("succeeded", "absent", "unknown"))
def test_l1_rejects_probe_resolution_that_breaks_original_authority(
    tmp_path, disposition: str
) -> None:
    """Catch probe routing that loses immutable outcome/error/policy binding or safe disposition."""
    root = tmp_path / f"probe-{disposition}"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    resolution = asyncio.run(_seed_probe_lineage(proj, disposition))
    facts = load_eval_run_facts(proj)
    if disposition == "succeeded":
        bad_resolution = resolution.model_copy(update={"error_code": "different_error"})
        bad_run = facts.run
    elif disposition == "absent":
        bad_resolution = resolution.model_copy(
            update={"retry_policy_fingerprint": "0" * 64}
        )
        bad_run = facts.run
    else:
        bad_resolution = resolution
        bad_run = facts.run.model_copy(update={"status": RunStatus.RUNNING})

    report = trace_project(
        proj,
        facts=facts.model_copy(
            update={"run": bad_run, "probe_resolutions": (bad_resolution,)}
        ),
    )

    assert report.path_conformance_ok is False
    assert any("probe resolution" in item for item in report.skipped_states)


def test_l1_rejects_probe_action_masquerading_as_ordinary_success(tmp_path) -> None:
    """Catch evidence-only ProbeResolution being replaced by an ungated artifact success."""
    root = tmp_path / "probe-ordinary-success"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    resolution = asyncio.run(_seed_probe_lineage(proj, "succeeded"))
    facts = load_eval_run_facts(proj)
    probe_receipt = next(
        item for item in facts.outcome_receipts if item.action_id == resolution.probe_action_id
    )
    bundle = ArtifactBundle(
        action_id=resolution.probe_action_id,
        attempt=1,
        entries=(
            ArtifactBundleEntry(
                staged_relpath=(
                    f"state/staging/{resolution.probe_action_id}/1/reports/probe.json"
                ),
                canonical_relpath="reports/probe.json",
                media_type="application/json",
                evidence_role="report",
            ),
        ),
    )
    envelope_json = canonical_model_json(
        ActionOutcomeEnvelope(
            action_id=resolution.probe_action_id,
            attempt=1,
            outcome=Succeeded(artifact_bundle=bundle, evidence_refs=("report",)),
        )
    )
    bundle_json = canonical_bundle_json(bundle)
    bad_receipt = probe_receipt.model_copy(
        update={
            "canonical_outcome_json": envelope_json,
            "outcome_digest": sha256_canonical_json(envelope_json),
            "canonical_bundle_json": bundle_json,
            "bundle_digest": sha256_canonical_json(bundle_json),
            "evidence_refs": ("report",),
        }
    )
    receipts = tuple(
        bad_receipt if item.action_id == resolution.probe_action_id else item
        for item in facts.outcome_receipts
    )

    report = trace_project(
        proj,
        facts=facts.model_copy(
            update={"outcome_receipts": receipts, "probe_resolutions": (resolution,)}
        ),
    )

    assert report.path_conformance_ok is False
    assert any("probe" in item and "ordinary success" in item for item in report.skipped_states)


async def _seed_hitl_continuation(
    proj: BookProject, *, block_started: bool = False, complete_success: bool = False
):
    action_id = "hitl-action"
    manifest = ExpectedArtifactManifest(
        action_id=action_id,
        entries=(
            ExpectedArtifact(
                canonical_relpath="reports/hitl.json",
                media_type="application/json",
                evidence_role="report",
            ),
        ),
    )
    policy = RetryPolicySpec(max_attempts=1)
    authorized = AuthorizedAction(
        action_id=action_id,
        proposal_id="hitl-proposal",
        plan_version=1,
        capability="report.hitl",
        parameters_json="{}",
        idempotency_key=action_id,
        expected_artifact_manifest=manifest,
        expected_artifact_manifest_digest=sha256_canonical_json(
            canonical_manifest_json(manifest)
        ),
        retry_policy=policy,
        retry_policy_fingerprint=sha256_canonical_json(canonical_model_json(policy)),
    )
    paused = Paused(
        reason="hitl",
        message="approval required",
        pending_hitl_interrupts=(
            PendingHitlInterrupt(
                interrupt_id="public-interrupt",
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
    paused_json = canonical_model_json(
        ActionOutcomeEnvelope(action_id=action_id, attempt=1, outcome=paused)
    )
    bundle = ArtifactBundle(
        action_id=action_id,
        attempt=1,
        entries=(
            ArtifactBundleEntry(
                staged_relpath="state/staging/hitl-action/1/reports/hitl.json",
                canonical_relpath="reports/hitl.json",
                media_type="application/json",
                evidence_role="report",
            ),
        ),
    )
    async with RunLedger.open(proj.run_db) as ledger:
        await ledger.create_run(RunSeed(run_id="hitl-run"))
        await ledger.append_plan(
            "hitl-run",
            PlanPatch(
                objective="run approved report",
                proposed_actions=(
                    ProposedAction(proposal_id="hitl-proposal", capability="report.hitl"),
                ),
                rationale="approval is required",
            ),
        )
        await ledger.authorize_actions("hitl-run", (authorized,))
        await ledger.start_attempt(action_id)
        original = await ledger.record_attempt_outcome(
            AttemptOutcomeReceiptPayload(
                action_id=action_id,
                attempt=1,
                canonical_outcome_json=paused_json,
                outcome_digest=sha256_canonical_json(paused_json),
            )
        )
        await ledger.finish_attempt(action_id, attempt=1, status=ActionStatus.PAUSED)
        await ledger.set_run_status("hitl-run", RunStatus.PAUSED_HITL)
        await ledger.claim_hitl_interrupt(
            run_id="hitl-run",
            action_id=action_id,
            attempt=1,
            interrupt_id="public-interrupt",
            decisions=("approve",),
            feedback=(None,),
        )
        await ledger.start_hitl_resume("public-interrupt")
        if block_started:
            incident = await ledger.block_hitl_resume_indeterminate(
                "public-interrupt", message="provider resume result is unknown"
            )
            return original, incident, None
        continuation = await ledger.record_hitl_continuation(
            "public-interrupt",
            ActionOutcomeEnvelope(
                action_id=action_id,
                attempt=1,
                outcome=Succeeded(artifact_bundle=bundle, evidence_refs=("report",)),
            ),
        )
        if complete_success:
            checksum = "c" * 64
            bundle_json = canonical_bundle_json(bundle)
            bundle_digest = sha256_canonical_json(bundle_json)
            decision = GateDecision(
                passed=True,
                reason_code="evidence_valid",
                message="valid",
                validator_id="report.hitl",
                validator_version="1",
                bundle_digest=bundle_digest,
                artifact_checksums=(checksum,),
                evidence_refs=("report",),
            )
            decision_json = canonical_model_json(decision)
            _, intents = await ledger.create_gate_receipt_and_bundle_intents(
                GateReceiptPayload(
                    action_id=action_id,
                    attempt=1,
                    validator_id="report.hitl",
                    validator_version="1",
                    canonical_gate_decision_json=decision_json,
                    gate_decision_digest=sha256_canonical_json(decision_json),
                    bundle_digest=bundle_digest,
                    artifacts=(
                        GateArtifactIdentity(
                            staged_relpath=bundle.entries[0].staged_relpath,
                            canonical_relpath=bundle.entries[0].canonical_relpath,
                            checksum=checksum,
                        ),
                    ),
                    evidence_refs=("report",),
                )
            )
            await ledger.commit_promotion_intent(intents[0].intent_id)
            await ledger.commit_success(
                SuccessCommit(
                    action_id=action_id,
                    artifacts=(
                        ArtifactCommit(
                            artifact_id="artifact:hitl",
                            relpath="reports/hitl.json",
                            sha256=checksum,
                            producer_action_id=action_id,
                            media_type="application/json",
                        ),
                    ),
                    gate_evidence=(
                        GateEvidence(
                            evidence_id="gate:hitl",
                            gate="report.hitl",
                            passed=True,
                            validator_version="1",
                            artifact_checksums=(checksum,),
                        ),
                    ),
                )
            )
        effective = await ledger.get_effective_attempt_outcome(action_id, 1)
        return original, continuation, effective


def test_l1_hitl_continuation_preserves_pause_and_cannot_skip_gate_authority(tmp_path) -> None:
    """Catch HITL effective success rewriting Paused or bypassing gate and promotion receipts."""
    root = tmp_path / "hitl"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    original, continuation, effective = asyncio.run(_seed_hitl_continuation(proj))
    facts = load_eval_run_facts(proj)
    assert isinstance(
        ActionOutcomeEnvelope.model_validate_json(original.canonical_outcome_json).outcome,
        Paused,
    )
    assert continuation.sequence == 1
    assert effective.source == "hitl_continuation"

    report = trace_project(
        proj,
        facts=facts.model_copy(
            update={
                "hitl_continuations": (continuation,),
                "effective_outcomes": (effective,),
            }
        ),
    )

    assert report.path_conformance_ok is False
    assert any("HITL" in item for item in report.skipped_states)


def test_l1_replays_gate_against_effective_hitl_continuation_success(tmp_path) -> None:
    """Catch gate replay incorrectly reading immutable Paused instead of effective success."""
    root = tmp_path / "hitl-effective-success"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    original, continuation, effective = asyncio.run(
        _seed_hitl_continuation(proj, complete_success=True)
    )
    facts = load_eval_run_facts(proj)

    report = trace_project(
        proj,
        facts=facts.model_copy(
            update={
                "hitl_continuations": (continuation,),
                "effective_outcomes": (effective,),
            }
        ),
    )

    assert isinstance(
        ActionOutcomeEnvelope.model_validate_json(original.canonical_outcome_json).outcome,
        Paused,
    )
    assert report.gate_integrity_ok is True
    assert report.path_conformance_ok is True


@pytest.mark.parametrize("tamper", ("digest", "envelope_identity"))
def test_l1_rejects_effective_hitl_digest_or_envelope_identity_tamper(
    tmp_path, tamper: str
) -> None:
    """Validate each continuation/effective receipt, not just equality between two rows."""
    root = tmp_path / f"hitl-effective-{tamper}"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    _, continuation, effective = asyncio.run(
        _seed_hitl_continuation(proj, complete_success=True)
    )
    facts = load_eval_run_facts(proj)
    if tamper == "digest":
        bad_continuation = continuation.model_copy(update={"outcome_digest": "0" * 64})
        bad_effective = effective.model_copy(update={"outcome_digest": "0" * 64})
    else:
        envelope = ActionOutcomeEnvelope.model_validate_json(
            continuation.canonical_outcome_json
        )
        assert isinstance(envelope.outcome, Succeeded)
        original_entry = envelope.outcome.artifact_bundle.entries[0]
        changed_bundle = envelope.outcome.artifact_bundle.model_copy(
            update={
                "action_id": "different-action",
                "entries": (
                    original_entry.model_copy(
                        update={
                            "staged_relpath": (
                                "state/staging/different-action/1/reports/hitl.json"
                            )
                        }
                    ),
                ),
            }
        )
        bad_json = canonical_model_json(
            envelope.model_copy(
                update={
                    "action_id": "different-action",
                    "outcome": envelope.outcome.model_copy(
                        update={"artifact_bundle": changed_bundle}
                    ),
                }
            )
        )
        bad_digest = sha256_canonical_json(bad_json)
        bad_continuation = continuation.model_copy(
            update={"canonical_outcome_json": bad_json, "outcome_digest": bad_digest}
        )
        bad_effective = effective.model_copy(
            update={"canonical_outcome_json": bad_json, "outcome_digest": bad_digest}
        )

    report = trace_project(
        proj,
        facts=facts.model_copy(
            update={
                "hitl_continuations": (bad_continuation,),
                "effective_outcomes": (bad_effective,),
            }
        ),
    )

    assert report.gate_integrity_ok is False
    assert report.path_conformance_ok is False
    assert any("HITL" in item and "digest" in item for item in report.skipped_states)


def test_l1_rejects_started_hitl_indeterminate_that_does_not_remain_blocked(tmp_path) -> None:
    """Catch a STARTED unresolved HITL resume being reopened instead of integrity-blocked."""
    root = tmp_path / "hitl-indeterminate"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    original, incident, _ = asyncio.run(
        _seed_hitl_continuation(proj, block_started=True)
    )
    facts = load_eval_run_facts(proj)
    assert incident.repair_class == "integrity"
    bad_run = facts.run.model_copy(update={"status": RunStatus.RUNNING})

    report = trace_project(
        proj,
        facts=facts.model_copy(
            update={"run": bad_run, "hitl_initial_receipts": (original,)}
        ),
    )

    assert report.path_conformance_ok is False
    assert any("HITL" in item and "BLOCKED" in item for item in report.skipped_states)


def test_l1_rejects_unblock_whose_evidence_digest_or_replacement_identity_drifted(
    tmp_path,
) -> None:
    """Catch integrity unblock evaluation accepting changed evidence or replacement identity."""
    root = tmp_path / "unblock-binding"
    (root / "state").mkdir(parents=True)
    proj = BookProject(root)
    asyncio.run(_seed_repair_lineage(proj, repair_class="integrity"))
    facts = load_eval_run_facts(proj)
    unblock = facts.unblock_resolutions[0]
    changed_request = unblock.request.model_copy(
        update={"evidence_refs": ("different-ticket",)}
    )
    bad_unblock = unblock.model_copy(update={"request": changed_request})

    report = trace_project(
        proj,
        facts=facts.model_copy(update={"unblock_resolutions": (bad_unblock,)}),
    )

    assert report.path_conformance_ok is False
    assert any("unblock evidence" in item for item in report.skipped_states)


def test_l1_rejects_manual_unblock_attached_to_semantic_repair(tmp_path) -> None:
    """Catch semantic repair evaluation accepting a human unblock bypass."""
    integrity_root = tmp_path / "integrity-source"
    semantic_root = tmp_path / "semantic-target"
    (integrity_root / "state").mkdir(parents=True)
    (semantic_root / "state").mkdir(parents=True)
    integrity = BookProject(integrity_root)
    semantic = BookProject(semantic_root)
    asyncio.run(_seed_repair_lineage(integrity, repair_class="integrity"))
    asyncio.run(_seed_repair_lineage(semantic, repair_class="semantic"))
    foreign_unblock = load_eval_run_facts(integrity).unblock_resolutions[0]
    facts = load_eval_run_facts(semantic)
    semantic_action = next(action for action in facts.actions if action.repair_class == "semantic")
    bad_unblock = foreign_unblock.model_copy(
        update={"run_id": facts.run.run_id, "source_action_id": semantic_action.action_id}
    )

    report = trace_project(
        semantic,
        facts=facts.model_copy(update={"unblock_resolutions": (bad_unblock,)}),
    )

    assert report.path_conformance_ok is False
    assert any("semantic repair" in item and "unblock" in item for item in report.skipped_states)


def test_load_glossary_only_enforced(tmp_path):
    proj = _make_project(tmp_path)
    g = _load_glossary(proj)
    assert g == {"family": "家庭", "intrigue": "暧昧"}  # 'avoid' row excluded


def test_score_book_l2_good_translation(tmp_path):
    proj = _make_project(tmp_path)
    rep = score_book_l2(proj, source_lang="en", target_lang="zh-Hans")
    assert rep.n_chapters == 1
    assert rep.n_chapters_translated == 1
    assert rep.n_paragraphs >= 1
    assert rep.completeness == 1.0
    assert rep.glossary_terms == 2
    assert rep.verdict in {"PASS", "WARN"}
    assert "completeness_fail" not in rep.flag_counts


def test_score_book_l2_missing_translation_fails(tmp_path):
    proj = _make_project(tmp_path, translated=False)
    rep = score_book_l2(proj, source_lang="en", target_lang="zh-Hans")
    assert rep.n_chapters_translated == 0
    assert rep.completeness == 0.0
    assert rep.verdict == "FAIL"
    assert rep.chapters[0].translated_missing is True


def test_check_book_l3_no_epub_fails(tmp_path):
    proj = _make_project(tmp_path)
    l3 = check_book_l3(proj)
    assert l3.epub.epub_built is False
    assert l3.verdict == "FAIL"
    assert l3.spotcheck.ran is False


def test_eval_book_rolls_up_worst_plane(tmp_path):
    proj = _make_project(tmp_path)
    report = eval_book(proj)
    # L3 has no EPUB -> FAIL, so the combined verdict must be FAIL.
    assert report.l3.verdict == "FAIL"
    assert report.verdict == "FAIL"
    assert report.l2.source_lang == "en"
    assert report.book == "0001_book"
