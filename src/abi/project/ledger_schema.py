"""SQLite schema for the durable orchestration business ledger."""


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
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
    failure_signature TEXT,
    committed_at TEXT,
    commit_signature TEXT,
    UNIQUE (run_id, action_id)
);

CREATE TABLE IF NOT EXISTS action_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL REFERENCES actions(action_id),
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    failure_signature TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (action_id, attempt)
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
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    committed_at TEXT,
    UNIQUE (action_id, attempt, staged_relpath, canonical_relpath),
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
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS interrupts (
    interrupt_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS budget_entries (
    entry_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    action_id TEXT REFERENCES actions(action_id),
    attempt INTEGER,
    amount_usd REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_outbox (
    event_id TEXT PRIMARY KEY,
    event_name TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
"""
