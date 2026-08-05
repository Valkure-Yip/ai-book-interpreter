# Task 8 Phase A Report: Typed Action Artifact Bundles

## Status

DONE — the Phase A entry gate is green. All 17 registered capabilities are bound to exact,
parameter-expanded artifact manifests and attempt-only output writers. Task 8 controller and
scheduler implementation remains outside this commit.

## Protocol implemented

- Added strict portable canonical artifact keys, typed expected manifests, typed ordered bundles,
  receipt/gate facts, retry fingerprints, and explicit repair/integrity classifications.
- Added `AttemptStagingWriter` and an in-memory `BufferedAttemptWriter`; composite spot-check work
  flushes to attempt staging only after its complete exact bundle matches the authorized manifest.
- Migrated deterministic ingest, split, EPUB build/check/lint, release, filesystem tools, gate
  tools, and reviewer subagents to attempt-only writes.
- Added a staging evidence overlay that exposes current-attempt outputs plus checksum-verified
  committed dependencies only.
- Added exact source split inputs and exact spot-check round/reviewer/chapter/sample/seed inputs.
- Spot-check sampling and validation are pure before writes; the validator recomputes current and
  consecutive prior quality from reviewer summaries rather than trusting a PASS report.
- Migrated provider outcome construction to the breaker outcome types without compatibility
  defaults or legacy `Succeeded(staging_relpath=...)` support.

## 17-capability audit

All 17 capabilities pass the Phase A enforcement audit:

1. `source.ingest`
2. `source.split`
3. `research.global`
4. `research.book`
5. `translation.trial`
6. `glossary.prepare`
7. `chapter.translate`
8. `chapter.control`
9. `chapter.review`
10. `preproduction.spec`
11. `preproduction.sample`
12. `epub.build`
13. `review.spotcheck`
14. `review.independent`
15. `release.prepare`
16. `output.finalize`
17. `retrospective.capture`

Agent/composite capabilities use the writer-backed tool belt and compare their recorded bundle
exactly before returning success. Deterministic capabilities write directly through the attempt
writer. Any missing, extra, reordered, or metadata/role/media-mismatched effect fails closed.

## Verification

- Tasks 1/3/4 plus amended Task 7 entry gate:
  `204 passed, 5 warnings`.
- Phase A focused ledger/artifact/action/tool suite:
  `191 passed, 4 warnings`.
- Real two-reviewer composite executor:
  `1 passed`; selection, two isolated reviewers, validation, exact comparison, and canonical-order
  flush completed; both pre-flush staging snapshots were empty and all ten outputs existed only in
  attempt staging after flush.
- Provider Action runtime after breaker migration: `90 passed`.
- Provider/offline/tracked-planner combined: `95 passed`.
- Architecture linter: PASS.
- Full Ruff: PASS.
- Python 3.12 strict mypy over 24 Phase A source files: PASS.
- `git diff --check`: PASS.
- Full pytest entry snapshot: `398 passed, 12 failed, 20 warnings`; the only failures are the
  explicitly deferred, untracked Phase B files `tests/test_dynamic_controller.py` (9) and
  `tests/test_scheduler.py` (3). No tracked or Phase A/provider/planner/offline test fails.

## Source and coverage audit

- Registered capabilities: 17; Action allowlist intersection with legacy direct content writers
  (`ingest_source`, `split_source`): empty.
- `Succeeded(staging_relpath=...)` in `src/`: zero.
- Action-reachable temporary/shadow-project writers: zero.
- Direct canonical writes in Action/gate/fs/subagent paths: zero. Legacy content helpers still
  contain canonical writers but are absent from every registered Action allowlist.
- Test diff: 1,693 insertions / 361 deletions across 15 tracked test files; no test file deleted.
- Baseline tracked test functions: 262. Current tracked/Phase-A test functions excluding the two
  deferred untracked Phase B files: 283. Coverage was preserved and expanded by 21 tests.

## Worktree separation

The commit stages Phase A action/type/project/EPUB/QA/tool/provider migrations and their tests.
It intentionally excludes the existing Task 8 orchestrator/controller/dispatcher/projector/
reconciler, scheduler, orchestration-runtime, observability, and the two untracked Phase B test
files. `providers/agent_runtime/runner.py` is included because the breaker types require its old
outcome constructors to be migrated; its existing stable Action identity/checkpoint hunks remain
in the same file and are documented here as approved by the parent task.

## Review fix round 1/5 — receipt, bundle, and semantic-gate hardening

### RED evidence

- Full receipt parsing/binding and exact spot-check reviewer protocol:
  `tests/test_orchestration_types.py` produced 5 failures because skeletal outcome/gate facts and
  non-exact reviewer sets were accepted.
- Ledger protocol:
  the five focused tests for receipt-only persistence, separate retry routing, commit bypass,
  exact replay identity, and receipt-bound finish all failed before implementation. The direct
  commit path succeeded without receipts/intents; intent replay compared only count; retry denial
  rolled its receipt back.
- Pinned evidence reads:
  the symlinked committed-parent and hash/read TOCTOU tests both failed (`2 failed`).
- Semantic validators:
  `.venv/bin/python -m pytest -q tests/test_action_validators.py -k
  'source_split_validator_exactly or semantic_validators_reject'` produced `11 failed`; generic
  existence checks accepted empty or incomplete exact bundles.
- Removed single-file protocol:
  `.venv/bin/python -m pytest -q tests/test_artifact_promotion.py -k
  single_file_promotion_apis` produced `1 failed` while the legacy ledger/store APIs remained.

### GREEN implementation and evidence

- `AttemptOutcomeReceiptPayload` now parses canonical `ActionOutcomeEnvelope`; `GateReceiptPayload`
  parses canonical `GateDecision`. Both bind outer identity and every embedded bundle, evidence,
  checksum, error, and failure-signature fact.
- `record_attempt_outcome` is an immutable insert-only handoff. `route_retry_from_receipt` applies
  the frozen retry policy in a separate transaction, so denied routing cannot erase the receipt.
- Gate replay compares the complete ordered intent identity. `verify_committed_bundle` binds the
  receipt, PASS gate, durable manifest, exact outcome bundle, and all `COMMITTED` intents;
  `commit_success` rejects any missing or divergent prerequisite. The legacy
  `create_promotion_intent` / `prepare_promotion` single-file APIs were removed.
- `finish_attempt` requires a typed receipt whose outcome maps exactly to the requested state;
  indeterminate signatures must match and repair completion requires a repair fact.
  `record_repair_required` accepts typed `RepairClass` / `RepairSource`, binds all fields to the
  receipt, and normalizes unknown or divergent sources to
  `integrity/integrity_guard/repair_class_unknown`, blocking the run.
- `StagingEvidenceView` pins the project root inode, opens every path component no-follow through
  directory file descriptors, requires a regular leaf, and derives bytes and checksum from the
  same open descriptor.
- Dedicated semantic gates were restored for source splitting, research, trials, glossary,
  chapter review, preproduction, independent review, finalization, and retrospective evidence.
  Source TOC slug/src order must exactly equal frozen `expected_chapters`; chapter and independent
  review manifests now include their gate/reviewer evidence. Spot-check reviewers are exactly
  `agent_a` and `agent_b`.
- Focused receipt/evidence tests: `7 passed`; focused ledger suite: `9 passed`; semantic suite:
  `11 passed`; actual ledger bypass, removed APIs, and repair normalization: `9 passed`.
- Tasks 1/3/4 plus amended Task 7: `233 passed, 5 warnings`.
- All tracked/Phase-A tests excluding the two untracked Phase B files:
  `419 passed, 20 warnings`.
- Architecture linter: PASS. Full Ruff: PASS. Python 3.12 strict mypy over 26 Phase A source files:
  PASS. `git diff --check`: PASS.

The deferred Phase B committer and controller tests still call the removed single-file promotion
surface. Those dirty/untracked files are intentionally excluded from this fix commit and must be
rewritten to use the gate-receipt plus complete-bundle-intent protocol in Phase B.
