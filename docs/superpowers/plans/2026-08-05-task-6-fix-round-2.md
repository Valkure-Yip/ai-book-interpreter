# Task 6 Fix Round 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the durable LangChain v1 Action harness at tool-start, HITL, provider-classification, callback-idempotency, and checkpoint-resume boundaries.

**Architecture:** Keep every SDK type and conversion inside `providers.agent_runtime`. Count actual business execution through request-scoped wrappers around ABI handlers, represent multi-tool human decisions with frozen ABI models, classify status errors by HTTP status, and translate invalid resume states to `RepairRequired`.

**Tech Stack:** Python 3.12, Pydantic v2, LangChain v1, LangGraph v1, AsyncSqliteSaver, pytest.

## Global Constraints

- Strict RED/GREEN TDD for each behavior change.
- No LangChain or LangGraph types outside `src/abi/providers/`.
- No compatibility requirement for the former single-decision HITL input.
- Preserve the SDK-free public `AgentRuntime` constructor and async callable support.

---

### Task 1: Actual business tool starts

**Files:**
- Modify: `src/abi/providers/agent_runtime/tooling.py`
- Modify: `src/abi/providers/agent_runtime/runner.py`
- Test: `tests/test_agent_runtime.py`
- Test: `tests/test_tool_boundaries.py`

**Interfaces:**
- `to_langchain_tool(binding, on_actual_start=...)` invokes the hook after Pydantic schema validation and immediately before the ABI callable.
- `_CostCallback.record_tool_start(binding, arguments)` records only real handler starts.

- [x] Add failing sync/async invalid-schema and timeout behavior tests.
- [x] Verify callback-based code reports a false start.
- [x] Wrap validated handlers and remove `on_tool_start` accounting.
- [x] Verify invalid schema records zero and real handler timeout records one.

### Task 2: Multi-tool HITL and request validation

**Files:**
- Modify: `src/abi/providers/agent_runtime/runner.py`
- Modify: `src/abi/providers/agent_runtime/__init__.py`
- Test: `tests/test_agent_runtime.py`

**Interfaces:**
- `HitlDecision(decision: Literal["approve", "reject"], feedback: str | None)`.
- `HitlResume(decisions: tuple[HitlDecision, ...])` with at least one decision.

- [x] Add real SQLite tests for approve/approve, reject/reject, mixed decisions, and count mismatch.
- [x] Verify the single-decision implementation fails.
- [x] Map ordered ABI decisions to provider `Command`; convert count mismatch to `RepairRequired(defect_codes=("hitl_decision_count_mismatch",))`.
- [x] Verify all HITL cases and strict boundaries.

### Task 3: Status-aware provider classification and duplicate tools

**Files:**
- Modify: `src/abi/providers/llm/factory.py`
- Modify: `src/abi/providers/agent_runtime/runner.py`
- Test: `tests/test_agent_runtime.py`

**Interfaces:**
- Provider classification examines OpenAI `status_code`: 429 is transient, 500–599 is transient, explicit 400/401/403/404 are permanent.
- `AgentActionRequest.__post_init__` rejects duplicate `ToolBinding.name` before filesystem/checkpoint work.

- [x] Add failing status-matrix and duplicate-name/no-checkpoint tests.
- [x] Verify failures expose broad tuple classification and duplicate masking.
- [x] Add status-aware classification and duplicate-name validation.
- [x] Verify safe outcomes and zero handler/checkpoint mutation.

### Task 4: Per-run LLM callback idempotency

**Files:**
- Modify: `src/abi/providers/agent_runtime/runner.py`
- Test: `tests/test_agent_runtime.py`

**Interfaces:**
- `_CostCallback` tracks started and finalized LangChain `run_id` values.
- Start/end/error and outer exception accounting finalize each run once.

- [x] Add a failing direct callback test for one start followed by two errors.
- [x] Verify duplicate metrics/events under the current aggregate counter.
- [x] Replace aggregate pending attempts with per-run sets and idempotent finalization.
- [x] Verify exactly one call, metric, and error event.

### Task 5: Checkpoint resume control outcomes

**Files:**
- Modify: `src/abi/providers/agent_runtime/runner.py`
- Test: `tests/test_agent_runtime.py`

**Interfaces:**
- Non-resumable checkpoint continuation returns `RepairRequired(defect_codes=("checkpoint_not_resumable",))` with a fresh-invocation repair instruction.
- A completed checkpoint resume returns the stored successful result without another model or tool call.

- [x] Add failing real SQLite tests for a missing checkpoint and completed checkpoint resume.
- [x] Verify missing resume is unclassified; verify completed resume is already cached by LangGraph.
- [x] Map `EmptyInputError` to a control-plane repair result while preserving LangGraph's cached completed result.
- [x] Verify no new human/model/tool work occurs.

### Task 6: Regression verification and handoff

**Files:**
- Modify: `.superpowers/sdd/2026-08-04-constrained-dynamic-orchestration/task-6-report.md`

- [x] Run focused runtime/tool/offline tests.
- [x] Run architecture linter, Ruff, strict mypy Python 3.12, full pytest, and diff check.
- [x] Append Round 2 RED/GREEN evidence to the ignored shared report.
- [ ] Commit the tracked implementation, tests, and plan in one new commit.
