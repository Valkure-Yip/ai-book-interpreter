"""SQLite schema for the durable orchestration business ledger."""

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    book_slug TEXT NOT NULL,
    source_lang TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    source_target TEXT NOT NULL,
    publication_mode TEXT NOT NULL,
    profile TEXT,
    budget_usd REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_versions (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    version INTEGER NOT NULL,
    objective TEXT NOT NULL,
    rationale TEXT NOT NULL,
    patch_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, version)
);

CREATE TABLE IF NOT EXISTS plan_rejections (
    rejection_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    plan_version INTEGER NOT NULL,
    reason_codes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id, plan_version) REFERENCES plan_versions(run_id, version)
);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    plan_version INTEGER NOT NULL,
    capability TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    dependencies_json TEXT NOT NULL,
    priority INTEGER NOT NULL,
    read_set_json TEXT NOT NULL,
    write_set_json TEXT NOT NULL,
    status TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    expected_manifest_json TEXT NOT NULL,
    expected_manifest_digest TEXT NOT NULL,
    expected_evidence_refs_json TEXT NOT NULL,
    retry_policy_json TEXT NOT NULL,
    retry_policy_fingerprint TEXT NOT NULL,
    failure_signature TEXT,
    repair_class TEXT,
    repair_source TEXT,
    reason_code TEXT,
    committed_at TEXT,
    commit_signature TEXT,
    UNIQUE (run_id, action_id)
);

CREATE TABLE IF NOT EXISTS action_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    expected_manifest_json TEXT NOT NULL,
    expected_manifest_digest TEXT NOT NULL,
    expected_evidence_refs_json TEXT NOT NULL,
    retry_policy_json TEXT NOT NULL,
    retry_policy_fingerprint TEXT NOT NULL,
    retry_of_attempt INTEGER,
    staging_relpath TEXT NOT NULL,
    failure_signature TEXT,
    repair_class TEXT,
    repair_source TEXT,
    reason_code TEXT,
    started_at TEXT,
    finished_at TEXT,
    UNIQUE (action_id, attempt),
    UNIQUE (action_id, retry_of_attempt)
);

CREATE TABLE IF NOT EXISTS probe_bindings (
    original_action_id TEXT NOT NULL REFERENCES actions(action_id),
    original_attempt INTEGER NOT NULL,
    operation_key TEXT NOT NULL,
    probe_capability TEXT NOT NULL,
    probe_action_id TEXT NOT NULL UNIQUE REFERENCES actions(action_id),
    plan_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (original_action_id, original_attempt),
    UNIQUE (original_action_id, original_attempt, operation_key, probe_capability),
    FOREIGN KEY (original_action_id, original_attempt)
        REFERENCES action_attempts(action_id, attempt)
);

CREATE TABLE IF NOT EXISTS probe_resolutions (
    original_action_id TEXT NOT NULL,
    original_attempt INTEGER NOT NULL,
    probe_action_id TEXT NOT NULL,
    probe_attempt INTEGER NOT NULL,
    operation_key TEXT NOT NULL,
    disposition TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    message TEXT NOT NULL,
    original_idempotency_key TEXT NOT NULL,
    retry_policy_json TEXT NOT NULL,
    retry_policy_fingerprint TEXT NOT NULL,
    error_code TEXT NOT NULL,
    failure_signature TEXT NOT NULL,
    resolution_digest TEXT NOT NULL,
    resolved_at TEXT NOT NULL,
    PRIMARY KEY (original_action_id, original_attempt),
    UNIQUE (probe_action_id, probe_attempt),
    FOREIGN KEY (original_action_id, original_attempt)
        REFERENCES action_attempts(action_id, attempt),
    FOREIGN KEY (probe_action_id, probe_attempt)
        REFERENCES action_attempts(action_id, attempt)
);

CREATE TABLE IF NOT EXISTS attempt_outcome_receipts (
    action_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    canonical_outcome_json TEXT NOT NULL,
    outcome_digest TEXT NOT NULL,
    canonical_bundle_json TEXT,
    bundle_digest TEXT,
    evidence_refs_json TEXT NOT NULL,
    error_code TEXT,
    failure_signature TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (action_id, attempt),
    FOREIGN KEY (action_id, attempt) REFERENCES action_attempts(action_id, attempt)
);

CREATE TABLE IF NOT EXISTS gate_receipts (
    action_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    validator_id TEXT NOT NULL,
    validator_version TEXT NOT NULL,
    canonical_gate_decision_json TEXT NOT NULL,
    gate_decision_digest TEXT NOT NULL,
    bundle_digest TEXT NOT NULL,
    artifacts_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (action_id, attempt),
    FOREIGN KEY (action_id, attempt) REFERENCES action_attempts(action_id, attempt)
);

CREATE TABLE IF NOT EXISTS validator_failure_receipts (
    action_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    validator_id TEXT NOT NULL,
    validator_version TEXT NOT NULL,
    canonical_gate_decision_json TEXT NOT NULL,
    gate_decision_digest TEXT NOT NULL,
    bundle_digest TEXT NOT NULL,
    artifact_checksums_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (action_id, attempt),
    FOREIGN KEY (action_id, attempt) REFERENCES action_attempts(action_id, attempt)
);

CREATE TABLE IF NOT EXISTS repair_facts (
    action_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    repair_class TEXT NOT NULL,
    repair_source TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    defect_codes_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    message TEXT NOT NULL,
    outcome_digest TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (action_id, attempt),
    FOREIGN KEY (action_id, attempt) REFERENCES action_attempts(action_id, attempt)
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    attempt INTEGER NOT NULL,
    canonical_relpath TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL,
    committed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promotion_intents (
    intent_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    attempt INTEGER NOT NULL,
    staged_relpath TEXT NOT NULL,
    canonical_relpath TEXT NOT NULL,
    checksum TEXT NOT NULL,
    media_type TEXT NOT NULL,
    evidence_role TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    bundle_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    committed_at TEXT,
    UNIQUE (action_id, attempt, staged_relpath, canonical_relpath),
    UNIQUE (action_id, attempt, ordinal),
    UNIQUE (canonical_relpath)
);

CREATE TABLE IF NOT EXISTS gate_evidence (
    evidence_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    gate TEXT NOT NULL,
    passed INTEGER NOT NULL,
    validator_version TEXT NOT NULL,
    artifact_checksums_json TEXT NOT NULL,
    committed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    action_id TEXT REFERENCES actions(action_id),
    error_code TEXT NOT NULL,
    subject TEXT,
    message TEXT NOT NULL,
    repair_class TEXT,
    repair_source TEXT,
    reason_code TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS open_promotion_incident_subject
ON incidents(action_id, error_code, subject)
WHERE status = 'OPEN' AND subject IS NOT NULL;

CREATE TABLE IF NOT EXISTS interrupts (
    interrupt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    attempt INTEGER NOT NULL,
    thread_id TEXT NOT NULL,
    pause_outcome_digest TEXT NOT NULL,
    pending_interrupt_json TEXT NOT NULL,
    decisions_json TEXT NOT NULL,
    decision_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    continuation_sequence INTEGER,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS hitl_continuation_receipts (
    continuation_id TEXT PRIMARY KEY,
    interrupt_id TEXT NOT NULL UNIQUE REFERENCES interrupts(interrupt_id),
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    attempt INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    canonical_outcome_json TEXT NOT NULL,
    outcome_digest TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (action_id, attempt, sequence)
);

CREATE TABLE IF NOT EXISTS budget_entries (
    entry_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    action_id TEXT REFERENCES actions(action_id),
    attempt INTEGER,
    amount_usd REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unblock_resolutions (
    resolution_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    source_action_id TEXT REFERENCES actions(action_id),
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    plan_version INTEGER,
    replacement_action_id TEXT REFERENCES actions(action_id),
    staging_relpath TEXT,
    resolved_at TEXT NOT NULL,
    UNIQUE (run_id, request_digest)
);

CREATE TABLE IF NOT EXISTS event_outbox (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    event_name TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
"""
