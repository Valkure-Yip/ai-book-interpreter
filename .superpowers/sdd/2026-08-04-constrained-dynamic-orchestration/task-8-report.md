# Task 8 Report — Scheduler, Dispatcher, Committer, and Dynamic Controller

## Status

BLOCKED — the Task 8 control plane and independent safety fixes pass verification, but the
Task 7 built-in artifact ABI cannot satisfy Task 8's staging/promotion contract, and the plan
does not define a typed probe-resolution outcome. The work is intentionally uncommitted pending
an explicit cross-Task contract revision.

## Implementation summary

- Added a stable priority/action-ID `Scheduler` that admits only currently eligible Actions whose dependencies are committed and whose batch read/write sets do not conflict.
- Added typed `Dispatcher` execution through `ActionRegistry.resolve_json()`, durable attempt creation, per-Action timeout, stable Action identity, and controller-owned frozen `GateRuntimeMetadata`.
- Added `Committer` validation, Task 4 promotion-intent/create-only promotion, deterministic gate evidence, and exactly-once `RunLedger.commit_success()` integration.
- Added `Reconciler` startup-cycle reconciliation for pending/committed/conflicting promotions, safe retry of interrupted side-effect-free attempts, fail-closed handling of interrupted side effects, and post-promotion business-commit recovery.
- Added ordered `OutboxProjector` delivery to `events.jsonl`, status/metrics projection refresh, durable delivered markers, and restart-safe event-ID deduplication in `EventLogger`.
- Added `DynamicController.tick()` for propose → authorize → schedule → dispatch → classify → validate/promote/commit, including bounded retry, repair replan, explicit alternatives, registered probes, exact pause mapping, permanent BLOCKED routing, and max-cycle incidents.
- Added provider-only `DurableLoopRuntime` with `LoopState(run_id, cycle, continue_run)`, `AsyncSqliteSaver`, stable `thread_id=run_id`, and the Task 6 ABI-owned single-process checkpoint lifecycle.
- Added a dynamic driver while retaining the pre-Task-10 legacy CLI adapter.

## Design choices

- `state/run.db` remains the sole business authority. LangGraph stores only the three-field runtime cursor.
- Outbox rows for plan/action/incident/run transitions are inserted in the same SQLite transaction as their business fact. Projection happens later and is idempotent across the append/delivered crash window.
- Dispatcher re-parses canonical durable JSON through the registry immediately before execution; unresolved capability/input/outcome/classification fails closed with a repair instruction.
- Action execution is at-least-once; success commit identities remain stable across run/plan/action/attempt, and reconciled committed promotions can finish the business commit without re-executing the Action.
- Interrupted work with no registered external side effect returns to `RETRY_WAIT`; possible side effects become indeterminate/BLOCKED and may not be retried without the registered read-only probe.
- Canonical artifacts use Task 4's portable namespace and promotion intents; no filesystem/SQLite atomicity claim or second staging protocol was introduced.

## RED / GREEN record

1. Initial RED
   - Command: `.venv/bin/pytest tests/test_scheduler.py tests/test_dynamic_controller.py -v`
   - Result: collection failed exactly because `abi.planning.scheduler` and `abi.orchestrator.committer` did not exist (`0 collected / 2 errors`).
2. Initial GREEN
   - Command: `.venv/bin/pytest tests/test_scheduler.py tests/test_dynamic_controller.py -v`
   - Result: `11 passed`.
3. Focused control-plane GREEN
   - Command: `.venv/bin/pytest tests/test_scheduler.py tests/test_dynamic_controller.py tests/test_policy_engine.py tests/test_run_ledger.py -v`
   - Result: `81 passed`.
4. Regression RED
   - Command: `.venv/bin/pytest tests/test_agent_runtime.py::test_llm_callback_finalizes_each_run_id_only_once -v`
   - Result: failed because new stable callback identity parameters were required by an existing direct constructor.
   - GREEN: added safe defaults for direct construction while production runtime supplies stable IDs; targeted test passed and extended suite reached `185 passed`.
5. Crash-window RED
   - Command: `.venv/bin/pytest tests/test_dynamic_controller.py::test_reconciler_retries_interrupted_side_effect_free_attempt -v`
   - Result: expected 2 attempts, observed 1; a crash after `start_attempt` left Action `RUNNING`.
   - GREEN: Reconciler now converts safe interrupted work to `RETRY_WAIT`; targeted test passed.
6. Promotion/business-commit crash recovery
   - Command: `.venv/bin/pytest tests/test_dynamic_controller.py::test_reconciler_finishes_business_commit_after_promotion_crash -v`
   - Result: passed; committed canonical promotion finished the ledger success commit with one execution attempt.
7. Extended focused GREEN
   - Command: `.venv/bin/pytest tests/test_scheduler.py tests/test_dynamic_controller.py tests/test_policy_engine.py tests/test_run_ledger.py tests/test_artifact_promotion.py -q`
   - Result: `130 passed`.

## Complete verification

- Architecture SDK boundary: `.venv/bin/python tools/lint/architecture.py src/abi` — exit 0.
- Full Ruff: `.venv/bin/ruff check src tests` — all checks passed.
- Relevant Python 3.12 strict mypy: `.venv/bin/mypy --strict --python-version 3.12 ...` over 20 changed/related source and test files — success, no issues.
- Full pytest: `.venv/bin/pytest -q` — `373 passed`, 18 pre-existing `datetime.utcnow()` deprecation warnings in IR/TOC code outside Task 8.
- Whitespace: `git diff --check` — exit 0.

## Changed files

- `src/abi/actions/builtins/catalog.py`
- `src/abi/actions/contracts.py`
- `src/abi/actions/registry.py`
- `src/abi/orchestrator/__init__.py`
- `src/abi/orchestrator/committer.py`
- `src/abi/orchestrator/controller.py`
- `src/abi/orchestrator/dispatcher.py`
- `src/abi/orchestrator/driver.py`
- `src/abi/orchestrator/projector.py`
- `src/abi/orchestrator/reconcile.py`
- `src/abi/planning/scheduler.py`
- `src/abi/project/run_ledger.py`
- `src/abi/project/ledger_schema.py`
- `src/abi/providers/agent_runtime/runner.py`
- `src/abi/providers/llm/factory.py`
- `src/abi/providers/observability/events.py`
- `src/abi/providers/orchestration_runtime/__init__.py`
- `src/abi/providers/orchestration_runtime/runtime.py`
- `src/abi/types/orchestration.py`
- `tests/test_dynamic_controller.py`
- `tests/test_scheduler.py`

## Independent review and blocker

The independent review rejected merge despite the green suite because its synthetic executor
writes exactly one staged file, while the real built-in catalog does not:

1. Task 7 built-ins write canonical paths before Task 8 creates a promotion intent. Several
   Actions produce directories or multiple files, while `Succeeded` exposes one
   `staging_relpath` and `ActionSpec` exposes only a broad effect string. A correct revision needs
   a typed artifact bundle (`staged_relpath`, exact canonical destination, media type per regular
   file), attempt-staging-only tool writes, parameter-expanded effects, and validators that check
   that staged bundle before promotion. Adopting already-written canonical files is explicitly
   rejected because it destroys the intent-before-side-effect and crash-recovery guarantees.
2. A probe is authorized as an ordinary Action, but no typed result states whether the original
   operation succeeded remotely, was absent and may be retried, or remains unknown. A correct
   revision needs a typed `ProbeResolution`, an atomic ledger transition for the original
   `INDETERMINATE` attempt, and an evidence-only probe commit contract. Treating ordinary
   `Succeeded` as implicit resolution is ambiguous and unsafe.

No commit was created because these are merge-blocking protocol gaps, not optional follow-up
polish.

## Concerns

- Full pytest still reports 18 existing `datetime.utcnow()` deprecation warnings in unrelated IR/TOC files; Task 8 introduces no new warnings.
- Deferred checkpoint archival/compaction remains intentionally out of scope for Tasks 9/11.
- The remaining artifact/probe work changes shared Task 7/8/9 types and execution semantics; it
  should be approved as a plan revision before implementation.

## Protocol Amendment Implementation

Phase B replaces the superseded in-memory/single-file controller path with a receipt-driven,
policy-gated loop. Dispatcher claims an explicit authorized attempt, writes only to its staging
namespace, classifies the typed outcome before persisting the immutable receipt, and never lets the
Controller route from the returned Python object. Reconciler reloads the receipt discriminant and
is the only normal routing authority. Ordinary retry closes attempt N as `RETRY_WAIT`, creates one
durable `AUTHORIZED` N+1 successor, and never re-enters N. Mapped semantic repair preserves the old
Action and creates exactly one new plan/action; integrity and unknown classifications block without
Planner replacement.

Ordinary success now follows the complete bundle protocol: exact pinned-dirfd staging inventory,
staging-aware validation, one transaction for the PASS gate receipt plus every promotion intent,
create-only per-entry promotion, unified postcheck, then one ledger success transaction. A failed
validator decision is instead stored in `validator_failure_receipts` as canonical raw evidence in
the same transaction as its repair fact/incident/outbox. It cannot create a PASS receipt or intent.
Exact failed-decision replay is idempotent; a different replay preserves the first fact and blocks
through independent `gate_binding_conflict` compensation.

Task 8 also implements the bounded probe handoff without consuming Task 9's atomic resolver scope.
An `INDETERMINATE` Action creates one controller-owned, Policy-authorized, read-only probe bound by
`ProbeActionInput` to original action/attempt/operation/capability. Dispatcher persists a valid raw
`ProbeResolution`; mismatched or ordinary outcomes become integrity receipts before persistence.
The probe is evidence-only completed, the original remains `INDETERMINATE`, and the run blocks as
`probe_resolution_pending` or `probe_resolution_unknown` until Task 9 atomically resolves it.

TDD and recovery evidence:

- Initial scheduler/controller baseline: 12 failed, 3 passed. The final dynamic-controller suite is
  58 passed, including nine two-entry receipt/gate/copy/postcheck/success crash boundaries, four
  retry-successor boundaries, four semantic-repair fact/plan/authorization boundaries, seven unsafe
  staging/bundle cases, and five probe-boundary cases.
- Validator failure RED: six failures at the missing typed API; GREEN: six binding/replay cases plus
  mapped/unmapped end-to-end integration with zero PASS receipts, intents, artifact facts, or
  canonical writes for the failed Action.
- Outbox restart RED exposed a regressing metrics watermark (`3 -> 2`); GREEN uses the run-scoped
  monotonic ledger event sequence and preserves JSONL/status/metrics across append-before-delivered
  replay without duplication.
- Focused control plane: 161 passed with one pre-existing IR `utcnow()` warning. Provider runtime
  regressions: 100 passed. Full tracked suite: 494 passed with 21 pre-existing `utcnow()` warnings.
- Architecture linter and full Ruff pass. Strict mypy passes for 18 changed production files and,
  after typing the new tests, for a 21-file production plus Phase-B-test scope. `git diff --check`
  passes. Legacy single-file/promotion API scans have zero Phase-B matches.

No Task 9 `probe_resolutions` row or original-attempt disposition is created here; the pending raw
receipt is intentionally preserved for that atomic resolver and its full disposition/crash matrix.

## Phase B review fix round 1/5 — uncertain effects, probe serialization, durable incidents, and provider identity

### RED evidence

- Side-effect timeout: `.venv/bin/pytest tests/test_dynamic_controller.py -q -k
  'side_effecting_timeout or side_effect_free_timeout'` produced `1 failed, 1 passed, 59
  deselected`. A timeout on a retryable side-effecting capability was incorrectly receipted as
  `RetryableFailure(provider_timeout)` instead of `Indeterminate`; the side-effect-free bounded
  retry already passed.
- Probe concurrency: `.venv/bin/pytest
  tests/test_dynamic_controller.py::test_concurrent_ticks_authorize_exactly_one_durable_probe_binding
  -q` produced `1 failed`. Eight barrier-aligned ticks raced the check/append/authorize sequence,
  producing stale transition failures and a blocked run instead of one durable binding.
- Commit boundary exceptions: `.venv/bin/pytest
  tests/test_dynamic_controller.py::test_committer_boundary_exception_durably_compensates_once -q`
  produced `2 failed` (`registry`, `validator`). Both ordinary `Exception`s escaped after the
  immutable success receipt and staging existed, without durable compensation.
- Repair incident projection: the focused bundle/outcome/validator/probe command produced `14
  failed, 49 deselected`. Every durable `incident.created` payload omitted the incident row's exact
  `repair_class`, `repair_source`, and `reason_code`.
- Provider event identity: `.venv/bin/pytest tests/test_llm_router_events.py -q` produced `1 failed`.
  Two legitimate same-agent/same-prompt/same-attempt calls yielded observability tuple
  `(event_count, distinct_event_ids, distinct_call_ids, metrics_calls) = (1, 1, 1, 2)` instead of
  `(2, 2, 2, 2)`.

### GREEN implementation and evidence

- Dispatcher now uses the existing canonical failure-signature helper and derives the operation
  key only from frozen action ID, attempt, authorized capability, and authorized canonical
  parameters. A side-effecting timeout is always `Indeterminate` and cannot enter retry; a
  side-effect-free timeout still follows the frozen bounded retry policy.
- `probe_bindings` gives each original action/attempt one ledger-enforced binding and also uniquely
  constrains the full original-action/original-attempt/operation/capability tuple. The controller
  submits an already Policy-authorized deterministic probe to one `BEGIN IMMEDIATE` ledger method,
  which revalidates the indeterminate receipt and current plan facts and atomically inserts the
  plan, outbox facts, Action, authorized attempt, and binding. Exact replay returns the existing
  binding; a divergent binding is independently compensated as integrity. A dedicated durable
  claim-conflict path prevents concurrent losers from redispatching or planning over `RUNNING`
  work.
- The eight-tick barrier plus three repeated blocked-run ticks pass with exactly 2 plans, 2 Actions,
  1 probe binding, 1 attempt and dispatch for the original, 1 attempt/dispatch/outcome receipt for
  the probe, and outbox counts: `plan.proposed=2`, `plan.authorized=2`,
  `action.authorized=2`, `action.started=2`, `action.outcome=2`, `action.committed=1`, and
  `incident.created=1`.
- Committer catches only ordinary `Exception` around registry resolution/validator execution,
  durably records stable `gate_binding_conflict` integrity compensation, preserves receipt and
  staging, and then raises `ArtifactConflictError` for Reconciler's existing recovery boundary.
  Repeated reconciliation is idempotent.
- Repair classifications are now written on the incident's initial INSERT and copied into the one
  `incident.created` outbox payload in the same transaction. The post-insert classification UPDATE
  paths were removed.
- `invoke_structured` allocates one logical invocation identity at entry, reuses it across parse
  attempts, and forms call/event IDs from logical identity plus attempt. The durable Planner caller
  supplies `planner:{run_id}:plan:{next_version}`; fallback identities distinguish actual calls.
  Metrics and budget accounting remain at two for two actual provider calls.

Verification after the fix:

- Consolidated five-finding selection: `19 passed, 44 deselected`; router collision regression:
  `1 passed`.
- Authoritative focused set (`scheduler`, `dynamic_controller`, `policy_engine`, `run_ledger`,
  `action_registry`, `llm_router_events`): `150 passed, 1` pre-existing IR `utcnow()` warning.
- Full dynamic controller: `63 passed, 1` pre-existing warning. Provider runtime plus the new router
  regression: `91 passed` (`90` existing `test_agent_runtime` cases plus `1` router case; the prior
  report's `100` used a wider provider/offline/planner command scope). Planner/router affected
  tests: `5 passed`.
- Full pytest: `500 passed, 21` pre-existing `utcnow()` warnings.
- Architecture SDK boundary: exit 0. Full Ruff: PASS. Python 3.12 strict mypy over the 10 changed
  production/test files: PASS. `git diff --check`: PASS.
- Phase-B legacy scans for `prepare_promotion`, `Succeeded(staging_relpath`, and canonical-project
  validator invocation forms each returned exit 1 with no matches. Production
  `logical_invocation_id` coverage shows the provider allocation/ID construction and the durable
  Planner caller.

## Phase B review fix round 2/5 — exact probe-binding replay across newer snapshots

### RED evidence

`.venv/bin/pytest
tests/test_dynamic_controller.py::test_probe_binding_replay_ignores_newer_speculative_plan_identity
-q` produced `1 failed`. After plan 2/action 2 durably owned the exact
`ProbeActionInput`, replaying that same binding from a newer policy snapshot supplied a valid but
speculative plan 3/action 3. The ledger compared those speculative identities to the stored row,
misclassified exact replay as `_ProbeBindingConflict`, durably blocked the run, and raised
`LedgerConflictError`.

### GREEN implementation and evidence

- Existing-binding identity is now only the durable original-action/original-attempt lookup plus
  stored operation key, probe capability, and the stored probe Action's capability/canonical
  parameters. Once those facts match, `authorize_probe_action` returns the stored Action with
  `created=False` before inspecting the loser's speculative patch/action/plan identity.
- Different operation keys or probe capabilities for the same original attempt still use the
  existing independent `probe_binding_conflict` integrity compensation and block fail-closed.
- Direct exact replay, both conflict dimensions, and the eight-tick barrier pass together: `4
  passed`. Exact replay leaves 2 plans, 2 Actions, 1 probe binding, no new attempt, incident, or
  outbox row, and preserves the original Action as `INDETERMINATE` with the run `RUNNING`.
- Full dynamic controller: `66 passed, 1` pre-existing IR `utcnow()` warning. Authoritative focused
  six-file set: `153 passed, 1` pre-existing warning. Full pytest: `503 passed, 21` pre-existing
  `utcnow()` warnings.
- Architecture SDK boundary: exit 0. Full Ruff: PASS. Python 3.12 strict mypy over the two changed
  production/test files: PASS. `git diff --check`: PASS.
