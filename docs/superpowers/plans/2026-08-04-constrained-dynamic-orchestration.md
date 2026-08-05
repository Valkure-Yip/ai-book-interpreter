# Constrained Dynamic Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ABI's fixed `HAPPY_PATH`/28-state macro workflow with a policy-gated Planner, durable Action loop, transactional run ledger, typed failure semantics, and resumable Action harness.

**Architecture:** A structured-output Planner proposes short `PlanPatch` objects from compressed `RunSnapshot` evidence. A deterministic `PolicyEngine` authorizes only registered, eligible Actions; a durable controller dispatches them, validates staging artifacts, and records business facts in SQLite before replanning. LangGraph checkpoints runtime cursors and Action conversations, while `state/run.db` remains the only business source of truth.

**Tech Stack:** Python 3.11+, Pydantic 2, SQLite/aiosqlite, LangChain 1.x `create_agent`, LangGraph 1.x, `langgraph-checkpoint-sqlite` 3.x, Typer, pytest/pytest-asyncio, Ruff, mypy.

> **Authorized breaker amendment (2026-08-05):** The artifact-bundle, attempt-scoped staging,
> staging-aware validation, multi-intent commit, and typed probe-resolution protocol below replaces
> the earlier Tasks 1/4/7/8/9 contract. Do not preserve the old single-file `Succeeded` shape or
> compatibility with artifacts, tasks, attempts, or state created from the superseded draft.
>
> **Recovery closure fix round 1 (2026-08-05):** Before executing Tasks 7/8/9, implement the durable
> expected-manifest/retry snapshot, attempt outcome receipt, gate receipt + all-intents transaction,
> receipt-driven Reconciler, unified bundle postcheck, and immutable conflict lifecycle specified
> below. These requirements replace any earlier step that assumed an in-memory outcome or PASS could
> survive a crash.
>
> **Retry attempt transition fix round 2 (2026-08-05):** Every allowed automatic retry must first
> terminate the old attempt as `RETRY_WAIT`, then idempotently create a strictly increasing
> `AUTHORIZED` next attempt with a fresh staging namespace before its first dispatch. No diagram or
> implementation step may route retry back into the current attempt.
>
> **Retry/manual-recovery distinction fix round 3 (2026-08-05):** Automatic retry keeps the
> authorized action ID and creates attempt+1 without human intervention or a new plan. Only
> integrity-class `REPAIR_REQUIRED`, conflict, durable corruption, and unknown-probe recovery require
> human resolve/unblock followed by a new plan version, action ID, and staging namespace.
>
> **Semantic/integrity repair distinction fix round 4 (2026-08-05):** `REPAIR_REQUIRED` is a status,
> not one universal disposition. A mapped ordinary business/quality repair keeps the run `RUNNING`
> and automatically creates a new plan version/action ID/staging namespace; integrity repair and
> missing/unknown classification fail closed to `BLOCKED`. Every repair fact, incident, and event
> persists explicit `repair_class`, `repair_source`, and stable `reason_code`.

## Global Constraints

- Work only in `/Users/yezhitong/my-projects/ai-book-interpreter/.worktrees/agent-loop-research` on branch `codex/research-agent-loop`.
- Do not preserve `pipeline_state.json`, old run data, `Status`, `HAPPY_PATH`, `STAGE_SEQUENCE`, `StageSpec`, or `--until` compatibility.
- The dependency order becomes `types → config → ir → project → epub → qa → release → tools → actions → planning → orchestrator → cli`; providers may depend only on `types` and `config`, while business layers may call provider interfaces.
- `langchain*`, `langgraph*`, `langfuse*`, and provider SDK imports remain under `src/abi/providers/**`; business tools use ABI-owned `ToolBinding` values.
- Every external value is parsed into a frozen Pydantic model at the boundary; no `dict` or `Any` crosses providers into business code.
- Planner may propose only; PolicyEngine, validators, Committer, and Reconciler exclusively control authorization, gates, run status, and business commits.
- Unknown capability, validator, predicate, outcome, or exception classification fails closed with an error message that includes a repair instruction.
- `state/run.db` is the business source of truth; `events.jsonl`, `metrics.json`, and `state/status.json` are rebuildable projections.
- A `RUNNING` attempt's executor is never automatically invoked again; missing handoff facts are reconstructed or blocked from durable staging/manifest evidence. Business commits are exactly-once by stable `run_id`, `plan_version`, `action_id`, `attempt`, and `idempotency_key`.
- Ordinary retryable outcomes and allowed probe-`absent` resolutions terminate the old attempt/Action as `RETRY_WAIT`; `create_next_attempt(action_id, previous_attempt)` uniquely and idempotently creates attempt+1=`AUTHORIZED`, freezes the same authorized facts, and reserves the logical `state/staging/{action_id}/{attempt+1}` identity before first dispatch. Automatic retry keeps the action ID; conflict recovery still requires a new plan version and action ID.
- Filesystem promotion and SQLite commits use `promotion_intent` plus reconciliation; never claim cross-medium atomicity.
- The platform remains single-process. Machine-managed canonical relpaths are exact portable lowercase keys; after an intent is `COMMITTED`, canonical plus ledger are authoritative and staged residue is non-authoritative.
- Externally supplied, foreign-owned, dirty, or hot-journal SQLite files are invalid input; no mutation-free inspection guarantee is made for them.
- Every built-in, agent tool handler, and deterministic writer writes only under `state/staging/{action_id}/{attempt}/...`; deterministic builders receive a staged sink/output view and never a canonical output path.
- Ordinary success carries a non-empty frozen typed artifact bundle bound to `action_id + attempt`; directories, globs, duplicate staged/canonical paths, non-portable canonical paths, and expected-effect mismatches fail closed.
- Parameter expansion creates an exact expected artifact manifest. Extra and missing bundle entries are equally invalid; a write set is only a permission ceiling.
- Validators read a staging-aware evidence view: this attempt's staged outputs shadow their logical canonical paths, while all other dependencies are read-only committed canonical artifacts. Gate evidence binds the canonical bundle digest and staged checksums.
- Committer persists every bundle promotion intent before copying any canonical file, promotes/reconciles all entries, and records success only after every intent is `COMMITTED`. Multi-file filesystem promotion is not atomic.
- Authorization and `start_attempt` persist the parameter-expanded expected manifest plus full retry policy JSON/fingerprint before executor dispatch.
- Dispatcher persists an immutable `attempt_outcome_receipt` in one SQLite transaction before any controller outcome hook. A `RUNNING` attempt is never automatically re-executed when this receipt is absent.
- Validator PASS becomes durable only when one SQLite transaction creates the `gate_receipt` and the complete promotion-intent set. No canonical copy precedes that transaction.
- Before success, Committer performs a unified postcheck of every committed canonical entry against receipts/intents; post-success reconciliation continues the same checks and blocks the run on drift.
- Any intent conflict, partial intent set, receipt mismatch, or integrity failure atomically moves attempt/Action to `REPAIR_REQUIRED`, blocks the run, preserves all evidence/files, and requires a new plan version/new action ID/new staging namespace after manual resolution.
- `REPAIR_REQUIRED` is never eligible for `create_next_attempt()`. A durable, Registry-mapped
  `repair_class=semantic` fact with no integrity conflict or uncertain external side effect leaves the
  run `RUNNING`; Planner automatically creates a new plan version, repair action ID, and staging
  namespace while preserving the original attempt. A `repair_class=integrity` fact, any
  conflict/durable corruption, unknown/conflicting probe, unsafe external side effect, or
  missing/unknown/mismatched classification blocks the run; only that path requires human
  resolve/unblock before a new plan/action/staging. Semantic repair cannot overwrite or clean
  conflict canonical files, receipts, gates, intents, or probe resolutions.
- The initial stable repair vocabulary includes semantic `term_drift` and Registry-mapped validator
  reasons, plus integrity `artifact_identity_conflict`, `artifact_bundle_conflict`,
  `artifact_checksum_conflict`, `canonical_write_incomplete`, `partial_intent_set`,
  `receipt_binding_conflict`, `gate_binding_conflict`, `post_success_drift`,
  `probe_resolution_unknown`, `probe_resolution_conflict`, `external_side_effect_unclassified`, and
  `repair_class_unknown`. The stored `repair_source` is the actual producing boundary, not a value
  rewritten to obtain semantic authorization.
- `ProbeResolution` is a separate evidence-only outcome. It atomically resolves the original `INDETERMINATE` attempt through the ledger; ordinary `Succeeded` never implicitly resolves an external operation.
- Unclassified failures are `PermanentFailure`, except possible external side effects, which are `Indeterminate` and must be probed before retry.
- Translation Actions receive only source text, five to eight style rules, and matched terminology; QA, EPUB, and release rules are excluded.
- Use TDD for every task, run the named focused test first, and make the listed commit only after the focused and regression checks pass.

---

## File and Boundary Map

| Area | Files | Responsibility |
| --- | --- | --- |
| Pure contracts | `src/abi/types/artifact_paths.py`, `src/abi/types/orchestration.py`, `src/abi/types/tools.py` | Pure lexical canonical-key validator; frozen run, plan, bundle/manifest, receipts, probe resolution, outcome, authorization, and tool-binding shapes |
| Configuration | `src/abi/types/run.py`, `src/abi/config/loader.py` | Planner horizon, loop, retry, timeout, and concurrency limits |
| Business persistence | `src/abi/project/ledger_schema.py`, `src/abi/project/run_ledger.py` | SQLite schema, durable authorization/attempt snapshots, outcome/gate receipts, legal transitions, probe resolutions, incidents, outbox |
| Artifact transactions | `src/abi/project/artifacts.py`, optional `src/abi/project/artifact_paths.py` re-export | Attempt-scoped writer, safe receipt reconstruction, per-item promotion, unified bundle postcheck/reconciliation |
| Action control | `src/abi/actions/contracts.py`, `registry.py`, `effects.py`, `evidence.py`, `validators.py`, `builtins/catalog.py` | Capability definitions, parameter-expanded expected manifests, staging-aware evidence, eligibility, execution, deterministic gates |
| Planning | `src/abi/planning/context.py`, `planner.py`, `policy.py`, `scheduler.py` | Snapshot compression, structured proposal, deterministic authorization and conflict-free batches |
| Provider runtimes | `src/abi/providers/agent_runtime/runner.py`, `tooling.py`, `src/abi/providers/orchestration_runtime/runtime.py` | LangChain Action harness, ABI tool adaptation, LangGraph durable cycle cursor |
| Control plane | `src/abi/orchestrator/controller.py`, `dispatcher.py`, `committer.py`, `reconcile.py`, `run.py` | Observe-plan-authorize-dispatch-validate-commit-replan loop |
| User surface | `src/abi/cli/main.py`, `src/abi/project/scaffold.py`, `layout.py` | Create/resume/inspect/unblock/cancel over the new ledger |
| Removal/docs | legacy state/stage files, architecture/reliability/product/eval docs | Delete fixed path and document the implemented behavior |

## Binding Protocol Flow

```mermaid
flowchart TD
    AUTH["Authorization + RUNNING attempt<br/>durable manifest + retry facts"] --> ACTION["Built-in / agent / deterministic Action"]
    ACTION --> WRITER["AttemptStagingWriter<br/>state/staging/action/attempt"]
    WRITER --> BUNDLE["Frozen ArtifactBundle<br/>caller-canonical order"]
    BUNDLE --> OUTREC["AttemptOutcomeReceipt<br/>durable before controller hook"]
    OUTREC --> VIEW["StagingEvidenceView<br/>current outputs shadow canonical"]
    VIEW --> GATE["GateDecision<br/>bundle digest + staged checksums"]
    GATE -- "PASS" --> INTENTS["One SQLite transaction<br/>GateReceipt + complete intent set"]
    INTENTS --> PROMOTE["Per-entry promote / reconcile<br/>filesystem is not atomic"]
    PROMOTE --> ALL{"All intents COMMITTED?"}
    ALL -- "yes" --> POSTCHECK["Unified canonical bundle postcheck"]
    POSTCHECK -- "pass" --> SUCCESS["One SQLite transaction<br/>artifacts + gate + attempt/action success"]
    ALL -- "no / conflict" --> INCIDENT["Integrity-class REPAIR_REQUIRED<br/>Run BLOCKED · immutable evidence"]
    POSTCHECK -- "drift" --> INCIDENT

    REPAIR["RepairRequired outcome"] --> REPAIRCLASS{"Explicit class/source/reason<br/>and Registry mapping?"}
    GATE -- "FAIL" --> REPAIRCLASS
    REPAIRCLASS -- "semantic + mapped" --> SEMANTIC["Original attempt/action REPAIR_REQUIRED<br/>Run stays RUNNING · repair fact + incident"]
    SEMANTIC --> NEWREPAIR["Planner creates new plan version<br/>new repair action ID + staging"]
    REPAIRCLASS -- "integrity / missing / unknown / unmapped" --> INCIDENT

    INDET["Original attempt INDETERMINATE"] --> PROBE["Bound read-only probe Action"]
    PROBE --> RESOLUTION{"ProbeResolution"}
    RESOLUTION -- "succeeded" --> ORIGINALOK["Original SUCCEEDED<br/>do not reissue"]
    RESOLUTION -- "absent + retry allowed" --> RETRY["Original attempt/action RETRY_WAIT"]
    RETRYABLE["RetryableFailure receipt<br/>old attempt/action RETRY_WAIT"] --> RETRY
    RETRY --> NEXTATTEMPT["One ledger transaction<br/>same action_id · attempt+1 AUTHORIZED<br/>fresh staging + frozen facts"]
    NEXTATTEMPT --> NEXTDISPATCH["First dispatch of new attempt only"]
    RESOLUTION -- "absent + retry denied" --> BLOCKED["Run BLOCKED"]
    RESOLUTION -- "unknown / conflicting" --> BLOCKED
```

## Task 1: Frozen Orchestration and Tool Contracts

**Files:**
- Create: `src/abi/types/artifact_paths.py`
- Create: `src/abi/types/orchestration.py`
- Create: `src/abi/types/tools.py`
- Modify: `src/abi/types/__init__.py`
- Modify: `src/abi/types/run.py`
- Test: `tests/test_orchestration_types.py`
- Test: `tests/test_artifact_paths.py`

**Interfaces:**
- Produces: pure `canonical_artifact_key()`, `RunStatus`, `ActionStatus`, `ActionKind`, `RetryPolicySpec`, `ActionSpec`, `ArtifactMetadata`, `ArtifactBundleEntry`, `ArtifactBundle`, `ExpectedArtifact`, `ExpectedArtifactManifest`, canonical `AttemptOutcomeReceiptPayload`, `GateReceiptPayload`, `ActionArgument`, `ProposedAction`, `PlanPatch`, `RunSnapshot`, `AuthorizedAction`, `AuthorizationDecision`, bundle-bound `GateDecision`, `ProbeResolution`, `ActionOutcomeEnvelope`, `ToolCallRecord`, `AgentRunResult`, `RunResult`, and `ToolBinding`.
- Consumes: existing `abi.types._base.FrozenModel`.

- [ ] **Step 1: Write the failing contract tests**

```python
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
    patch = PlanPatch(
        objective="ingest source",
        proposed_actions=(ProposedAction(
            proposal_id="p1",
            capability="source.ingest",
            arguments=(ActionArgument(name="source_relpath", value_json='"source/raw.txt"'),),
            dependencies=(),
            expected_evidence=("source_manifest",),
            priority=100,
        ),),
        rationale="source evidence is absent",
    )
    with pytest.raises(ValidationError):
        patch.objective = "mutated"  # type: ignore[misc]


def test_action_outcome_is_discriminated() -> None:
    envelope = ActionOutcomeEnvelope.model_validate({
        "action_id": "a1",
        "attempt": 1,
        "outcome": {"kind": "permanent_failure", "error_code": "unsupported_format",
                    "message": "convert the source to txt or epub"}
    })
    assert isinstance(envelope.outcome, PermanentFailure)
    assert RunStatus.BLOCKED.value == "BLOCKED"
```

Add parameterized tests that reject an empty bundle; directory/glob paths; uppercase, Unicode,
absolute, dot, backslash, or `state/staging` canonical paths; a staged path outside the exact
action/attempt namespace; duplicate staged or canonical paths; unordered entries/metadata; and
conflicting `ActionOutcomeEnvelope` identity. Add a golden canonical-JSON/digest test and parse all
three `ProbeResolution.disposition` values. Assert `Indeterminate` rejects missing error code or a
non-canonical failure signature. Add receipt-payload tests for JSON/digest mismatch, success without
bundle, failure without failure fields, and caller disorder; never auto-sort. Assert the superseded
single-file payload is rejected as an extra/missing-field error. Assert `RepairRequired` rejects a
missing/unknown `repair_class`, missing/unknown `repair_source`, or empty `reason_code`; Task 8's
boundary classifier must convert such malformed external outcomes into an integrity incident with
`reason_code=repair_class_unknown`, never silently default them to semantic.

- [ ] **Step 2: Run the tests and confirm the missing-module failure**

Run: `.venv/bin/pytest tests/test_artifact_paths.py tests/test_orchestration_types.py -v`

Expected: collection fails with missing `abi.types.artifact_paths` / orchestration contracts.

- [ ] **Step 3: Implement the complete pure contract surface**

Use `StrEnum`, `Literal`, `Annotated`, `Field(discriminator="kind")`, and `FrozenModel`. Define exactly these terminal shapes:

```python
class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED_BUDGET = "PAUSED_BUDGET"
    PAUSED_HITL = "PAUSED_HITL"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ArtifactMetadata(FrozenModel):
    name: str
    value_json: str


class ArtifactBundleEntry(FrozenModel):
    staged_relpath: str
    canonical_relpath: str
    media_type: str
    evidence_role: str
    metadata: tuple[ArtifactMetadata, ...] = ()


class ArtifactBundle(FrozenModel):
    action_id: str
    attempt: int = Field(ge=1)
    entries: tuple[ArtifactBundleEntry, ...] = Field(min_length=1)


class ExpectedArtifact(FrozenModel):
    canonical_relpath: str
    media_type: str
    evidence_role: str
    metadata: tuple[ArtifactMetadata, ...] = ()


class ExpectedArtifactManifest(FrozenModel):
    action_id: str
    entries: tuple[ExpectedArtifact, ...] = ()


class Succeeded(FrozenModel):
    kind: Literal["succeeded"] = "succeeded"
    artifact_bundle: ArtifactBundle
    evidence_refs: tuple[str, ...] = ()


class RetryableFailure(FrozenModel):
    kind: Literal["retryable_failure"] = "retryable_failure"
    error_code: str
    message: str
    retry_after_s: float | None = None


class RepairRequired(FrozenModel):
    kind: Literal["repair_required"] = "repair_required"
    repair_class: Literal["semantic", "integrity"]
    repair_source: Literal["action_outcome", "validator", "integrity_guard"]
    reason_code: str
    defect_codes: tuple[str, ...]
    message: str


class PermanentFailure(FrozenModel):
    kind: Literal["permanent_failure"] = "permanent_failure"
    error_code: str
    message: str


class Indeterminate(FrozenModel):
    kind: Literal["indeterminate"] = "indeterminate"
    operation_key: str
    error_code: str
    failure_signature: str
    message: str


class ProbeResolution(FrozenModel):
    kind: Literal["probe_resolution"] = "probe_resolution"
    operation_key: str
    disposition: Literal["succeeded", "absent", "unknown"]
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    message: str


class Paused(FrozenModel):
    kind: Literal["paused"] = "paused"
    reason: Literal["budget", "hitl"]
    message: str


ActionOutcome = Annotated[
    Succeeded | RetryableFailure | RepairRequired | PermanentFailure | Indeterminate
    | ProbeResolution | Paused,
    Field(discriminator="kind"),
]


class ActionOutcomeEnvelope(FrozenModel):
    outcome: ActionOutcome
    action_id: str
    attempt: int = Field(ge=1)


class AttemptOutcomeReceiptPayload(FrozenModel):
    action_id: str
    attempt: int = Field(ge=1)
    canonical_outcome_json: str
    outcome_digest: str
    canonical_bundle_json: str | None = None
    bundle_digest: str | None = None
    evidence_refs: tuple[str, ...] = ()
    error_code: str | None = None
    failure_signature: str | None = None


class GateArtifactIdentity(FrozenModel):
    staged_relpath: str
    canonical_relpath: str
    checksum: str


class GateReceiptPayload(FrozenModel):
    action_id: str
    attempt: int = Field(ge=1)
    validator_id: str
    validator_version: str
    canonical_gate_decision_json: str
    gate_decision_digest: str
    bundle_digest: str
    artifacts: tuple[GateArtifactIdentity, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()


class ToolCallRecord(FrozenModel):
    name: str
    arguments_json: str


class AgentRunResult(FrozenModel):
    outcome: ActionOutcome
    llm_calls: int
    tool_calls: int
    cost_usd: float
    stopped_reason: Literal["completed", "iteration_limit", "paused", "error"]
    tool_log: tuple[ToolCallRecord, ...] = ()
```

Define the remaining contracts with these exact fields and defaults:

```python
class ActionKind(StrEnum):
    DETERMINISTIC = "deterministic"
    AGENT = "agent"
    COMPOSITE = "composite"


class ActionStatus(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRY_WAIT = "RETRY_WAIT"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    PERMANENT_FAILED = "PERMANENT_FAILED"
    INDETERMINATE = "INDETERMINATE"
    PAUSED = "PAUSED"


class RetryPolicySpec(FrozenModel):
    max_attempts: int = Field(default=3, ge=1)
    retryable_codes: tuple[str, ...] = ()
    base_delay_s: float = Field(default=1.0, ge=0)
    max_delay_s: float = Field(default=30.0, ge=0)


class PredicateSpec(FrozenModel):
    name: str
    arguments: tuple[ActionArgument, ...] = ()


class EffectSpec(FrozenModel):
    name: str
    artifact_pattern: str | None = None


class EvidenceSpec(FrozenModel):
    name: str
    required: bool = True


class ActionSpec(FrozenModel):
    capability: str
    description: str
    input_schema: str
    action_kind: ActionKind
    prerequisites: tuple[PredicateSpec, ...] = ()
    effects: tuple[EffectSpec, ...] = ()
    expected_evidence: tuple[EvidenceSpec, ...] = ()
    tool_allowlist: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    read_set: tuple[str, ...] = ()
    write_set: tuple[str, ...] = ()
    retry_policy: RetryPolicySpec = Field(default_factory=RetryPolicySpec)
    validator: str
    resource_class: str = "default"
    estimated_cost_usd: float = Field(default=0.0, ge=0)
    may_have_side_effects: bool = False
    probe_capability: str | None = None


class ActionArgument(FrozenModel):
    name: str
    value_json: str


class ProposedAction(FrozenModel):
    proposal_id: str
    capability: str
    arguments: tuple[ActionArgument, ...] = ()
    dependencies: tuple[str, ...] = ()
    expected_evidence: tuple[str, ...] = ()
    priority: int = 0


class PlanPatch(FrozenModel):
    objective: str
    proposed_actions: tuple[ProposedAction, ...]
    superseded_action_ids: tuple[str, ...] = ()
    rationale: str


class ArtifactRef(FrozenModel):
    artifact_id: str
    relpath: str
    sha256: str
    producer_action_id: str


class GateEvidence(FrozenModel):
    evidence_id: str
    gate: str
    passed: bool
    validator_version: str
    artifact_checksums: tuple[str, ...] = ()


class IncidentView(FrozenModel):
    incident_id: str
    error_code: str
    message: str
    action_id: str | None = None
    repair_class: Literal["semantic", "integrity"] | None = None
    repair_source: Literal["action_outcome", "validator", "integrity_guard"] | None = None
    reason_code: str | None = None


class ActionView(FrozenModel):
    action_id: str
    capability: str
    status: ActionStatus
    failure_signature: str | None = None
    repair_class: Literal["semantic", "integrity"] | None = None
    repair_source: Literal["action_outcome", "validator", "integrity_guard"] | None = None
    reason_code: str | None = None


class EligibleAction(FrozenModel):
    capability: str
    description: str
    input_schema: str
    estimated_cost_usd: float = Field(ge=0)


class RunSnapshot(FrozenModel):
    run_id: str
    status: RunStatus
    plan_version: int = Field(default=0, ge=0)
    actions: tuple[ActionView, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    gate_evidence: tuple[GateEvidence, ...] = ()
    incidents: tuple[IncidentView, ...] = ()
    eligible_actions: tuple[EligibleAction, ...] = ()
    remaining_budget_usd: float | None = Field(default=None, ge=0)
    failure_signatures: tuple[str, ...] = ()


class AuthorizedAction(FrozenModel):
    action_id: str
    proposal_id: str
    plan_version: int = Field(ge=1)
    capability: str
    parameters_json: str
    dependencies: tuple[str, ...] = ()
    priority: int = 0
    read_set: tuple[str, ...] = ()
    write_set: tuple[str, ...] = ()
    idempotency_key: str
    expected_artifact_manifest: ExpectedArtifactManifest
    expected_artifact_manifest_digest: str
    expected_evidence_refs: tuple[str, ...] = ()
    retry_policy: RetryPolicySpec
    retry_policy_fingerprint: str


class AuthorizationDecision(FrozenModel):
    authorized: bool
    reason_codes: tuple[str, ...] = ()
    actions: tuple[AuthorizedAction, ...] = ()


class GateDecision(FrozenModel):
    passed: bool
    reason_code: str
    message: str
    validator_id: str
    validator_version: str
    bundle_digest: str
    artifact_checksums: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()


class RunResult(FrozenModel):
    run_id: str
    status: RunStatus
    cost_usd: float = Field(default=0.0, ge=0)
    blocked_reason: str | None = None
```

Create the pure lexical `canonical_artifact_key()` in `abi.types.artifact_paths`; it imports no
project/business module. Project code may import or re-export it, but `types` never imports
`project`. Implement one canonical JSON encoder for artifact bundles/receipts. Validate staged paths as relative
regular-file candidates scoped exactly beneath `state/staging/{action_id}/{attempt}/`; validate
canonical paths through `canonical_artifact_key()`; reject directories, globs, empty bundles,
duplicate staged paths, duplicate canonical paths, empty media/evidence roles, duplicate metadata
names, and metadata values that are not canonical JSON. Require callers to provide entries already
strictly ordered by `(canonical_relpath, staged_relpath)` and metadata already strictly ordered by
`name`; reject disorder or duplicates at the boundary and never auto-sort. Serialize with UTF-8, sorted keys, and
compact separators, then SHA-256 the bytes as `bundle_digest`. Do not casefold, Unicode-normalize,
or dot-normalize paths. `ExpectedArtifactManifest` uses the same canonical ordering and rejects
disorder or duplicate canonical paths; the deterministic expander must emit it correctly.

Canonical failure signature is exactly
`sha256(capability + "\n" + canonical_parameters_json + "\n" + error_code)`. Validate every
`Indeterminate.failure_signature` against it. Receipt payloads validate canonical JSON/digest
pairs, require bundle fields only for ordinary success, and preserve caller order rather than
repairing it. Apply discriminant-specific rules: copy
`error_code` where the outcome defines it, and require `failure_signature` for `Indeterminate`.

`IncidentView` and `ActionView` use model validators so repair fields are all-null for non-repair
records or all-present for repair records. A `REPAIR_REQUIRED` action cannot be parsed without a
class/source/reason; malformed persisted values become the integrity `repair_class_unknown` incident
at the repository boundary before a snapshot is exposed.

No legacy single-file `Succeeded` field or compatibility parser is permitted. An ordinary success
must contain at least one entry; evidence-only probe Actions return `ProbeResolution`, never an
empty bundle. `ActionOutcomeEnvelope.action_id` and `.attempt`, when parsed at the dispatcher
boundary, are required to equal the bundle identity before commit.

Add `PlannerConfig(horizon=5, max_rejections=3)`, `OrchestrationConfig(max_cycles=500, max_parallel_actions=4, default_action_attempts=3)`, and fields on `RunConfig` with frozen defaults. Define `ToolBinding` as a frozen dataclass containing `name`, `description`, `args_schema: type[FrozenModel]`, and a sync-or-async callable; it is an internal runtime binding, not a persisted model.

- [ ] **Step 4: Run focused type and configuration tests**

Run: `.venv/bin/pytest tests/test_artifact_paths.py tests/test_orchestration_types.py tests/test_project_model.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit the contracts**

```bash
git add src/abi/types tests/test_artifact_paths.py tests/test_orchestration_types.py
git commit -m "feat: define dynamic orchestration contracts"
```

## Task 2: Action Registry, Predicates, and Deterministic Policy

**Files:**
- Create: `src/abi/actions/__init__.py`
- Create: `src/abi/actions/contracts.py`
- Create: `src/abi/actions/registry.py`
- Create: `src/abi/actions/predicates.py`
- Create: `src/abi/planning/__init__.py`
- Create: `src/abi/planning/policy.py`
- Test: `tests/test_action_registry.py`
- Test: `tests/test_policy_engine.py`

**Interfaces:**
- Consumes: Task 1 models.
- Produces: `ActionDefinition`, `ResolvedAction`, `ActionExecutionContext`, `ActionRegistry.register()`, `ActionRegistry.resolve()`, explicit semantic-repair reason-to-capability mappings, `PredicateCatalog`, and `PolicyEngine.authorize(snapshot, patch, next_plan_version)`.

- [ ] **Step 1: Write registry fail-closed tests**

```python
def test_registry_rejects_missing_validator_and_duplicate_capability() -> None:
    registry = ActionRegistry(predicates=PredicateCatalog(), validators={})
    with pytest.raises(RegistryConfigurationError, match="register validator"):
        registry.register(definition_for("source.ingest", validator="source_manifest"))


def test_registry_parses_arguments_with_capability_schema() -> None:
    registry = populated_registry()
    resolved = registry.resolve(
        "source.ingest",
        (ActionArgument(name="source_relpath", value_json='"source/raw.txt"'),),
    )
    assert resolved.parameters.source_relpath == "source/raw.txt"
```

- [ ] **Step 2: Write policy tests for illegal plans and conflicts**

```python
def test_policy_rejects_unknown_capability_cycle_and_write_conflict() -> None:
    decision = policy.authorize(snapshot, illegal_patch(), next_plan_version=2)
    assert decision.authorized is False
    assert set(decision.reason_codes) == {
        "unknown_capability", "dependency_cycle", "write_conflict"
    }


def test_policy_emits_only_canonical_validated_parameters() -> None:
    decision = policy.authorize(snapshot, ingest_patch(), next_plan_version=2)
    assert decision.authorized is True
    action = decision.actions[0]
    assert action.parameters_json == '{"source_relpath":"source/raw.txt"}'
    assert action.expected_artifact_manifest.entries[0].canonical_relpath == "source/raw.txt"
    assert action.expected_evidence_refs == ("source_manifest",)
    assert action.retry_policy.retryable_codes == ("provider_timeout",)
    assert action.retry_policy_fingerprint == retry_policy_digest(action.retry_policy)
```

Add tests that a durable semantic repair fact such as `term_drift` authorizes only its explicitly
mapped repair capability with a new action ID/manifest, while the original action remains outside
the patch. Assert an integrity incident, uncertain external side effect, unknown/missing/mismatched
repair class/source/reason, or semantic repair manifest touching conflict canonical paths, old
receipts, gates, intents, or probe resolutions rejects the whole patch with stable reason codes and
does not call the Planner fallback.

- [ ] **Step 3: Run both test files and observe missing implementations**

Run: `.venv/bin/pytest tests/test_action_registry.py tests/test_policy_engine.py -v`

Expected: collection fails because `abi.actions.registry` and `abi.planning.policy` do not exist.

- [ ] **Step 4: Implement registry startup validation and argument parsing**

Use this public shape:

```python
@dataclass(frozen=True)
class ActionDefinition:
    spec: ActionSpec
    input_model: type[FrozenModel]
    executor: ActionExecutor
    validator: ActionValidator


@dataclass(frozen=True)
class ResolvedAction:
    definition: ActionDefinition
    parameters: FrozenModel
    parameters_json: str


class ActionExecutor(Protocol):
    async def __call__(
        self, context: ActionExecutionContext, parameters: FrozenModel
    ) -> ActionOutcomeEnvelope: ...


class ActionValidator(Protocol):
    def __call__(self, project: BookProject, parameters: FrozenModel) -> GateDecision: ...


class ActionRegistry:
    def register(self, definition: ActionDefinition) -> None:
        capability = definition.spec.capability
        if capability in self._definitions:
            raise RegistryConfigurationError(
                f"duplicate capability {capability}; remove one registration"
            )
        self._validate_definition(definition)
        self._definitions[capability] = definition

    def get(self, capability: str) -> ActionDefinition:
        try:
            return self._definitions[capability]
        except KeyError as exc:
            raise UnknownCapabilityError(
                f"unknown capability {capability}; choose an eligible registered capability"
            ) from exc

    def resolve(
        self, capability: str, arguments: tuple[ActionArgument, ...]
    ) -> ResolvedAction:
        definition = self.get(capability)
        raw = self._decode_unique_arguments(capability, arguments)
        parameters = definition.input_model.model_validate(raw)
        return ResolvedAction(
            definition=definition,
            parameters=parameters,
            parameters_json=parameters.model_dump_json(),
        )
```

`validate_startup()` iterates every definition and calls `_validate_definition()`. `eligible()` evaluates every declared predicate through `PredicateCatalog`, then returns immutable `EligibleAction` summaries sorted by capability. Unit tests must exercise those two methods directly rather than relying only on `register()`.

Registry startup also validates an explicit one-to-one semantic repair mapping from stable validator/
outcome `reason_code` to a registered repair capability. Duplicate reasons, missing targets, or a
mapping to a capability whose declared effects could touch protocol-owned conflict evidence fail
startup. Absence from this mapping is not an implicit semantic repair.

`resolve()` rejects duplicate argument names, parses each `value_json`, builds one JSON object only inside the parsing boundary, validates it through `input_model.model_validate`, and immediately stores `model_dump_json()` as canonical JSON. Error text names the capability, field, and how to correct the plan.

- [ ] **Step 5: Implement deterministic policy checks**

`PolicyEngine.authorize()` runs in this order: horizon 1–5, unique proposal IDs, known/eligible capability, argument parsing, deterministic effect expansion, dependencies exist, acyclic graph, hard prerequisites, budget estimate, read/write conflicts, repeated-failure signature, terminal release policy. For each authorized Action it embeds the canonical parameter-expanded expected manifest JSON/digest, stable expected evidence refs, and the complete canonical `RetryPolicySpec` JSON/fingerprint; these are authorization facts, not later Registry lookups. It returns all rejection codes in stable sorted order and never partially authorizes a rejected patch.

Before ordinary plan checks, classify open repair facts. Only a fact with
`repair_class=semantic`, an explicit trusted source/reason, a Registry mapping, run=`RUNNING`, and no
integrity incident or uncertain external side effect may authorize an automatic repair plan. That
plan creates a new plan version/action ID and exact fresh-staging manifest. Integrity or
missing/unknown/mismatched classification keeps run=`BLOCKED` and rejects Planner output. A semantic
repair action may not overwrite/delete/select/clean conflict canonical files or mutate old outcome/
gate receipts, promotion intents, or probe resolutions.

```python
class PolicyEngine:
    def __init__(self, registry: ActionRegistry) -> None:
        self._registry = registry

    def authorize(
        self, snapshot: RunSnapshot, patch: PlanPatch, *, next_plan_version: int
    ) -> AuthorizationDecision:
        reasons = self._collect_rejections(snapshot, patch)
        if reasons:
            return AuthorizationDecision(authorized=False, reason_codes=tuple(sorted(reasons)))
        actions = self._resolve_actions(patch, next_plan_version)
        return AuthorizationDecision(authorized=True, actions=actions)
```

- [ ] **Step 6: Run focused tests and static checks**

Run: `.venv/bin/pytest tests/test_action_registry.py tests/test_policy_engine.py -v`

Run: `.venv/bin/ruff check src/abi/actions src/abi/planning tests/test_action_registry.py tests/test_policy_engine.py`

Expected: both commands pass.

- [ ] **Step 7: Commit registry and policy**

```bash
git add src/abi/actions src/abi/planning tests/test_action_registry.py tests/test_policy_engine.py
git commit -m "feat: authorize registered actions with deterministic policy"
```

## Task 3: Async RunLedger and Legal State Transitions

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/abi/project/ledger_schema.py`
- Create: `src/abi/project/run_ledger.py`
- Modify: `src/abi/project/__init__.py`
- Test: `tests/test_run_ledger.py`

**Interfaces:**
- Consumes: Task 1 models.
- Produces: `RunLedger.open(path)`, `create_run()`, `append_plan()`, `authorize_actions()`, `create_next_attempt()`, `start_attempt()`, `record_attempt_outcome()`, `record_repair_required()`, `create_gate_receipt_and_bundle_intents()`, `mark_bundle_conflict()`, `finish_attempt()`, `commit_success()`, `record_incident()`, `set_run_status()`, `load_snapshot()`, and `rebuild_status_projection()`.

- [ ] **Step 1: Write transaction, transition, and exactly-once tests**

```python
@pytest.mark.asyncio
async def test_commit_success_is_exactly_once(tmp_path: Path) -> None:
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await seed_validated_committed_bundle(ledger, action_id="a1", checksum="abc")
        first = await ledger.commit_success(success_commit("a1", checksum="abc"))
        second = await ledger.commit_success(success_commit("a1", checksum="abc"))
        assert first == second
        assert await ledger.count_committed_actions("a1") == 1
        assert await ledger.count_outbox_events("action.committed", "a1") == 1


@pytest.mark.asyncio
async def test_illegal_transition_rolls_back(tmp_path: Path) -> None:
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        run_id = await ledger.create_run(run_seed())
        with pytest.raises(LedgerTransitionError, match="resume or unblock"):
            await ledger.set_run_status(run_id, RunStatus.COMPLETED)
        assert (await ledger.get_run(run_id)).status is RunStatus.RUNNING


@pytest.mark.asyncio
async def test_attempt_snapshots_manifest_and_retry_policy_before_dispatch(tmp_path: Path) -> None:
    async with seeded_ledger(tmp_path) as ledger:
        attempt = await ledger.start_attempt("a1", attempt=1)
        assert attempt.expected_manifest_digest == authorized_manifest_digest("a1")
        assert attempt.retry_policy.retryable_codes == ("provider_timeout",)
        assert attempt.retry_policy_fingerprint == retry_policy_digest(attempt.retry_policy)


@pytest.mark.asyncio
async def test_concurrent_retry_ticks_create_exactly_attempt_two(tmp_path: Path) -> None:
    async with retry_wait_ledger(tmp_path, action_id="a1", attempt=1) as ledger:
        first, second = await asyncio.gather(
            ledger.create_next_attempt("a1", previous_attempt=1),
            ledger.create_next_attempt("a1", previous_attempt=1),
        )
        assert first == second
        assert first.attempt == 2
        assert first.status is ActionStatus.AUTHORIZED
        assert first.expected_manifest_digest == authorized_manifest_digest("a1")
        assert first.retry_policy_fingerprint == retry_policy_digest(first.retry_policy)
        assert await ledger.attempt_numbers("a1") == (1, 2)
        assert await ledger.attempt_status("a1", 1) is ActionStatus.RETRY_WAIT


@pytest.mark.asyncio
async def test_outcome_receipt_is_idempotent_but_conflicts_on_changed_bundle(tmp_path: Path) -> None:
    async with running_attempt_ledger(tmp_path) as ledger:
        receipt = outcome_receipt("a1", 1, bundle_digest="bundle-a")
        assert await ledger.record_attempt_outcome(receipt) == await ledger.record_attempt_outcome(receipt)
        with pytest.raises(LedgerConflictError):
            await ledger.record_attempt_outcome(
                outcome_receipt("a1", 1, bundle_digest="bundle-b")
            )
```

Add transaction tests for both `record_repair_required()` branches. Semantic `term_drift` must
atomically persist the original receipt-bound repair fact/incident/outbox, set original
attempt/action `REPAIR_REQUIRED`, and leave run `RUNNING`. Integrity, unknown/missing class/source/
reason, and conflicting replay must preserve evidence and set run `BLOCKED`. Assert all repair rows
carry identical class/source/reason values and neither branch creates a retry attempt or replacement
Action inside the ledger transaction.

- [ ] **Step 2: Run the ledger tests and confirm failure**

Run: `.venv/bin/pytest tests/test_run_ledger.py -v`

Expected: collection fails because `abi.project.run_ledger` does not exist.

- [ ] **Step 3: Add the async SQLite dependency and schema**

Add `"aiosqlite>=0.20,<1"` to runtime dependencies and run `uv lock`. `SCHEMA_SQL` must create WAL-backed tables `runs`, `plan_versions`, `actions`, `action_attempts`, `attempt_outcome_receipts`, `artifact_bundles`, `artifacts`, `gate_receipts`, `promotion_intents`, `gate_evidence`, `probe_resolutions`, `repair_facts`, `incidents`, `interrupts`, `budget_entries`, and `event_outbox`.

`actions` stores canonical expected-manifest JSON/digest, stable expected evidence refs, plus complete retry-policy JSON/fingerprint.
Initial `start_attempt()` copies those immutable facts into `action_attempts` in the same transaction
that sets `RUNNING`, before executor dispatch. Retry attempts are first created as `AUTHORIZED` by
`create_next_attempt()` and carry `retry_of_attempt`; a partial unique constraint on
`(action_id, retry_of_attempt)` plus unique `(action_id, attempt)` prevents two controller ticks
from creating different successors for the same old attempt. `attempt_outcome_receipts` stores action/attempt, canonical
outcome JSON/digest, optional bundle JSON/digest, evidence refs, error code, failure signature, and
`recorded_at`. `gate_receipts` stores validator ID/version, canonical GateDecision JSON/digest,
bundle digest, ordered staged path/canonical path/checksum identities, evidence refs, and
`recorded_at`. Add unique constraints on `(run_id, version)`, `(run_id, action_id)`,
`(action_id, attempt)`, non-null `(action_id, retry_of_attempt)`, exactly one outcome receipt and gate receipt per attempt, one bundle per
attempt, artifact canonical path, one probe resolution per original attempt/operation key, and
event idempotency key.

`repair_facts` uniquely binds run/action/attempt to `repair_class`, `repair_source`, stable
`reason_code`, defect/evidence identities, and the original outcome/gate receipt. The matching
nullable fields on `actions`, `action_attempts`, `incidents`, and repair-related `event_outbox` rows
must agree with that fact. Repository parsing treats a missing/unknown/mismatched class/source/reason
as `repair_class=integrity`, `reason_code=repair_class_unknown`, and blocks the run; projections never
invent a semantic default.

```sql
CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  plan_version INTEGER NOT NULL,
  capability TEXT NOT NULL,
  parameters_json TEXT NOT NULL,
  expected_manifest_json TEXT NOT NULL,
  expected_manifest_digest TEXT NOT NULL,
  expected_evidence_refs_json TEXT NOT NULL,
  retry_policy_json TEXT NOT NULL,
  retry_policy_fingerprint TEXT NOT NULL,
  status TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  committed_at TEXT
);
```

- [ ] **Step 4: Implement `RunLedger` with explicit transactions**

Use `aiosqlite.Connection`, `BEGIN IMMEDIATE`, injected UTC clock, and repository-owned row-to-model parsing. `record_attempt_outcome()` verifies the receipt against the immutable attempt identity/manifest/policy snapshot; exact replay is idempotent and any differing fact conflicts. Route an allowed ordinary `RetryableFailure` by atomically terminating its attempt/Action as `RETRY_WAIT` while preserving its receipt/error/signature. `create_next_attempt(action_id, previous_attempt)` then verifies that durable state and the snapshotted policy/count, derives exactly `previous_attempt + 1`, and in one transaction creates a not-yet-run `AUTHORIZED` row with a fresh staging identity and copies the same authorized parameters/manifest/evidence/retry facts. It also returns the Action to `AUTHORIZED`. Exact/concurrent replay returns that row; an existing mismatched successor fails closed instead of creating another attempt. `create_gate_receipt_and_bundle_intents()` verifies outcome/bundle identity and inserts the gate receipt plus the **complete** intent set in one transaction; partial replay is corruption, never piecemeal repair. `commit_success()` requires matching outcome/gate receipts and every expected intent `COMMITTED`, then writes Action status, artifacts, gate evidence, budget entry, and outbox event in one SQLite transaction. An identical repeated commit returns the prior record; a different checksum for the same Action enters the conflict lifecycle.

`record_repair_required()` has two mutually exclusive transactions. For a trusted, Registry-mapped
semantic reason with no integrity/external-side-effect conflict, preserve the original receipt,
insert the repair fact + semantic incident + outbox, set original attempt/Action
`REPAIR_REQUIRED`, and leave run=`RUNNING`; it neither calls `create_next_attempt()` nor creates the
replacement action. For integrity or missing/unknown/mismatched classification, preserve every fact,
write `repair_class=integrity` (unknown reason becomes `repair_class_unknown`), set attempt/Action
`REPAIR_REQUIRED` or preserve `INDETERMINATE` as appropriate, and set run=`BLOCKED`. Exact replay is
idempotent; conflicting replay is itself an integrity block.

Add legal retry transitions `RUNNING → RETRY_WAIT → AUTHORIZED → RUNNING`, with the last two
transitions applying only through the unique new attempt. Add compensating transitions `RUNNING → REPAIR_REQUIRED` and
`SUCCEEDED → REPAIR_REQUIRED` for receipt/intent/canonical integrity failure. `mark_bundle_conflict()`
atomically applies the attempt and Action transition, sets run `BLOCKED`, and inserts one idempotent
subject-scoped incident with `repair_class=integrity`, `repair_source=integrity_guard`, and its stable
conflict `reason_code`, while preserving all prior receipts/intents/artifact/gate rows. It cannot
authorize retry or create a replacement Action.

```python
@asynccontextmanager
async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
    await self._db.execute("BEGIN IMMEDIATE")
    try:
        yield self._db
    except BaseException:
        await self._db.rollback()
        raise
    else:
        await self._db.commit()
```

- [ ] **Step 5: Run ledger tests and regression tests**

Run: `.venv/bin/pytest tests/test_run_ledger.py tests/test_project_model.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit the ledger**

```bash
git add pyproject.toml uv.lock src/abi/project tests/test_run_ledger.py
git commit -m "feat: add transactional run ledger"
```

## Task 4: Staging, Promotion Intents, and Reconciliation

**Files:**
- Create: `src/abi/project/artifacts.py`
- Modify or create only as a re-export: `src/abi/project/artifact_paths.py`
- Extend: `src/abi/project/run_ledger.py`
- Extend: `src/abi/project/ledger_schema.py`
- Modify: `src/abi/project/layout.py`
- Test: `tests/test_artifact_promotion.py`
- Test: `tests/test_run_ledger.py`

**Interfaces:**
- Consumes: `RunLedger`, `ArtifactBundle`, and `ExpectedArtifactManifest`.
- Produces: attempt-bound `ArtifactStore.writer(action_id, attempt) -> AttemptStagingWriter`,
  create-only `AttemptStagingWriter.write_bytes(canonical_relpath, data, media_type,
  evidence_role, metadata) -> ArtifactBundleEntry`, display-only `staging_dir()`, canonical
  bundle JSON/digest, safe `rebuild_outcome_receipt()`, per-item `promote()`,
  `verify_committed_bundle()`, `reconcile_attempt(run_id, action_id, attempt)`, safe `sha256_file()`, and durable
  `PENDING | COMMITTED | CONFLICT` promotion transitions. `canonical_artifact_key()` is the one
  lexical boundary for portable canonical reservation keys.

- [ ] **Step 1: Write crash, race, storage-failure, and conflict-state tests**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("crash_point", ["after_intent", "after_canonical_write"])
async def test_reconcile_completes_interrupted_promotion(tmp_path: Path, crash_point: str) -> None:
    async with prepared_store(tmp_path, content="translation") as (store, ledger, staged):
        with pytest.raises(InjectedCrash):
            await store.promote(staged, crash_after=crash_point)
        await store.reconcile_all()
        assert (tmp_path / "chapters/final/001.md").read_text() == "translation"
        assert await ledger.promotion_state(staged.intent_id) == "COMMITTED"


@pytest.mark.asyncio
async def test_different_canonical_checksum_creates_conflict(tmp_path: Path) -> None:
    async with prepared_store(tmp_path, content="new") as (store, ledger, staged):
        canonical = tmp_path / "chapters/final/001.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("old")
        with pytest.raises(ArtifactConflictError, match="choose the canonical artifact"):
            await store.promote(staged)
        assert await ledger.has_open_incident("artifact_checksum_conflict")


@pytest.mark.asyncio
async def test_bundle_persists_every_intent_before_any_canonical_copy(tmp_path: Path) -> None:
    bundle = two_file_bundle(tmp_path, action_id="a1", attempt=1)
    copied: list[str] = []
    store = instrumented_store(tmp_path, on_copy=lambda path: copied.append(path))
    receipt, intents = await store.ledger.create_gate_receipt_and_bundle_intents(
        gate_receipt_for(bundle), intents_for(bundle)
    )
    assert copied == []
    assert receipt.bundle_digest == bundle_digest(bundle)
    assert {item.status for item in intents} == {"PENDING"}
    assert await store.ledger.count_intents("a1", 1) == 2


@pytest.mark.asyncio
async def test_canonical_race_inside_ledger_commit_is_compensated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with prepared_store(tmp_path, content="expected") as (store, ledger, intent):
        canonical = tmp_path / "chapters/final/001.md"
        commit = ledger.commit_promotion_intent

        async def commit_then_replace(intent_id: str) -> PromotionIntent:
            committed = await commit(intent_id)
            canonical.unlink()
            canonical.write_text("competing", encoding="utf-8")
            return committed

        monkeypatch.setattr(ledger, "commit_promotion_intent", commit_then_replace)
        with pytest.raises(ArtifactConflictError):
            await store.promote(intent)
        assert await ledger.promotion_state(intent.intent_id) == "CONFLICT"


def test_sha256_file_rejects_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.bin"
    target.write_bytes(b"artifact")
    alias = tmp_path / "alias.bin"
    alias.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        sha256_file(alias)
```

Add the companion FIFO test with a bounded worker join, forced teardown only for the RED run, and
`/dev/fd` counts before/after. It must fail if the call blocks, accepts the FIFO, or leaks its opened
descriptor. Add partial-write then `ENOSPC` injection that asserts `canonical_write_incomplete`, a
durable `CONFLICT`, preserved partial/staged bytes, and absence of `artifact_intent_invalid`.

Add portable-key tests that reject uppercase (`STATE/STAGING`, `CHAPTERS/FINAL`), Unicode and
casefold aliases, empty/dot components, backslashes, absolute paths, and the lowercase
`state/staging` prefix before intent or filesystem mutation. Legal lowercase examples must be
stored verbatim and reserve one unique key. Add restart tests proving that, after a valid
`COMMITTED` transition, changed, deleted, or externally cleaned staging residue causes neither
compensation nor an incident while the canonical facts remain valid. Retain the canonical
commit-window and post-commit drift tests unchanged.

Add bundle tests proving one failed intent insert rolls back all intent rows and performs zero
canonical copies; a crash after intent creation or after any individual copy leaves the attempt
`RUNNING`; reconciliation is scoped by `run_id/action_id/attempt`, completes every recoverable
intent without executing the Action, and reports every conflict after read-only inspection of the
rest. Add missing outcome-receipt tests that rebuild only when every expected regular file exists
and there are no extra/unsafe leaves; incomplete/extra staging must atomically set attempt/Action
`REPAIR_REQUIRED`, persist `repair_class=integrity`, `repair_source=integrity_guard` and the stable
staging reason code, and set run `BLOCKED` without executor calls. Assert a bundle with one `CONFLICT` can
never be reported successful even when all sibling intents had already committed.

Add unified-postcheck tests that mutate entry 1 or entry 2 after its per-item postcheck but before
success; `verify_committed_bundle()` must detect name/inode/checksum/dirchain drift against
outcome/gate receipts and intents, enter the conflict lifecycle, and withhold success. Repeat after
success and assert the compensating `SUCCEEDED → REPAIR_REQUIRED` transition, idempotent incident,
explicit integrity class/source/drift reason, and run `BLOCKED` while prior success/artifact/gate
history remains auditable.

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.venv/bin/pytest tests/test_artifact_promotion.py tests/test_run_ledger.py -v`

Expected: the new tests fail because `CONFLICT` parsing/compensation, post-commit repair,
`canonical_write_incomplete`, no-follow/nonblocking public hashing, portable canonical-key
validation, and the COMMITTED staging-authority boundary are absent.

- [ ] **Step 3: Implement the durable promotion state machine**

`PromotionIntent.status` is exactly `PENDING | COMMITTED | CONFLICT`. The ledger provides an
atomic compensation operation that changes either `PENDING` or `COMMITTED` to `CONFLICT` and
creates one subject-scoped incident in the same SQLite transaction. Repeating the same
compensation is idempotent. Repeating a matching `COMMITTED` transition is idempotent, but
`CONFLICT → COMMITTED` is an illegal transition with a repair-oriented error. Unknown persisted
states fail closed during repository-owned parsing.

Use Task 3's `create_gate_receipt_and_bundle_intents(gate_receipt, intents)` as the only
intent-creation API. It validates the gate receipt against the current attempt outcome receipt and
durable expected manifest, stores gate receipt plus all intents in the same SQLite transaction, and
returns existing rows only for an exact idempotent replay. A partial prior set, different
ordering/digest/checksum, duplicate destination, or action/attempt mismatch enters the bundle
conflict lifecycle; it must not repair by adding missing rows piecemeal.

| Current status | Requested transition | Result |
| --- | --- | --- |
| `PENDING` | commit after all prechecks | `COMMITTED` |
| `COMMITTED` | identical commit replay | unchanged `COMMITTED` |
| `PENDING` or `COMMITTED` | conflict compensation + incident | `CONFLICT` atomically |
| `CONFLICT` | repeated same compensation | unchanged `CONFLICT`, no duplicate incident |
| `CONFLICT` | commit | reject; never report success |

- [ ] **Step 4: Implement portable canonical keys, pinned staging, and checksum I/O**

Import the Task 1 validator from `abi.types.artifact_paths`; `project.artifact_paths` may re-export
it, but the implementation must not move the lexical rule back into project or create a
`types → project` dependency. Machine-managed canonical relpaths use `/` separators and components matching only
`[a-z0-9._-]+`. The shared lexical validator rejects uppercase, Unicode, empty, `.`, `..`, absolute
paths, backslashes, and the `state/staging` prefix without normalization. ArtifactStore invokes it
before canonical filesystem traversal; RunLedger invokes the same function before opening a
transaction and stores only its returned key. No old-path compatibility or casefold migration is
provided.

Pin the project root descriptor and its device/inode for the `ArtifactStore` lifetime. Traverse all
staging and canonical directories relative to pinned dirfds with `O_DIRECTORY | O_NOFOLLOW`, and
revalidate the durable root and directory-chain identities before commit and after commit.
`write_staged_bytes()` creates each leaf once with
`O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_NONBLOCK`, requires a regular file, and fsyncs the
file and parent. Expose writes only through an `AttemptStagingWriter` that maps requested logical
canonical paths to create-only staged leaves and returns typed effect entries; do not expose a raw
canonical output path or writer. `staging_dir()` is display-only. `sha256_file()` opens with
`O_RDONLY | O_NOFOLLOW | O_NONBLOCK`, requires a regular file through `fstat`, and closes the fd on
every success and failure path; symlinks are rejected and FIFOs never block.

`rebuild_outcome_receipt(action_id, attempt)` is the only recovery path when a `RUNNING` attempt has
no receipt. Traverse the exact attempt namespace with pinned no-follow dirfds. Derive each staged
leaf from the durable expected manifest's deterministic canonical→staged mapping; require every
expected regular file, the exact expected ancestor directories, and no extra/unsafe entries.
Media type, evidence role, metadata, and stable expected evidence refs come from the durable
manifest/authorization facts; compute file checksums and canonical outcome/bundle JSON/digests,
then call `record_attempt_outcome()`. Any ambiguity, missing/extra leaf, unexpected directory,
symlink/FIFO/device, or identity mismatch invokes `mark_bundle_conflict()` and never calls the
executor. An empty/evidence-only manifest or an attempt whose outcome cannot be uniquely proven by
the exact file set is not reconstructable; block rather than guessing success/failure/probe.

- [ ] **Step 5: Implement all-intents-first bundle promotion**

Reject non-canonical caller ordering; do not auto-sort. Persist the gate receipt and every lexically
valid `PENDING` intent in one SQLite transaction before any canonical mutation. Any
insert/identity/effect error rolls back the whole set. Per-item promotion starts only after
reloading and proving outcome receipt, gate receipt, durable intent count, and identities exactly
match the bundle. Hold the
canonical-parent dirfd
through the whole operation. If the canonical name already exists, open it read-only without
following links and commit only when its checksum matches. If absent, open the final canonical name
exactly once with `O_CREAT | O_EXCL | O_NOFOLLOW | O_NONBLOCK`, copy verified staged bytes through
the returned fd, fsync and hash that fd, and prove that the name still identifies the same inode and
the parent still belongs to the pinned durable chain. Then commit the ledger intent and repeat the
inode/checksum/directory-chain checks.

The platform is single-process. While an intent is `PENDING`, staged bytes are authoritative
promotion input and must pass every existing precommit check. Once canonical creation, durability,
identity checks, and the ledger transition have succeeded, `COMMITTED` makes canonical plus ledger
the only authoritative facts; staging immediately becomes non-authoritative runtime residue.
COMMITTED reconciliation therefore never parses or hashes staged paths, and staged deletion,
cleanup, or drift creates no conflict or incident. This boundary does not remove or weaken any
canonical commit-window or post-commit checksum, inode, name, root, or directory-chain check.

The last filesystem precheck and the SQLite update are deliberately **not** described as atomic.
Any identity, checksum, or directory-chain failure after SQLite commit immediately compensates
`COMMITTED → CONFLICT` and records the incident. A process crash in that window leaves
`COMMITTED`; startup reconciliation repeats the postchecks and performs the same durable
compensation if it finds drift.

The same non-atomic window permits another connection to compensate a stale `PENDING` snapshot to
`CONFLICT` while a worker is already copying. That worker may have created or written the canonical
candidate before it learns the durable state, but its ledger commit must be rejected and converted
to an aggregateable artifact conflict. Preserve the candidate; after durable conflict, later
sibling intents receive read-only inspection only and no new promotion. Calls that load `CONFLICT`
perform no further write or delete; this rule does not claim to undo syscalls
already issued by an in-flight worker.

Filesystem promotion of multiple entries is not atomic and must never be described that way. Do not
create promotion temporary names, rename/replace/link an artifact into place, overwrite a
canonical name, or automatically unlink staged/canonical files. A partial canonical created by a
write, file-fsync, or parent-directory-fsync failure is retained. Record
`canonical_write_incomplete` with instructions to inspect storage and preserve the partial
canonical plus staged source; never misclassify that failure as `artifact_intent_invalid`.

After every entry is `COMMITTED` and before the success transaction,
`verify_committed_bundle()` reopens the complete canonical set through pinned no-follow dirfds and
proves every name/inode/checksum/dirchain against outcome receipt, gate receipt, and intents. A
single failure marks the entire bundle conflict. This pre-success check does not make filesystem
and SQLite atomic. The next Reconciler cycle repeats it even after success; post-success drift uses
the compensating `SUCCEEDED → REPAIR_REQUIRED`, run `BLOCKED`, and idempotent incident path while
preserving prior business history; the action/attempt/incident/event all carry matching
`repair_class=integrity`, `repair_source=integrity_guard`, and post-success-drift reason.

- [ ] **Step 6: Implement reconciliation from the durable state table**

| Durable status | Filesystem evidence | Reconciliation |
| --- | --- | --- |
| `PENDING` | valid staged; canonical absent | perform the create-only copy and guarded commit |
| `PENDING` | canonical checksum matches; staged absent or matches | guarded commit to `COMMITTED` |
| `PENDING` | neither artifact exists | enter bundle conflict lifecycle; idempotent `artifact_promotion_missing` incident |
| `PENDING` | canonical differs/is partial, staged differs, or intent path is unsafe | atomically enter `CONFLICT`; retain every artifact |
| `COMMITTED` | canonical name/inode/checksum/dirchain match; staged has any contents or is absent | remain `COMMITTED`; staged is non-authoritative residue |
| `COMMITTED` | canonical missing/drifted, or any canonical identity/dirchain check fails | atomically compensate to `CONFLICT`; never recreate canonical |
| `CONFLICT` | any evidence | never commit, rewrite, delete, retry, or replan; preserve evidence and inspect siblings read-only |

`reconcile_attempt(run_id, action_id, attempt)` first validates outcome receipt, gate receipt, and
the complete intent set. It processes every safe intent, then calls `verify_committed_bundle()`
before success. When some entries are already `COMMITTED` and staging was cleaned, it proves them
with gate receipt + intent + canonical checksums and does not rerun the validator. It may rerun the
validator only if all staging still exists safely and the canonical GateDecision JSON/digest is
identical to the gate receipt. Any conflict or integrity failure calls `mark_bundle_conflict()`;
manual continuation requires a new plan version/action ID/staging namespace after explicit
canonical conflict selection/cleanup. `reconcile_all()` may iterate attempts but cannot mix their success
conditions. Repeated
reconciliation and compensation do not duplicate incidents. Automatic staged cleanup remains out of
scope until a separate durable ownership protocol can prove safe unlinking.

- [ ] **Step 7: Run focused and project tests**

Run: `.venv/bin/pytest tests/test_artifact_promotion.py tests/test_project_model.py tests/test_run_ledger.py -v`

Expected: all tests pass.

- [ ] **Step 8: Commit artifact transactions**

```bash
git add src/abi/project tests/test_artifact_promotion.py tests/test_run_ledger.py
git commit -m "feat: reconcile staged artifact promotion"
```

## Task 5: Snapshot Builder and Structured Planner

**Files:**
- Create: `src/abi/planning/context.py`
- Create: `src/abi/planning/planner.py`
- Create: `src/abi/prompts/planner.py`
- Test: `tests/test_planner.py`

**Interfaces:**
- Consumes: `RunLedger.load_snapshot()`, `ActionRegistry.eligible()`, and `LLMRouter.invoke_structured()`.
- Produces: `SnapshotBuilder.build(run_id) -> PlanningContext` and
  `Planner.plan(context) -> PlanPatch`. `PlanningContext.policy_snapshot` is
  the complete durable evidence view for PolicyEngine revalidation;
  `planner_snapshot` is the separately bounded, code-only view serialized to
  the LLM.

- [ ] **Step 1: Write snapshot minimization and structured-output tests**

```python
@pytest.mark.asyncio
async def test_snapshot_contains_hashes_not_book_body(tmp_path: Path) -> None:
    builder = await seeded_snapshot_builder(tmp_path, body="SECRET BOOK BODY")
    snapshot = await builder.build("run-1")
    payload = snapshot.model_dump_json()
    assert "SECRET BOOK BODY" not in payload
    assert snapshot.artifacts[0].sha256 == sha256_text("SECRET BOOK BODY")


@pytest.mark.asyncio
async def test_planner_returns_a_valid_patch_from_structured_provider() -> None:
    router = DeterministicPlannerProvider(result=valid_patch())
    patch = await Planner(router=router).plan(snapshot_with_ingest_eligible())
    assert patch.proposed_actions[0].capability == "source.ingest"
    assert patch.objective == "produce missing source evidence"
    assert patch.proposed_actions[0].expected_evidence == ("source_manifest",)
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.venv/bin/pytest tests/test_planner.py -v`

Expected: collection fails because the planner modules do not exist.

- [ ] **Step 3: Implement compressed snapshot construction**

Read one complete durable ledger snapshot, derive eligible capabilities from
that full policy view, then construct a separately bounded Planner view. Keep
the full `policy_snapshot` for PolicyEngine; never substitute the sampled
planner view during authorization. The Planner view contains only artifact
metadata, bounded actions/gates/incidents/rejection codes, remaining budget,
and eligible capability descriptions—never book body or arbitrary rejection
messages. Limit incident messages to 500 characters, samples to configured
counts, and Planner horizon to five. Loading additional content is represented
by an eligible `inspect.*` Action rather than direct file access.
Repair facts/incidents retain their explicit class/source/reason. Only mapped semantic repair facts
may expose repair capabilities while run=`RUNNING`; an integrity/unknown-class blocked snapshot
exposes no automatic repair candidate and controller must not invoke Planner for it.

- [ ] **Step 4: Implement the Planner prompt and structured call**

```python
PLANNER_SYSTEM_PROMPT = """You are ABI's constrained planner.
Return one PlanPatch with one to five actions chosen only from eligible_actions.
You cannot mark gates passed, mutate run state, invent capabilities, or skip dependencies.
Prefer the smallest action that produces missing evidence or repairs a mapped semantic incident.
Treat prior rejection reasons as hard feedback.
"""


class Planner:
    async def plan(self, snapshot: RunSnapshot) -> PlanPatch:
        patch, _ = await self._router.invoke_structured(
            PlanPatch,
            [system_message(PLANNER_SYSTEM_PROMPT), user_message(snapshot.model_dump_json())],
            agent_name="orchestration.planner",
            prompt_version="dynamic-plan-v1",
            max_retries=2,
        )
        return patch
```

- [ ] **Step 5: Run planner and policy tests**

Run: `.venv/bin/pytest tests/test_planner.py tests/test_policy_engine.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit snapshot and Planner**

```bash
git add src/abi/planning src/abi/prompts/planner.py tests/test_planner.py
git commit -m "feat: plan from compressed run evidence"
```

## Task 6: LangChain v1 Action Harness and Durable Checkpointers

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/abi/providers/agent_runtime/tooling.py`
- Rewrite: `src/abi/providers/agent_runtime/runner.py`
- Modify: `src/abi/providers/agent_runtime/__init__.py`
- Modify: `src/abi/providers/services.py`
- Modify: `src/abi/tools/fs.py`
- Modify: `src/abi/tools/content.py`
- Modify: `src/abi/tools/gates.py`
- Modify: `src/abi/tools/subagent.py`
- Modify: `src/abi/tools/belt.py`
- Create: `tools/lint/architecture.py`
- Test: `tests/test_agent_runtime.py`
- Test: `tests/test_tool_boundaries.py`

**Interfaces:**
- Consumes: `ToolBinding`, `ActionOutcomeEnvelope`, project checkpoint path, shared budget/events/metrics.
- Produces: `AgentActionRequest` and `AgentRuntime.run_action(request) -> AgentRunResult` with no LangChain types in its public return value.

- [ ] **Step 1: Write migration and persistence tests**

```python
def test_architecture_linter_reports_forbidden_sdk_import(tmp_path: Path) -> None:
    module = tmp_path / "src/abi/tools/bad.py"
    module.parent.mkdir(parents=True)
    module.write_text("from langchain.tools import tool\n", encoding="utf-8")
    violations = scan_tree(tmp_path / "src/abi")
    assert [(item.rule, item.path, item.line) for item in violations] == [
        ("sdk-import-outside-providers", "tools/bad.py", 1)
    ]


@pytest.mark.asyncio
async def test_action_harness_resumes_same_thread(tmp_path: Path) -> None:
    runtime = fake_model_runtime(checkpoint_path=tmp_path / "graph-checkpoints.sqlite")
    first = await runtime.run_action(action_request(thread_id="run-1/a1/1"))
    second = await runtime.run_action(action_request(thread_id="run-1/a1/1", resume=True))
    assert first.outcome.kind == "paused"
    assert second.outcome.kind == "succeeded"
    assert second.outcome.evidence_refs == ("prior_tool_result",)
```

- [ ] **Step 2: Run focused tests before dependency changes**

Run: `.venv/bin/pytest tests/test_agent_runtime.py tests/test_tool_boundaries.py -v`

Expected: tests fail because `run_action` and ABI-owned tool bindings are absent.

- [ ] **Step 3: Upgrade the agent runtime dependency set**

Set these ranges in `pyproject.toml`, then run `uv lock` and `uv sync --extra dev`:

```toml
"langchain>=1.3.14,<2",
"langchain-openai>=1.3.5,<2",
"langchain-core>=1.3,<2",
"langgraph>=1.2.9,<2",
"langgraph-checkpoint-sqlite>=3.1,<4",
```

Keep Langfuse on its existing major during this refactor; its v4 rewrite is a separate migration. Verify the selected versions against the official [LangChain v1 release notes](https://docs.langchain.com/oss/python/releases/langchain-v1), [LangGraph v1 release notes](https://docs.langchain.com/oss/python/releases/langgraph-v1), and [SQLite checkpointer documentation](https://docs.langchain.com/oss/python/langgraph/persistence).

- [ ] **Step 4: Replace SDK-owned tools at business boundaries**

Each `make_*_tools()` returns `list[ToolBinding]`. Put Pydantic input models beside their business handler. `providers.agent_runtime.tooling.to_langchain_tool()` is the only adapter. `tools/lint/architecture.py` parses imports with `ast`, returns frozen `Violation` records, and offers a CLI that exits non-zero after printing repair instructions when actual source violates the boundary:

```python
def to_langchain_tool(binding: ToolBinding) -> BaseTool:
    return StructuredTool.from_function(
        func=binding.handler if not inspect.iscoroutinefunction(binding.handler) else None,
        coroutine=binding.handler if inspect.iscoroutinefunction(binding.handler) else None,
        name=binding.name,
        description=binding.description,
        args_schema=binding.args_schema,
    )
```

Remove `set_state` and `record_gate` from content tools. Replace `get_state` with read-only `get_run_snapshot`, supplied by an Action-scoped callback rather than direct ledger mutation.

- [ ] **Step 5: Implement `create_agent` with persistent `AsyncSqliteSaver`**

Use `langchain.agents.create_agent`, `response_format=ActionOutcomeEnvelope`, and `AsyncSqliteSaver.from_conn_string`. Every structured result must echo required `action_id` and `attempt`. Map graph recursion to `RetryableFailure(error_code="iteration_limit")`, budget to `Paused(reason="budget")`, declared transient provider errors to `RetryableFailure`, and possible side-effect timeouts to `Indeterminate(operation_key=..., error_code="provider_timeout", failure_signature=canonical_failure_signature(...), message=...)`. Unknown exceptions become `PermanentFailure(error_code="unclassified_exception")`.

Define the provider-owned request as a frozen dataclass so callable tools are not persisted as Pydantic data:

```python
@dataclass(frozen=True)
class AgentActionRequest:
    system_prompt: str
    user_prompt: str
    tools: tuple[ToolBinding, ...]
    agent_name: str
    thread_id: str
    checkpoint_path: Path
    max_iterations: int
    may_have_side_effects: bool = False
```

```python
async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
    agent = create_agent(
        model=model,
        tools=[to_langchain_tool(tool) for tool in request.tools],
        system_prompt=request.system_prompt,
        response_format=ActionOutcomeEnvelope,
        checkpointer=saver,
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": request.user_prompt}]},
        config={"configurable": {"thread_id": request.thread_id},
                "recursion_limit": request.max_iterations * 2 + 6,
                "callbacks": callbacks},
    )
```

- [ ] **Step 6: Run runtime, tool, and offline regressions**

Run: `.venv/bin/pytest tests/test_agent_runtime.py tests/test_tool_boundaries.py tests/test_orchestrator_offline.py -v`

Run: `.venv/bin/ruff check src tests`

Run: `.venv/bin/python tools/lint/architecture.py src/abi`

Expected: all three commands pass; repository search finds no `create_react_agent`.

- [ ] **Step 7: Commit the runtime migration**

```bash
git add pyproject.toml uv.lock src/abi/providers src/abi/tools tools/lint/architecture.py tests/test_agent_runtime.py tests/test_tool_boundaries.py tests/test_orchestrator_offline.py
git commit -m "feat: add durable LangChain v1 action harness"
```

## Task 7: Built-in Capability Catalog and Fail-Closed Validators

**Files:**
- Create: `src/abi/actions/builtins/__init__.py`
- Create: `src/abi/actions/builtins/inputs.py`
- Create: `src/abi/actions/builtins/catalog.py`
- Create: `src/abi/actions/effects.py`
- Create: `src/abi/actions/evidence.py`
- Create: `src/abi/actions/validators.py`
- Create: `src/abi/prompts/actions.py`
- Move: `src/abi/prompts/stages/*.md.j2` to `src/abi/prompts/actions/*.md.j2`
- Modify: `src/abi/tools/content.py`
- Modify: `src/abi/tools/gates.py`
- Test: `tests/test_builtin_actions.py`
- Test: `tests/test_action_validators.py`
- Test: `tests/test_action_permissions.py`

**Interfaces:**
- Consumes: Action registry/contracts, Action harness, existing deterministic EPUB/QA/release functions, and existing prompt contents.
- Produces: `build_action_registry()`, `ActionPromptRegistry`, typed inputs for every built-in capability, deterministic `expand_expected_artifacts(capability, action_id, parameters)`, attempt-scoped tool handlers/builders, `StagingEvidenceView`, and `validate_evidence(capability, evidence_view, parameters, bundle)`.

- [ ] **Step 1: Write catalog completeness and no-state-mutation tests**

```python
def test_builtin_catalog_has_closed_dependencies_and_validators() -> None:
    registry = build_action_registry()
    registry.validate_startup()
    assert {item.capability for item in registry.specs()} >= {
        "source.ingest", "source.split", "research.global", "research.book",
        "translation.trial", "glossary.prepare", "chapter.translate",
        "chapter.control", "chapter.review", "preproduction.spec",
        "preproduction.sample", "epub.build", "review.spotcheck",
        "review.independent", "release.prepare", "output.finalize",
        "retrospective.capture",
    }


def test_agent_visible_tools_cannot_mutate_control_state() -> None:
    names = {tool.name for tool in build_belt(fake_context()).all()}
    assert names.isdisjoint({"set_state", "record_gate", "commit_action", "mark_done"})


def test_action_receives_only_allowlisted_tools_and_paths() -> None:
    envelope = build_action_envelope("chapter.translate", ChapterBatchInput(chapters=("001",)))
    assert {tool.name for tool in envelope.tools} == {"read_file", "write_file", "grep"}
    assert envelope.permissions.can_write("chapters/translated/001.md")
    assert not envelope.permissions.can_write("glossary/terms.csv")


def test_parameter_expansion_declares_exact_chapter_outputs() -> None:
    manifest = expand_expected_artifacts(
        "chapter.translate", "a1", ChapterBatchInput(chapters=("002", "001"))
    )
    assert manifest.action_id == "a1"
    assert tuple(item.canonical_relpath for item in manifest.entries) == (
        "chapters/translated/001.md", "chapters/translated/002.md"
    )
```

- [ ] **Step 2: Write fail-closed validator tests**

```python
def test_unknown_capability_never_passes(tmp_path: Path) -> None:
    project = scaffold_without_ledger(tmp_path)
    result = validate_evidence(
        "invented.capability", empty_evidence_view(project), EmptyInput(), one_file_test_bundle()
    )
    assert result.passed is False
    assert result.reason_code == "validator_not_registered"


def test_chapter_validator_reads_current_attempt_staging_overlay(tmp_path: Path) -> None:
    project = project_with_committed_source(tmp_path, chapters=("001", "002"))
    bundle = staged_translation_bundle(project, action_id="a1", attempt=1, chapters=("001",))
    view = StagingEvidenceView.for_bundle(project, committed_manifest(project), bundle)
    one = validate_evidence(
        "chapter.translate", view, ChapterBatchInput(chapters=("001",)), bundle
    )
    two = validate_evidence(
        "chapter.translate", view, ChapterBatchInput(chapters=("002",)), bundle
    )
    assert one.passed is True
    assert two.passed is False
```

Add tests proving a canonical output prewritten before validation cannot satisfy the gate; current
attempt staged outputs shadow the same logical canonical path; unrelated dependencies come only
from the committed artifact manifest; another attempt's staging is invisible; and the returned
`GateDecision` binds the exact bundle digest, ordered staged checksums, evidence refs, and validator
version. Add fail-closed tests for extra and missing expected effects, media/evidence-role mismatch,
metadata mismatch, caller-disordered bundle entries/metadata (no auto-sort), and a deterministic
builder or agent tool attempting a direct canonical write. Assert even the unknown-validator FAIL
decision contains required validator ID/version, bundle digest, ordered checksums, and evidence refs;
because its reason is not Registry-mapped semantic repair, controller must fail closed to integrity
`BLOCKED`, not automatically replan.

- [ ] **Step 3: Run focused tests and confirm failure**

Run: `.venv/bin/pytest tests/test_builtin_actions.py tests/test_action_validators.py tests/test_action_permissions.py -v`

Expected: collection fails because the built-in catalog and validator router do not exist.

- [ ] **Step 4: Define typed inputs and registered capability effects**

Use separate models: `SourceIngestInput`, `SourceSplitInput`, `ResearchInput`, `ChapterBatchInput`, `ReviewBatchInput`, `BuildEpubInput`, `ReleaseInput`, and `EmptyInput`. Every ActionSpec declares `prerequisites`, `effects`, `expected_evidence`, `tool_allowlist`, `skill_refs`, `read_set`, `write_set`, retry policy, validator, estimated cost, and Action kind. Every artifact-producing ActionDefinition also binds a deterministic effect expander. It converts action identity plus parsed parameters into a glob-free, directory-free `ExpectedArtifactManifest` with exact canonical path, media type, evidence role, and exact metadata. The expander emits canonical order; it never relies on a later auto-sort. PolicyEngine persists its canonical JSON/digest and the full retry policy/fingerprint at authorization, and `start_attempt()` snapshots both before executor dispatch. Runtime output must match it exactly. Keep dependency rules as predicates, not list ordering.

- [ ] **Step 5: Port prompts and Action execution without `StageSpec`**

`ActionPromptRegistry.render(capability, parameters, snapshot)` selects by capability. Agent Actions execute through one `AgentActionExecutor`; deterministic Actions call existing parsers/builders/linters through a staged sink/output view. Chapter Actions require an explicit chapter tuple so Scheduler can prove disjoint writes. Translation prompt construction enforces the source + five-to-eight style rules + matched-terms envelope. Resolve the registry's `tool_allowlist` through `ToolBelt`, then enforce the same read/write set again inside filesystem handlers.

All built-in, agent, and deterministic writes go through the current
`AttemptStagingWriter(state/staging/{action_id}/{attempt})`. Agent tools continue accepting logical
canonical paths, but the ABI handler maps them to staged leaves, records one typed effect entry, and
never opens canonical output. Deterministic parsers/builders receive a staged sink or
`AttemptOutputView`; remove direct canonical-output calls rather than wrapping them after the write.
At successful executor return, reject recorded entries unless they are already in strict canonical
order, compare them exactly with the durable parameter-expanded manifest, and build
`ActionOutcomeEnvelope(action_id=context.action_id, attempt=context.attempt,
outcome=Succeeded(artifact_bundle=...))`. Composite review Actions
allocate independent thread IDs and reuse the shared BudgetGate.

- [ ] **Step 6: Move validators and make the router exhaustive**

Port checks from `src/abi/stages/validators.py` into capability validators. Change every validator
signature to accept `StagingEvidenceView` and the typed bundle. The view overlays only current
attempt staged outputs at their logical canonical paths and serves every other dependency from the
read-only committed artifact manifest. It must not expose arbitrary project paths or any other
attempt's staging. Replace the old final `return _ok()` with an explicit failure:

```python
def validate_evidence(
    capability: str,
    evidence_view: StagingEvidenceView,
    parameters: FrozenModel,
    bundle: ArtifactBundle,
) -> GateDecision:
    validator = _VALIDATORS.get(capability)
    if validator is None:
        return GateDecision(
            passed=False,
            reason_code="validator_not_registered",
            message=f"No validator for {capability}; register one before authorizing this Action.",
            validator_id="unregistered",
            validator_version="none",
            bundle_digest=evidence_view.bundle_digest,
            artifact_checksums=evidence_view.artifact_checksums,
            evidence_refs=(),
        )
    return validator(evidence_view, parameters, bundle)
```

Every PASS contains the bundle digest, ordered staged checksums, stable evidence refs, and validator
version. A validator may never pass by observing output prewritten to canonical.

- [ ] **Step 7: Run catalog, validator, ingest, EPUB, and QA tests**

Run: `.venv/bin/pytest tests/test_builtin_actions.py tests/test_action_validators.py tests/test_action_permissions.py tests/test_artifact_promotion.py tests/test_ingest_txt.py tests/test_epub_build.py tests/test_eval.py -v`

Expected: all tests pass.

- [ ] **Step 8: Commit the capability catalog**

```bash
git add src/abi/actions src/abi/prompts src/abi/tools tests/test_builtin_actions.py tests/test_action_validators.py tests/test_action_permissions.py
git commit -m "feat: register ABI work as typed capabilities"
```

**Mandatory Task 7 backfill before Task 8 continues:** The earlier Task 7 implementation predates
this breaker amendment. First replace its canonical writers, single-file effects, and project-root
validators with the attempt writer, exact bundle/manifest, and staging-aware contracts above. Do
not continue implementing the Task 8 Committer against the superseded Task 7 surface.

## Task 8: Scheduler, Dispatcher, Committer, and Dynamic Controller

> **Entry gate:** Do not continue the in-progress Task 8 implementation until the mandatory Task 7
> backfill has landed and its focused tests prove attempt-scoped writing, exact bundle effects, and
> staging-aware validation. Tasks 1/3/4 receipt schemas/APIs and compensating conflict transitions
> must also be landed first. Replace any in-progress single-file/in-memory-outcome Committer code;
> do not adapt it with a compatibility branch.

**Files:**
- Create: `src/abi/planning/scheduler.py`
- Create: `src/abi/orchestrator/dispatcher.py`
- Create: `src/abi/orchestrator/committer.py`
- Create: `src/abi/orchestrator/projector.py`
- Create: `src/abi/orchestrator/reconcile.py`
- Create: `src/abi/orchestrator/controller.py`
- Modify: `src/abi/providers/observability/events.py`
- Create: `src/abi/providers/orchestration_runtime/__init__.py`
- Create: `src/abi/providers/orchestration_runtime/runtime.py`
- Rewrite: `src/abi/orchestrator/driver.py`
- Test: `tests/test_scheduler.py`
- Test: `tests/test_dynamic_controller.py`

**Interfaces:**
- Consumes: Planner, PolicyEngine, RunLedger, ArtifactStore/AttemptStagingWriter, ActionRegistry with effect expanders and staging-aware validators, AgentRuntime.
- Produces: `Scheduler.select_batch()`, `Dispatcher.execute()`, `Committer.commit()`, `OutboxProjector.flush()`, `Reconciler.reconcile()`, `DynamicController.tick()`, and generic `DurableLoopRuntime.run()`.

- [ ] **Step 1: Write conflict-free Scheduler tests**

```python
def test_scheduler_parallelizes_only_non_conflicting_actions() -> None:
    batch = Scheduler(max_parallel=4).select_batch((
        action("t1", reads=("chapters/src/001.md",), writes=("chapters/translated/001.md",)),
        action("t2", reads=("chapters/src/002.md",), writes=("chapters/translated/002.md",)),
        action("g", reads=("chapters/translated/*",), writes=("glossary/terms.csv",)),
        action("g2", reads=(), writes=("glossary/terms.csv",)),
    ))
    assert {item.action_id for item in batch} == {"t1", "t2", "g"}
```

- [ ] **Step 2: Write controller outcome-path tests**

```python
@pytest.mark.asyncio
async def test_controller_replans_after_repair_and_completes() -> None:
    rig = controller_rig(outcomes=(
        ActionOutcomeEnvelope(
            action_id="a1", attempt=1,
            outcome=RepairRequired(
                repair_class="semantic",
                repair_source="action_outcome",
                reason_code="term_drift",
                defect_codes=("term_drift",),
                message="repair glossary",
            ),
        ),
        ActionOutcomeEnvelope(
            action_id="a2", attempt=1,
            outcome=Succeeded(artifact_bundle=bundle(
                action_id="a2", attempt=1,
                entries=(("chapter.md", "chapters/translated/001.md", "text/markdown", "translation"),),
            ), evidence_refs=("gate",)),
        ),
    ))
    await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
    assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.COMPLETED
    assert await rig.ledger.attempt_status("a1", 1) is ActionStatus.REPAIR_REQUIRED
    assert await rig.ledger.plan_versions() == (1, 2)
    assert await rig.ledger.action_ids() == ("a1", "a2")
    assert await rig.executor.attempt_ids("a1") == (1,)
    assert await rig.ledger.event_names() == expected_replan_event_sequence()


@pytest.mark.asyncio
async def test_integrity_repair_blocks_without_planner_or_replacement() -> None:
    rig = controller_rig(outcomes=(ActionOutcomeEnvelope(
        action_id="a1", attempt=1,
        outcome=RepairRequired(
            repair_class="integrity",
            repair_source="action_outcome",
            reason_code="artifact_identity_conflict",
            defect_codes=("artifact_identity_conflict",),
            message="preserve evidence for human resolution",
        ),
    ),))
    await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
    assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
    assert rig.planner.call_count == 0
    assert await rig.ledger.action_ids() == ("a1",)


@pytest.mark.asyncio
async def test_permanent_failure_blocks_without_retry() -> None:
    rig = controller_rig(outcomes=(ActionOutcomeEnvelope(
        action_id="rights", attempt=1,
        outcome=PermanentFailure(
            error_code="copyright_denied", message="supply a license or use private mode"
        ),
    ),))
    await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
    assert await rig.ledger.count_attempts(capability="rights.check") == 1
    assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED


@pytest.mark.asyncio
async def test_retry_dispatches_attempt_two_without_reentering_attempt_one() -> None:
    rig = controller_rig(outcomes=(
        ActionOutcomeEnvelope(
            action_id="a1", attempt=1,
            outcome=RetryableFailure(error_code="provider_timeout", message="retry later"),
        ),
        ActionOutcomeEnvelope(
            action_id="a1", attempt=2,
            outcome=Succeeded(artifact_bundle=bundle(
                action_id="a1", attempt=2,
                entries=(("chapter.md", "chapters/translated/001.md", "text/markdown", "translation"),),
            ), evidence_refs=("gate",)),
        ),
    ))
    await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
    assert await rig.executor.attempt_ids("a1") == (1, 2)
    assert await rig.ledger.attempt_status("a1", 1) is ActionStatus.RETRY_WAIT
    assert await rig.ledger.action_status("a1") is ActionStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_committer_requires_complete_multi_file_bundle(tmp_path: Path) -> None:
    rig = committer_rig(tmp_path, expected=("reports/a.json", "reports/b.json"))
    envelope = two_file_success(rig, action_id="a1", attempt=1)
    await rig.ledger.record_attempt_outcome(receipt_for(envelope))
    result = await rig.commit(envelope)
    assert {item.relpath for item in result.artifacts} == {"reports/a.json", "reports/b.json"}
    assert await rig.ledger.count_intents("a1", 1) == 2
    assert await rig.ledger.action_status("a1") is ActionStatus.SUCCEEDED
```

Add Committer tests for bundle/action/attempt mismatch; permission violation; non-regular staged
leaf; extra/missing/duplicate manifest entry; gate digest/checksum mismatch; zero canonical copies
until outcome receipt then gate receipt + every intent are durable; crash after each receipt/intent/
copy/postcheck and unified postcheck; one conflict among committed
siblings; and refusal to mark either attempt or Action successful before every intent is
`COMMITTED`. Assert a crash leaves the attempt `RUNNING` unless integrity failure invokes the
explicit `REPAIR_REQUIRED + BLOCKED` compensation.
Add a mapped validator `term_drift` FAIL test with the same semantic lineage, and unmapped validator
FAIL, malformed/unknown repair classification, uncertain external side effect, and concurrent
integrity-incident tests that all block with zero replacement Action/Planner invocation. Assert the
semantic branch records class/source/reason consistently, leaves the run `RUNNING` between old-action
repair commit and Planner append, creates one new plan/action/staging under repeated ticks, never
creates attempt+1 for the old action, and cannot write/delete/select conflict canonical or protocol
receipt/intent rows.

- [ ] **Step 3: Run Scheduler and controller tests and confirm failure**

Run: `.venv/bin/pytest tests/test_scheduler.py tests/test_dynamic_controller.py -v`

Expected: collection fails because the Scheduler and dynamic controller do not exist.

- [ ] **Step 4: Implement batch selection and typed dispatch**

Scheduler sorts by priority then action ID, incrementally admits Actions whose dependencies are committed and whose read/write sets do not conflict with the batch. Before first execution, controller/Dispatcher uses `start_attempt()` to create/claim the initial attempt and snapshot authorized manifest/policy facts. For retry, controller must first idempotently obtain the exact `AUTHORIZED` successor from `create_next_attempt(action_id, previous_attempt)`; Dispatcher may only claim and dispatch that returned attempt, never infer a number or reuse the old attempt. It resolves canonical parameters, creates the exact new attempt-scoped writer/output view, runs deterministic/agent/composite executors, applies per-Action timeout, and requires `ActionOutcomeEnvelope.action_id/attempt` on every result. It rejects a `Succeeded` bundle whose identity/order/effects differ from the durable action/attempt manifest and rejects `ProbeResolution` from a non-probe capability.

Immediately after typed executor return, the `before_outcome_receipt` test hook may crash. Otherwise
Dispatcher canonical-encodes the envelope and calls `record_attempt_outcome()` in one SQLite
transaction. Only after that transaction commits may it invoke the `after_action_output` hook or
return control to the controller. This applies to success and all failure outcomes; receipts are not
PASS or terminal status. `Indeterminate` requires error code and canonical failure signature.
If external outcome parsing lacks a valid repair class/source/reason, Dispatcher/controller records
an integrity incident with `reason_code=repair_class_unknown` and blocks the run; it never repairs the
payload by defaulting to semantic.

- [ ] **Step 5: Implement commit and reconciliation routing**

Implement ordinary-success commit in this exact order:

1. Load the durable outcome receipt; reject non-canonical caller ordering (never auto-sort), then
   validate the frozen bundle's current run/action/attempt identity, regular
   staged leaves, portable lowercase canonical paths, media/evidence metadata, write permission,
   unique staged/canonical names, and exact equality with the durable parameter-expanded manifest.
2. Construct `StagingEvidenceView` so current attempt outputs shadow their logical canonical paths
   and all other inputs are read-only committed canonical artifacts. Run the deterministic validator
   and require a PASS bound to this bundle digest, ordered staged checksums, evidence refs, and
   validator version.
3. Call `create_gate_receipt_and_bundle_intents()` so canonical GateDecision/validator identity,
   ordered staged checksums/evidence, and **every** intent become durable in one SQLite transaction.
   No canonical copy may precede its commit; a partial intent set is corruption.
4. Promote/reconcile each intent using the create-only protocol and re-read all intents for this
   run/action/attempt. After a conflict inspect siblings read-only; do not promote, retry, or replan.
5. Require every intent to be `COMMITTED`, then run `verify_committed_bundle()` across the complete
   canonical name/inode/checksum/dirchain set against outcome/gate receipts and intents.
6. Only after the unified postcheck passes, use one SQLite transaction to write
   the bundle/artifact rows and gate evidence and transition the attempt/Action to `SUCCEEDED` with
   the cost/outbox facts.

Filesystem promotion across bundle entries is explicitly non-atomic. Any crash before step 6 leaves
the attempt `RUNNING`; it does not become retryable, repair-required, or successful merely because
some files exist. Outcome routing is exact: retryable → atomically close old attempt/Action as
`RETRY_WAIT`, then explicit idempotent attempt+1 creation and first dispatch; mapped semantic repair
→ durable repair fact/incident, original attempt/Action `REPAIR_REQUIRED`, run remains `RUNNING`, then
Planner creates exactly one new plan version/repair action ID/staging; integrity or missing/unknown
repair classification → preserve evidence and run `BLOCKED` with no Planner call;
permanent → alternative capability or BLOCKED; indeterminate → registered probe Action only; paused
→ run pause; ordinary success → the six-step receipt protocol. Reconciler runs before every planning
cycle and groups facts by run/action/attempt. A `RUNNING` attempt with no outcome receipt is never
redispatched: rebuild an exact receipt only from safe staging plus its durable expected manifest;
missing/extra/unsafe evidence invokes `mark_bundle_conflict()`. With a gate receipt and partial
`COMMITTED` set, recover from gate receipt + intents + canonical checksums without requiring cleaned
staging or rerunning validator. A durable non-success receipt is routed idempotently from its
discriminant and attempt-snapshotted policy/failure facts, never from in-memory output. Revalidate only when all staging remains and require byte-identical
GateDecision JSON/digest. Finalize only after unified postcheck. Any conflict/integrity failure
atomically sets attempt/Action `REPAIR_REQUIRED`, run `BLOCKED`, preserves receipts/intents/files,
and creates an idempotent subject incident with explicit integrity class/source/reason; no automatic
rerun/replan. Manual continuation creates a
new plan version/action ID/staging namespace after explicit canonical conflict selection/cleanup.
Automatic retry is different: it retains the same authorized `action_id`, increments attempt exactly
once, freezes identical authorized facts, and uses `state/staging/{action_id}/{next_attempt}`. Repeated
or concurrent ticks return the existing successor and cannot create two next attempts.
Even after ledger success, the next Reconciler cycle repeats committed-intent/canonical postchecks
and compensates drift to integrity-class `REPAIR_REQUIRED + BLOCKED`; this does not claim FS/SQLite
atomicity. Semantic repair never uses conflict cleanup and PolicyEngine rejects it if any integrity
incident appears before authorization.

`OutboxProjector.flush()` reads undelivered ledger events in sequence order, appends them to `events.jsonl` through `EventLogger.append_record(event_id, record)`, updates metrics/status projections, then marks each outbox row delivered. `EventLogger` builds a seen-event-ID set from the existing JSONL file at startup and refuses a second append of the same ID, closing the crash window between file append and the delivered flag. Provider events also receive stable call/attempt IDs, so one file remains a deduplicated projection.
Every repair-related event projects the ledger's exact `repair_class`, `repair_source`, and
`reason_code`; `repair_class_unknown` is projected as integrity and cannot be omitted or rewritten.

- [ ] **Step 6: Implement a provider-generic durable cycle graph**

`DurableLoopRuntime` depends on no ABI business modules. Its graph state contains only `run_id`, `cycle`, and `continue_run`; one `tick` callback performs a business cycle. Use `AsyncSqliteSaver` and stable `thread_id=run_id`.

```python
class LoopState(TypedDict):
    run_id: str
    cycle: int
    continue_run: bool


async def cycle_node(state: LoopState) -> LoopState:
    keep_running = await tick(state["run_id"])
    return {"run_id": state["run_id"], "cycle": state["cycle"] + 1,
            "continue_run": keep_running}
```

Route back to `cycle` only while `continue_run` is true and `cycle < max_cycles`; otherwise end. Max-cycle exhaustion creates a controller incident and BLOCKED state, never implicit completion.

- [ ] **Step 7: Run focused control-plane tests**

Run: `.venv/bin/pytest tests/test_scheduler.py tests/test_dynamic_controller.py tests/test_policy_engine.py tests/test_run_ledger.py -v`

Expected: all tests pass.

- [ ] **Step 8: Commit the dynamic controller**

```bash
git add src/abi/planning src/abi/orchestrator src/abi/providers/orchestration_runtime src/abi/providers/observability/events.py tests/test_scheduler.py tests/test_dynamic_controller.py
git commit -m "feat: execute durable policy-gated action loop"
```

## Task 9: Fault Injection, Retry Exhaustion, and Indeterminate Side Effects

> **Required scope, not follow-up work:** Task 9 implements the complete two-entry bundle crash
> matrix and the complete `succeeded | absent | unknown` probe-resolution/idempotency/conflict
> matrix. Do not defer any row to a later reliability task.

**Files:**
- Extend: `src/abi/orchestrator/reconcile.py`
- Extend: `src/abi/orchestrator/dispatcher.py`
- Extend: `src/abi/project/run_ledger.py`
- Create: `tests/test_orchestration_recovery.py`
- Create: `tests/test_failure_semantics.py`

**Interfaces:**
- Consumes: Task 8 control plane.
- Produces: deterministic bundle/retry-transition crash-point hooks, retry signature accounting, frozen `ProbeResolution`, atomic `RunLedger.resolve_indeterminate()`, durable `probe_resolutions`, idempotent next-attempt creation under concurrent ticks, and resume behavior for paused/blocked runs.

- [ ] **Step 1: Write the recovery matrix as executable tests**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [
    "before_dispatch", "before_outcome_receipt", "after_outcome_receipt",
    "after_action_output", "before_gate_receipt_and_intents", "after_gate_receipt_and_intents",
    "after_first_canonical_create", "after_first_canonical_write",
    "after_first_intent_commit_before_postcheck", "between_bundle_entries",
    "after_all_intents_committed", "after_unified_bundle_postcheck",
    "after_success_ledger_commit", "before_graph_checkpoint",
])
async def test_crash_boundaries_do_not_duplicate_business_facts(
    tmp_path: Path, boundary: str
) -> None:
    rig = recovery_rig(tmp_path, crash_at=boundary)
    with pytest.raises(InjectedCrash):
        await rig.run()
    rig.disable_crash()
    await rig.resume()
    if boundary == "after_first_canonical_create":
        assert await rig.ledger.action_status("a1") is ActionStatus.REPAIR_REQUIRED
        assert await rig.ledger.run_status() is RunStatus.BLOCKED
        assert await rig.ledger.count_artifacts_for("a1") == 0
        assert await rig.canonical_bytes("reports/a.json") == b""
    else:
        assert await rig.ledger.count_committed_actions("a1") == 1
        assert await rig.ledger.count_artifacts_for("a1") == 2
    assert await rig.executor.call_count("a1") == 1
```

`after_first_canonical_create` is the true post-`O_EXCL`/pre-copy authority row and therefore
fails closed while preserving the partial canonical, staging, receipts, and intents. The true
post-copy/pre-fsync boundary is `after_first_canonical_write`. Also test
`after_first_canonical_copy_verified` after file fsync and checksum as an additional successful
recovery boundary; it is not an extra authority-matrix row.

Add a separate retry-transition matrix for both ordinary `RetryableFailure` and allowed probe
`absent`:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [
    "after_retry_wait", "before_create_next_attempt", "after_create_next_attempt",
    "before_start_next_attempt", "before_dispatch_next_attempt",
])
async def test_retry_crashes_create_and_dispatch_only_attempt_two(
    retry_rig: RetryRig, boundary: str
) -> None:
    retry_rig.crash_at(boundary)
    with pytest.raises(InjectedCrash):
        await retry_rig.run()
    retry_rig.disable_crash()
    await asyncio.gather(retry_rig.controller.tick(), retry_rig.controller.tick())
    await retry_rig.resume()
    assert await retry_rig.ledger.attempt_numbers("a1") == (1, 2)
    assert await retry_rig.executor.attempt_ids("a1") == (1, 2)
    assert await retry_rig.ledger.attempt_status("a1", 1) is ActionStatus.RETRY_WAIT
```

Define `before_dispatch_next_attempt` before the `AUTHORIZED → RUNNING` claim. Also inject the
unavoidable crash immediately after that claim and before executor invocation: attempt 2 is then
`RUNNING` with no receipt, so recovery must reconstruct or block under the existing at-most-once
rule and must call neither attempt 1 nor attempt 2 again. It may not demote attempt 2 to
`AUTHORIZED`. In all cases keep both attempt namespaces and prior receipts/intents unchanged.

For every boundary, use a two-entry bundle and assert: all intent rows existed before the first
canonical copy; outcome receipt existed before `after_action_output`; gate receipt and all intents
appeared in one transaction; the attempt stayed `RUNNING` until the complete success transaction; resume grouped
by run/action/attempt and did not rerun that attempt's Action executor; exactly one bundle/gate/success fact was
committed; and staged residue became non-authoritative only after each corresponding intent reached
`COMMITTED`. At `before_outcome_receipt`, resume must rebuild the exact receipt from safe staging +
durable expected manifest without executor calls. Add incomplete, extra, symlink, and unsafe staging
variants that instead produce `repair_class=integrity`, `repair_source=integrity_guard`, a stable
staging reason code, and `REPAIR_REQUIRED + BLOCKED`.

Inject a deliberately partial durable intent set separately (the normal transaction cannot create
one) and assert corruption is blocked without adding missing rows or copying canonical. Add conflict
on entry 1/entry 2 and drift after unified postcheck and after success. Each case sets attempt/Action
`REPAIR_REQUIRED`, run `BLOCKED`, preserves all receipts/intents/files/history, creates one stable
subject incident whose repair class/source/reason matches the action and attempt, and only inspects
siblings read-only. Assert unblock cannot reuse the old Action;
a new plan version/action ID/staging namespace is required after explicit canonical cleanup.
For retry transitions, race many identical `create_next_attempt("a1", previous_attempt=1)` calls and
assert one durable `(a1, 2)` row with `retry_of_attempt=1`, no `(a1, 3)`, one fresh
`state/staging/a1/2` namespace, and exact manifest/retry-policy fingerprints copied from the same
authorized Action. A conflicting successor row or a call from `RUNNING`, `INDETERMINATE`, or
`REPAIR_REQUIRED` fails closed without executor calls.

Add a semantic-repair crash matrix at `before/after_repair_fact_commit`,
`before/after_semantic_replan`, and `before/after_repair_action_authorization`. Start from mapped
`term_drift` and assert every recovery preserves the original receipt, leaves run `RUNNING`, never
reruns original attempt or calls `create_next_attempt()`, and produces exactly one new plan version,
repair action ID, and staging namespace. Inject an integrity incident between repair-fact commit and
authorization and assert PolicyEngine switches to fail-closed `BLOCKED` without authorizing the
semantic action. Also assert semantic recovery cannot clean any conflict canonical/receipt/gate/
intent evidence. Missing/unknown/mismatched repair class/source/reason must record
`repair_class_unknown` on the integrity branch.

- [ ] **Step 2: Write failure classification and probe tests**

```python
@pytest.mark.asyncio
async def test_indeterminate_release_is_probed_not_reissued(tmp_path: Path) -> None:
    operation_log = tmp_path / "external-operations.jsonl"
    rig = release_rig(
        first=ActionOutcomeEnvelope(
            action_id="release", attempt=1,
            outcome=Indeterminate(
                operation_key="release:abc", error_code="provider_timeout",
                failure_signature=canonical_failure_signature(
                    "release.prepare", canonical_release_parameters(), "provider_timeout"
                ),
                message="timeout after request",
            ),
        ),
        probe=ActionOutcomeEnvelope(
            action_id="release-probe", attempt=1,
            outcome=ProbeResolution(
                operation_key="release:abc", disposition="succeeded",
                evidence_refs=("external:release:abc",), message="remote release exists",
            ),
        ),
        operation_log=operation_log,
    )
    await rig.run()
    records = [json.loads(line) for line in operation_log.read_text().splitlines()]
    assert [record["operation"] for record in records] == ["release", "probe"]
    assert await rig.ledger.count_attempts(capability="release.prepare") == 1
    assert await rig.ledger.count_attempts(capability="release.probe") == 1
    assert await rig.ledger.action_status("release") is ActionStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "original_status", "run_status"),
    [
        ("succeeded", ActionStatus.SUCCEEDED, RunStatus.RUNNING),
        ("absent", ActionStatus.RETRY_WAIT, RunStatus.RUNNING),
        ("unknown", ActionStatus.INDETERMINATE, RunStatus.BLOCKED),
    ],
)
async def test_probe_resolution_matrix(
    disposition: str, original_status: ActionStatus, run_status: RunStatus
) -> None:
    rig = indeterminate_rig(disposition=disposition, retry_allowed=True)
    await rig.run_probe()
    assert await rig.ledger.action_status(rig.original_action_id) is original_status
    assert (await rig.ledger.get_run(rig.run_id)).status is run_status
    assert await rig.ledger.action_status(rig.probe_action_id) is ActionStatus.SUCCEEDED


def test_unknown_exception_classifies_fail_closed() -> None:
    outcome = classify_exception(RuntimeError("boom"), may_have_side_effects=False)
    assert outcome.kind == "permanent_failure"
```

Add `absent` cases for retry exhausted, unregistered error code, and mismatched retry-policy
fingerprint; all must block rather than retry. Mutate the in-memory/current catalog policy after the
original attempt and prove `absent` still uses only the outcome receipt error code plus the attempt's
durable full policy/max attempts. Add wrong probe capability, writable/side-effecting
probe registration, wrong original status, wrong operation key, and probe returning ordinary
`Succeeded` tests. Replay the exact same resolution and assert idempotency; replay a different
disposition, evidence set, operation key, original attempt, or retry fingerprint and assert a
durable conflict with no overwritten fact. Inject a crash at every point of the single SQLite
resolve transaction and prove it rolls back probe evidence, probe success, and original status
together. For allowed `absent`, assert resolve terminates the original attempt/Action as
`RETRY_WAIT` but creates no next attempt. Replaying resolution still creates none; only subsequent
concurrent controller calls to `create_next_attempt(original_action_id, original_attempt)` may
produce the single `AUTHORIZED` attempt+1. For `unknown`, assert `create_next_attempt()` is rejected
and no executor runs until human resolve/unblock authorizes a new plan version/action ID/staging
namespace.

- [ ] **Step 3: Run recovery tests and confirm red results**

Run: `.venv/bin/pytest tests/test_orchestration_recovery.py tests/test_failure_semantics.py -v`

Expected: tests fail at the unimplemented crash hooks, signature accounting, and probe routes.

- [ ] **Step 4: Implement retry signatures and external-operation probes**

Define the failure signature exactly as
`sha256(capability + "\n" + canonical_parameters_json + "\n" + error_code)` and require
`Indeterminate.error_code` plus that signature. Authorization and attempt rows already persist full
retry-policy JSON (`retryable_codes`, `max_attempts`, delays) and fingerprint; the outcome receipt
persists the original error code/signature. Retry only errors allowed by those durable attempt facts,
enforce their max attempts/backoff, and never consult a later catalog version. External Actions
declare `may_have_side_effects=True` plus a separate `probe_capability`; Dispatcher refuses to retry
them while the prior attempt is indeterminate.

Registry startup requires every probe capability to be read-only, evidence-only, side-effect-free,
effect/write-set empty, and bound to exactly the probed ActionSpec/operation-key input schema. A
probe executor returns only `ProbeResolution(kind="probe_resolution", operation_key,
disposition="succeeded|absent|unknown", evidence_refs, message)`. It never returns an empty or
ordinary `Succeeded` and never creates promotion intents.

Implement `probe_resolutions` and `RunLedger.resolve_indeterminate(request)` as one serialized
SQLite transaction. Validate probe/original action and attempt identity, registered probe binding,
operation key, original `INDETERMINATE` status, evidence-only probe status, idempotency key, the
outcome receipt's error code/failure signature, and the attempt's complete durable retry policy/
fingerprint. In the same transaction, commit the probe attempt/action and evidence,
insert the immutable resolution/outbox facts, then apply exactly one original route:

- `succeeded`: original attempt/Action → `SUCCEEDED`; never redispatch the external operation.
- `absent`: original attempt/Action → `RETRY_WAIT` only when its attempt-snapshotted retry policy,
  outcome-receipt error code, and attempt count allow; otherwise run → `BLOCKED` with an incident.
  The resolve transaction does not create or dispatch the successor.
- `unknown`: leave original attempt/Action `INDETERMINATE`; run → `BLOCKED` with an incident that
  carries `repair_class=integrity`, `repair_source=integrity_guard`, and
  `reason_code=probe_resolution_unknown`, and requests human/external evidence. It is ineligible for
  automatic retry or `create_next_attempt()`;
  any later executor work requires human resolve/unblock and a new plan version/action ID/staging
  namespace.

An exact replay returns the stored resolution. Any conflicting resolution/evidence/key/policy fact
fails closed and preserves the first durable fact. After either an ordinary retryable receipt route
or allowed `absent`, controller invokes Task 3's `create_next_attempt()` as a separate explicit
idempotent transaction, applies backoff, then first-claims that exact `AUTHORIZED` attempt. Normal
automatic retry retains the action ID and produces executor attempt IDs 1 then 2; it never reuses
attempt 1. `INDETERMINATE`, `RUNNING`, and `REPAIR_REQUIRED` attempts are ineligible.

- [ ] **Step 5: Implement all recovery boundaries**

At startup reconcile ledger/checkpoint/artifacts in this order: retryable receipts whose old
attempt/Action transition to `RETRY_WAIT` is incomplete; `RETRY_WAIT` Actions and their unique
`AUTHORIZED` successor rows; never-started `AUTHORIZED` retry attempts; `RUNNING` attempts grouped by
run/action/attempt and their durable manifest/policy snapshot; missing/conflicting outcome receipts;
missing/conflicting gate receipt + complete intent sets; pending/committed intents; unified
canonical bundle postcheck; success rows missing graph progress; post-success canonical drift;
semantic repair facts awaiting one idempotent new-plan/new-action authorization; indeterminate
operations through bound probe Actions; outbox delivery. A missing outcome receipt
never causes that same attempt's Action executor to be redispatched: exact safe reconstruction or
integrity-class `REPAIR_REQUIRED + BLOCKED` only. A partial intent set is corruption, not a repair
invitation. An
intent conflict stops further sibling
promotion but permits read-only evidence collection. Emit `action.reconciled` for every correction
and preserve original receipts/intents/files/history. Integrity, unknown-class, and indeterminate
routes never auto-create a replacement plan/action; only mapped semantic repair facts with run still
`RUNNING` may idempotently invoke Planner for one new plan/action/staging, and they cannot mutate
conflict evidence.
Creating an automatic retry successor is not a replacement plan: it is allowed only from
`RETRY_WAIT`, retains the same action ID, uses attempt+1 and a fresh staging namespace, and is
idempotent across crashes/concurrent ticks. Reconcile may first-dispatch an existing `AUTHORIZED`
successor but may never re-enter any `RUNNING`/`INDETERMINATE`/`REPAIR_REQUIRED` attempt.

- [ ] **Step 6: Run recovery and full unit tests**

Run: `.venv/bin/pytest tests/test_orchestration_recovery.py tests/test_failure_semantics.py -v`

Run: `.venv/bin/pytest -q`

Expected: focused tests and the full suite pass.

- [ ] **Step 7: Commit reliability semantics**

```bash
git add src/abi/orchestrator src/abi/project/run_ledger.py tests/test_orchestration_recovery.py tests/test_failure_semantics.py
git commit -m "feat: recover action failures without blind retries"
```

## Task 10: New Project Lifecycle and CLI Surface

**Files:**
- Rewrite: `src/abi/project/scaffold.py`
- Rewrite: `src/abi/project/layout.py`
- Rewrite: `src/abi/orchestrator/run.py`
- Rewrite: `src/abi/cli/main.py`
- Modify: `src/abi/project/__init__.py`
- Modify: `src/abi/orchestrator/__init__.py`
- Test: `tests/test_project_model.py`
- Create: `tests/test_dynamic_cli.py`
- Rewrite: `tests/test_orchestrator_offline.py`

**Interfaces:**
- Consumes: new controller and ledger.
- Produces: `make_book()`, `resume()`, `inspect_run()`, `approve_interrupt()`, `unblock()`, `cancel()`, and CLI commands `make-book`, `resume`, `inspect`, `approve`, `unblock`, `cancel`.

- [ ] **Step 1: Replace scaffold and CLI expectations with ledger behavior**

```python
def test_scaffold_creates_new_state_contract(tmp_path: Path) -> None:
    project = scaffold_project(request(tmp_path))
    assert project.run_db.exists()
    assert project.graph_checkpoints.parent.is_dir()
    assert not (project.root / "state/pipeline_state.json").exists()


def test_inspect_prints_plan_actions_and_incidents(cli_runner, seeded_project) -> None:
    result = cli_runner.invoke(app, ["inspect", str(seeded_project.root)])
    assert result.exit_code == 0
    assert "RUNNING" in result.stdout
    assert "source.ingest" in result.stdout
    assert "Open incidents" in result.stdout
```

Add a conflict-unblock test that starts with immutable old receipts/intents and an integrity-class
`REPAIR_REQUIRED` Action, supplies explicit canonical resolution evidence, and asserts a new plan
version, new action ID, and new staging namespace are created while every old row remains unchanged.
Add a semantic-repair test proving `unblock` rejects the request because that run never entered
`BLOCKED`: its replacement comes only from automatic Planner replan and cannot select/clean conflict
canonical files or mutate old protocol rows.

- [ ] **Step 2: Run lifecycle tests and confirm they fail against old behavior**

Run: `.venv/bin/pytest tests/test_project_model.py tests/test_dynamic_cli.py tests/test_orchestrator_offline.py -v`

Expected: failures reference `pipeline_state.json`, `Status`, and absent CLI commands.

- [ ] **Step 3: Initialize the ledger during scaffold and build services with stable run ID**

Scaffold creates the directory contract, `state/run.db`, `state/staging`, and checkpoint parent. `make_book()` creates one ledger run, writes source, and starts the durable runtime. `resume()` loads the existing run ID and never creates a fresh business run. Observability uses that stable run ID across resumes.

- [ ] **Step 4: Replace status-based CLI commands**

Remove `--until` and the happy-path table. `inspect` prints run status, current plan version, authorized/running/recent Actions, gates, receipts, incidents, budget, and next recovery instruction. `approve` resumes one `PAUSED_HITL` interrupt by ID and records the human decision. `unblock` is only for integrity-class blocked recovery and requires a reason and evidence that canonical conflicts were explicitly selected/cleaned. For a bundle-conflict incident it must append a new plan version and authorize a new action ID/new staging namespace before returning the run to `RUNNING`; it never resets or reuses the old `REPAIR_REQUIRED` Action or changes old receipts/intents/conflicts. It rejects semantic repair facts and may not be used to bypass their automatic policy-mapped replan. A budget-only unblock may transition without replacement work. `cancel` is idempotent and cannot reopen COMPLETED.

- [ ] **Step 5: Rewrite the offline end-to-end test around a deterministic Planner**

The fake Planner proposes eligible capabilities in small patches, fake agent executors write staging artifacts, deterministic validators decide PASS, and the run reaches `RunStatus.COMPLETED`. Assert at least one replan, one conflict-free chapter batch, all required evidence, and no agent-visible state mutation tools.

- [ ] **Step 6: Run lifecycle tests and CLI help smoke tests**

Run: `.venv/bin/pytest tests/test_project_model.py tests/test_dynamic_cli.py tests/test_orchestrator_offline.py -v`

Run: `.venv/bin/abi --help`

Expected: tests pass and help lists `make-book`, `resume`, `inspect`, `approve`, `unblock`, and `cancel`.

- [ ] **Step 7: Commit the new lifecycle**

```bash
git add src/abi/project src/abi/orchestrator src/abi/cli tests/test_project_model.py tests/test_dynamic_cli.py tests/test_orchestrator_offline.py
git commit -m "feat: expose durable dynamic run lifecycle"
```

## Task 11: Remove the Fixed Pipeline and Update Architecture, Reliability, Eval, and Generated Inputs

**Files:**
- Delete: `src/abi/project/state.py`
- Delete: `src/abi/stages/runner.py`
- Delete: `src/abi/stages/validators.py`
- Delete: `src/abi/stages/__init__.py`
- Delete: `src/abi/prompts/stages.py`
- Modify: `src/abi/tools/context.py`
- Modify: `src/abi/eval/trace.py`
- Modify: `src/abi/eval/book.py`
- Modify: `ARCHITECTURE.md`
- Modify: `docs/DESIGN.md`
- Rewrite: `docs/RELIABILITY.md`
- Modify: `docs/design-docs/agentic-pipeline.md`
- Modify: `docs/design-docs/langgraph-and-state-machine.md`
- Modify: `docs/design-docs/dynamic-agent-orchestration.md`
- Modify: `docs/product-specs/cli-and-config.md`
- Modify: `AGENTS.md`
- Modify: `tests/test_tool_boundaries.py`
- Modify: `tests/test_eval.py`

**Interfaces:**
- Consumes: completed dynamic implementation.
- Produces: no fixed macro path in source, L1 eval over ledger events, and documentation whose architecture/flow diagrams match implemented modules.

- [ ] **Step 1: Extend the executable architecture linter with legacy-symbol rules**

```python
def test_architecture_linter_reports_legacy_control_symbols(tmp_path: Path) -> None:
    module = tmp_path / "src/abi/orchestrator/old.py"
    module.parent.mkdir(parents=True)
    module.write_text("HAPPY_PATH = []\n", encoding="utf-8")
    violations = scan_tree(tmp_path / "src/abi")
    assert [(item.rule, item.symbol) for item in violations] == [
        ("fixed-macro-control", "HAPPY_PATH")
    ]
```

- [ ] **Step 2: Run the structural test and confirm old symbols are found**

Run: `.venv/bin/pytest tests/test_tool_boundaries.py -v`

Expected: failure lists the remaining legacy symbols and files.

- [ ] **Step 3: Delete legacy modules and update imports**

Remove the fixed state/stage code and numbered sequence registry. `ToolContext` exposes project, services, run ID, and read-only snapshot callback; it has no `state()` or `save_state()`. Remove every import of `abi.project.state` and `abi.stages`.

- [ ] **Step 4: Rewrite L1 process eval against the ledger**

Gate integrity replays `gate_evidence` against authorization/attempt expected-manifest facts,
attempt outcome receipt, gate receipt, canonical bundle digest, ordered artifact checksums, validator
identity/version, and the complete intent set. Path conformance becomes policy conformance: every
ordinary success had a non-empty caller-canonical exact bundle; outcome receipt preceded controller
handling; gate receipt and every intent were created together before promotion; every intent and
the unified canonical postcheck passed before ledger success; post-success drift created
integrity-class `REPAIR_REQUIRED + BLOCKED`; conflict history was never reset/reused; every release prerequisite
was committed; no failed/paused/indeterminate Action was treated as success except through a valid
immutable probe resolution using durable attempt policy/error facts; and every plan rejection has
reasons. Every automatic retry lineage must show old attempt/Action `RETRY_WAIT`, exactly one
attempt+1 `AUTHORIZED` successor with the same action ID and frozen manifest/policy fingerprint,
fresh staging, and executor attempt IDs that never repeat. Every semantic repair lineage must show
explicit class/source/reason, original attempt/action `REPAIR_REQUIRED`, run remaining `RUNNING`, and
exactly one automatic new plan version/repair action ID/staging without original re-execution; every
integrity or unknown-class lineage must show `BLOCKED`, zero automatic Planner repair, and a new plan/
action only after human resolve/unblock. Keep L2 translation and L3 EPUB scoring behavior unchanged.

- [ ] **Step 5: Synchronize all authoritative documentation**

Update layering, project layout, CLI behavior, checkpoint/recovery matrix, failure semantics, and event schema. Copy the approved Mermaid architecture and flow diagrams from `dynamic-agent-orchestration.md` into the current architecture overview where useful; do not create a second contradictory diagram. Change the design document status to `Implemented` only after the end-to-end tests pass.

- [ ] **Step 6: Run structural, eval, and documentation sanity checks**

Run: `.venv/bin/pytest tests/test_tool_boundaries.py tests/test_eval.py -v`

Run: `git diff --check`

Run: `.venv/bin/python tools/lint/architecture.py src/abi`

Run: `rg -n 'pipeline_state\.json|28.state|HAPPY_PATH|STAGE_SEQUENCE' ARCHITECTURE.md docs`

Expected: tests pass; diff check is clean; the search finds only historical explanation in the approved design document's “old design” discussion and no active instructions or source code.

- [ ] **Step 7: Commit legacy removal and documentation**

```bash
git add -A src/abi tests tools/lint/architecture.py ARCHITECTURE.md docs AGENTS.md
git commit -m "refactor: remove fixed macro workflow"
```

## Task 12: Final Verification and Design Completion Record

**Files:**
- Modify only if verification finds a defect in files owned by Tasks 1–11.
- Update: `docs/design-docs/dynamic-agent-orchestration.md` status and completion evidence.

**Interfaces:**
- Consumes: entire implementation.
- Produces: reproducible verification evidence and a clean branch ready for code review.

- [ ] **Step 1: Run the complete automated test suite from a clean process**

Run: `.venv/bin/pytest`

Expected: all tests pass with no unexpected skips.

- [ ] **Step 2: Run lint and type checks**

Run: `.venv/bin/ruff check src tests`

Run: `.venv/bin/mypy --python-version 3.12 src`

Expected: Ruff passes. Mypy must contain no new errors; the pre-existing 20-error baseline recorded in commit `fa0bd60` must be reduced to zero for files modified by this plan, and any untouched baseline errors must be listed explicitly in the completion evidence.

- [ ] **Step 3: Run recovery, control-plane, and offline acceptance tests separately**

Run: `.venv/bin/pytest tests/test_orchestration_recovery.py tests/test_failure_semantics.py tests/test_dynamic_controller.py tests/test_orchestrator_offline.py -v`

Expected: all two-entry boundaries before/after outcome receipt, before/after atomic gate receipt +
intents, each promotion, unified postcheck, success/checkpoint, partial-intent corruption,
pre/post-success drift, immutable conflict/unblock replacement, permanent failures, all three probe
dispositions using durable policy/error facts, replay conflicts, concurrency, and completion pass.
The same set must cover mapped semantic `term_drift`/validator repair at every repair-fact and replan
crash boundary, integrity-class repair, missing/unknown classification fail-closed, and semantic
repair refusal to touch conflict evidence.
The recovery set must also cover ordinary retryable and allowed probe-`absent` transitions at
`after_retry_wait`, before/after next-attempt creation, before first dispatch, and after
`AUTHORIZED → RUNNING`: concurrent/repeated ticks create only attempt 2, the normal executor log is
exactly attempts `1, 2`, and no `RUNNING`/`INDETERMINATE`/`REPAIR_REQUIRED` attempt is re-entered.

- [ ] **Step 4: Verify forbidden symbols and SDK boundaries**

Run: `rg -n 'create_react_agent|HAPPY_PATH|STAGE_SEQUENCE|StageSpec|def set_state|def record_gate' src tests`

Run: `rg -n '^(from|import) (langchain|langgraph|langfuse)' src/abi --glob '!src/abi/providers/**'`

Run: `rg -n 'Succeeded\x28staging_relpath|ProbeR[e]sult' src tests docs/superpowers/plans/2026-08-04-constrained-dynamic-orchestration.md`

Run: `rg -n 'from abi\.project\.artifact_paths|import abi\.project\.artifact_paths' src/abi/types`

Expected: all four searches return no matches. References to per-entry `staged_relpath` inside
`ArtifactBundleEntry` remain valid; only the superseded single-file outcome is forbidden.

- [ ] **Step 5: Record implementation evidence in the design document**

Add the implementation commit range, test counts, dependency versions from `uv.lock`, checkpoint database path, ledger schema version, and the exact recovery scenarios exercised. Set status to `Implemented` only now.

- [ ] **Step 6: Commit the verification record**

```bash
git add docs/design-docs/dynamic-agent-orchestration.md
git commit -m "docs: record dynamic orchestration verification"
```

- [ ] **Step 7: Request code review before integration**

Invoke `superpowers:requesting-code-review`, review the full diff from `main...codex/research-agent-loop`, resolve findings through `superpowers:receiving-code-review`, then rerun Steps 1–4 before using `superpowers:finishing-a-development-branch`.
