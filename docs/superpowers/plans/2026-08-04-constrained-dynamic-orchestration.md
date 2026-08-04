# Constrained Dynamic Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ABI's fixed `HAPPY_PATH`/28-state macro workflow with a policy-gated Planner, durable Action loop, transactional run ledger, typed failure semantics, and resumable Action harness.

**Architecture:** A structured-output Planner proposes short `PlanPatch` objects from compressed `RunSnapshot` evidence. A deterministic `PolicyEngine` authorizes only registered, eligible Actions; a durable controller dispatches them, validates staging artifacts, and records business facts in SQLite before replanning. LangGraph checkpoints runtime cursors and Action conversations, while `state/run.db` remains the only business source of truth.

**Tech Stack:** Python 3.11+, Pydantic 2, SQLite/aiosqlite, LangChain 1.x `create_agent`, LangGraph 1.x, `langgraph-checkpoint-sqlite` 3.x, Typer, pytest/pytest-asyncio, Ruff, mypy.

## Global Constraints

- Work only in `/Users/yezhitong/my-projects/ai-book-interpreter/.worktrees/agent-loop-research` on branch `codex/research-agent-loop`.
- Do not preserve `pipeline_state.json`, old run data, `Status`, `HAPPY_PATH`, `STAGE_SEQUENCE`, `StageSpec`, or `--until` compatibility.
- The dependency order becomes `types → config → ir → project → epub → qa → release → tools → actions → planning → orchestrator → cli`; providers may depend only on `types` and `config`, while business layers may call provider interfaces.
- `langchain*`, `langgraph*`, `langfuse*`, and provider SDK imports remain under `src/abi/providers/**`; business tools use ABI-owned `ToolBinding` values.
- Every external value is parsed into a frozen Pydantic model at the boundary; no `dict` or `Any` crosses providers into business code.
- Planner may propose only; PolicyEngine, validators, Committer, and Reconciler exclusively control authorization, gates, run status, and business commits.
- Unknown capability, validator, predicate, outcome, or exception classification fails closed with an error message that includes a repair instruction.
- `state/run.db` is the business source of truth; `events.jsonl`, `metrics.json`, and `state/status.json` are rebuildable projections.
- Action execution is at-least-once; business commits are exactly-once by stable `run_id`, `plan_version`, `action_id`, `attempt`, and `idempotency_key`.
- Filesystem promotion and SQLite commits use `promotion_intent` plus reconciliation; never claim cross-medium atomicity.
- Unclassified failures are `PermanentFailure`, except possible external side effects, which are `Indeterminate` and must be probed before retry.
- Translation Actions receive only source text, five to eight style rules, and matched terminology; QA, EPUB, and release rules are excluded.
- Use TDD for every task, run the named focused test first, and make the listed commit only after the focused and regression checks pass.

---

## File and Boundary Map

| Area | Files | Responsibility |
| --- | --- | --- |
| Pure contracts | `src/abi/types/orchestration.py`, `src/abi/types/tools.py` | Frozen run, plan, Action, evidence, outcome, authorization, and tool-binding shapes |
| Configuration | `src/abi/types/run.py`, `src/abi/config/loader.py` | Planner horizon, loop, retry, timeout, and concurrency limits |
| Business persistence | `src/abi/project/ledger_schema.py`, `src/abi/project/run_ledger.py` | SQLite schema, legal transitions, plans, attempts, evidence, incidents, outbox |
| Artifact transactions | `src/abi/project/artifacts.py` | Staging paths, checksums, promotion intents, promotion reconciliation |
| Action control | `src/abi/actions/contracts.py`, `registry.py`, `predicates.py`, `validators.py`, `builtins/catalog.py` | Capability definitions, typed inputs, eligibility, execution, deterministic evidence |
| Planning | `src/abi/planning/context.py`, `planner.py`, `policy.py`, `scheduler.py` | Snapshot compression, structured proposal, deterministic authorization and conflict-free batches |
| Provider runtimes | `src/abi/providers/agent_runtime/runner.py`, `tooling.py`, `src/abi/providers/orchestration_runtime/runtime.py` | LangChain Action harness, ABI tool adaptation, LangGraph durable cycle cursor |
| Control plane | `src/abi/orchestrator/controller.py`, `dispatcher.py`, `committer.py`, `reconcile.py`, `run.py` | Observe-plan-authorize-dispatch-validate-commit-replan loop |
| User surface | `src/abi/cli/main.py`, `src/abi/project/scaffold.py`, `layout.py` | Create/resume/inspect/unblock/cancel over the new ledger |
| Removal/docs | legacy state/stage files, architecture/reliability/product/eval docs | Delete fixed path and document the implemented behavior |

## Task 1: Frozen Orchestration and Tool Contracts

**Files:**
- Create: `src/abi/types/orchestration.py`
- Create: `src/abi/types/tools.py`
- Modify: `src/abi/types/__init__.py`
- Modify: `src/abi/types/run.py`
- Test: `tests/test_orchestration_types.py`

**Interfaces:**
- Produces: `RunStatus`, `ActionStatus`, `ActionKind`, `RetryPolicySpec`, `ActionSpec`, `ActionArgument`, `ProposedAction`, `PlanPatch`, `RunSnapshot`, `AuthorizedAction`, `AuthorizationDecision`, `GateDecision`, `ActionOutcomeEnvelope`, `ToolCallRecord`, `AgentRunResult`, `RunResult`, and `ToolBinding`.
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
        "outcome": {"kind": "permanent_failure", "error_code": "unsupported_format",
                    "message": "convert the source to txt or epub"}
    })
    assert isinstance(envelope.outcome, PermanentFailure)
    assert RunStatus.BLOCKED.value == "BLOCKED"
```

- [ ] **Step 2: Run the tests and confirm the missing-module failure**

Run: `.venv/bin/pytest tests/test_orchestration_types.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'abi.types.orchestration'`.

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


class Succeeded(FrozenModel):
    kind: Literal["succeeded"] = "succeeded"
    staging_relpath: str
    evidence_refs: tuple[str, ...] = ()


class RetryableFailure(FrozenModel):
    kind: Literal["retryable_failure"] = "retryable_failure"
    error_code: str
    message: str
    retry_after_s: float | None = None


class RepairRequired(FrozenModel):
    kind: Literal["repair_required"] = "repair_required"
    defect_codes: tuple[str, ...]
    message: str


class PermanentFailure(FrozenModel):
    kind: Literal["permanent_failure"] = "permanent_failure"
    error_code: str
    message: str


class Indeterminate(FrozenModel):
    kind: Literal["indeterminate"] = "indeterminate"
    operation_key: str
    message: str


class Paused(FrozenModel):
    kind: Literal["paused"] = "paused"
    reason: Literal["budget", "hitl"]
    message: str


ActionOutcome = Annotated[
    Succeeded | RetryableFailure | RepairRequired | PermanentFailure | Indeterminate | Paused,
    Field(discriminator="kind"),
]


class ActionOutcomeEnvelope(FrozenModel):
    outcome: ActionOutcome


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

Add `PlannerConfig(horizon=5, max_rejections=3)`, `OrchestrationConfig(max_cycles=500, max_parallel_actions=4, default_action_attempts=3)`, and fields on `RunConfig` with frozen defaults. Define `ToolBinding` as a frozen dataclass containing `name`, `description`, `args_schema: type[FrozenModel]`, and a sync-or-async callable; it is an internal runtime binding, not a persisted model.

- [ ] **Step 4: Run focused type and configuration tests**

Run: `.venv/bin/pytest tests/test_orchestration_types.py tests/test_project_model.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit the contracts**

```bash
git add src/abi/types tests/test_orchestration_types.py
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
- Produces: `ActionDefinition`, `ResolvedAction`, `ActionExecutionContext`, `ActionRegistry.register()`, `ActionRegistry.resolve()`, `PredicateCatalog`, and `PolicyEngine.authorize(snapshot, patch, next_plan_version)`.

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
```

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

`resolve()` rejects duplicate argument names, parses each `value_json`, builds one JSON object only inside the parsing boundary, validates it through `input_model.model_validate`, and immediately stores `model_dump_json()` as canonical JSON. Error text names the capability, field, and how to correct the plan.

- [ ] **Step 5: Implement deterministic policy checks**

`PolicyEngine.authorize()` runs in this order: horizon 1–5, unique proposal IDs, known/eligible capability, argument parsing, dependencies exist, acyclic graph, hard prerequisites, budget estimate, read/write conflicts, repeated-failure signature, terminal release policy. It returns all rejection codes in stable sorted order and never partially authorizes a rejected patch.

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
- Produces: `RunLedger.open(path)`, `create_run()`, `append_plan()`, `authorize_actions()`, `start_attempt()`, `finish_attempt()`, `commit_success()`, `record_incident()`, `set_run_status()`, `load_snapshot()`, and `rebuild_status_projection()`.

- [ ] **Step 1: Write transaction, transition, and exactly-once tests**

```python
@pytest.mark.asyncio
async def test_commit_success_is_exactly_once(tmp_path: Path) -> None:
    async with RunLedger.open(tmp_path / "run.db") as ledger:
        await seed_authorized_action(ledger, action_id="a1")
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
```

- [ ] **Step 2: Run the ledger tests and confirm failure**

Run: `.venv/bin/pytest tests/test_run_ledger.py -v`

Expected: collection fails because `abi.project.run_ledger` does not exist.

- [ ] **Step 3: Add the async SQLite dependency and schema**

Add `"aiosqlite>=0.20,<1"` to runtime dependencies and run `uv lock`. `SCHEMA_SQL` must create WAL-backed tables `runs`, `plan_versions`, `actions`, `action_attempts`, `artifacts`, `promotion_intents`, `gate_evidence`, `incidents`, `interrupts`, `budget_entries`, and `event_outbox`. Add unique constraints on `(run_id, version)`, `(run_id, action_id)`, `(action_id, attempt)`, artifact canonical path, and event idempotency key.

```sql
CREATE TABLE IF NOT EXISTS actions (
  action_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  plan_version INTEGER NOT NULL,
  capability TEXT NOT NULL,
  parameters_json TEXT NOT NULL,
  status TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  committed_at TEXT
);
```

- [ ] **Step 4: Implement `RunLedger` with explicit transactions**

Use `aiosqlite.Connection`, `BEGIN IMMEDIATE`, injected UTC clock, and repository-owned row-to-model parsing. `commit_success()` writes Action status, artifacts, gate evidence, budget entry, and outbox event in one SQLite transaction. An identical repeated commit returns the prior record; a different checksum for the same Action creates a conflict incident and raises `LedgerConflictError`.

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
- Extend: `src/abi/project/run_ledger.py`
- Modify: `src/abi/project/layout.py`
- Test: `tests/test_artifact_promotion.py`

**Interfaces:**
- Consumes: `RunLedger` and `ArtifactRef`.
- Produces: `ArtifactStore.staging_dir()`, `prepare_promotion()`, `promote()`, `reconcile_intent()`, `sha256_file()`, and ledger promotion-intent methods.

- [ ] **Step 1: Write crash-boundary and conflict tests**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("crash_point", ["after_intent", "after_rename"])
async def test_reconcile_completes_interrupted_promotion(tmp_path: Path, crash_point: str) -> None:
    store, ledger, staged = await prepared_store(tmp_path, content="translation")
    with pytest.raises(InjectedCrash):
        await store.promote(staged, crash_after=crash_point)
    await store.reconcile_all()
    assert (tmp_path / "chapters/final/001.md").read_text() == "translation"
    assert await ledger.promotion_state(staged.intent_id) == "COMMITTED"


@pytest.mark.asyncio
async def test_different_canonical_checksum_creates_conflict(tmp_path: Path) -> None:
    store, ledger, staged = await prepared_store(tmp_path, content="new")
    canonical = tmp_path / "chapters/final/001.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("old")
    with pytest.raises(ArtifactConflictError, match="choose the canonical artifact"):
        await store.promote(staged)
    assert await ledger.has_open_incident("artifact_checksum_conflict")
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.venv/bin/pytest tests/test_artifact_promotion.py -v`

Expected: collection fails because `abi.project.artifacts` does not exist.

- [ ] **Step 3: Implement staged paths and checksum records**

`BookProject` gains `run_db`, `graph_checkpoints`, `staging_root`, and `status_projection` properties. `ArtifactStore.staging_dir(action_id, attempt)` returns `state/staging/{action_id}/{attempt}` after validating both path components.

```python
async def prepare_promotion(
    self, *, action_id: str, attempt: int, staged_relpath: str,
    canonical_relpath: str, media_type: str
) -> PromotionIntent:
    checksum = sha256_file(self._project.root / staged_relpath)
    return await self._ledger.create_promotion_intent(
        action_id=action_id,
        attempt=attempt,
        staged_relpath=staged_relpath,
        canonical_relpath=canonical_relpath,
        checksum=checksum,
        media_type=media_type,
    )
```

- [ ] **Step 4: Implement promotion and reconciliation**

Promotion writes the ledger intent first, verifies canonical checksum, uses same-filesystem `Path.replace`, then confirms the intent in a second transaction. `reconcile_intent()` handles all four combinations of staged/canonical presence and never deletes a differing artifact. Clean only staging directories whose intents are committed and whose checksum matches canonical.

- [ ] **Step 5: Run focused and project tests**

Run: `.venv/bin/pytest tests/test_artifact_promotion.py tests/test_project_model.py -v`

Expected: all tests pass.

- [ ] **Step 6: Commit artifact transactions**

```bash
git add src/abi/project tests/test_artifact_promotion.py
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
- Produces: `SnapshotBuilder.build(run_id) -> RunSnapshot` and `Planner.plan(snapshot) -> PlanPatch`.

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
async def test_planner_uses_plan_patch_schema_and_no_tools() -> None:
    router = RecordingRouter(result=valid_patch())
    patch = await Planner(router=router).plan(snapshot_with_ingest_eligible())
    assert patch.proposed_actions[0].capability == "source.ingest"
    assert router.schema is PlanPatch
    assert router.agent_name == "orchestration.planner"
```

- [ ] **Step 2: Run focused tests and confirm failure**

Run: `.venv/bin/pytest tests/test_planner.py -v`

Expected: collection fails because the planner modules do not exist.

- [ ] **Step 3: Implement compressed snapshot construction**

Read only committed ledger facts and artifact metadata. Include unresolved incidents, remaining budget, plan rejections, and eligible capability descriptions. Limit incident messages to 500 characters, artifact samples to configured counts, and Planner horizon to five. Loading additional content is represented by an eligible `inspect.*` Action rather than direct file access.

- [ ] **Step 4: Implement the Planner prompt and structured call**

```python
PLANNER_SYSTEM_PROMPT = """You are ABI's constrained planner.
Return one PlanPatch with one to five actions chosen only from eligible_actions.
You cannot mark gates passed, mutate run state, invent capabilities, or skip dependencies.
Prefer the smallest action that produces missing evidence or repairs an open incident.
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
- Test: `tests/test_agent_runtime.py`
- Test: `tests/test_tool_boundaries.py`

**Interfaces:**
- Consumes: `ToolBinding`, `ActionOutcomeEnvelope`, project checkpoint path, shared budget/events/metrics.
- Produces: `AgentActionRequest` and `AgentRuntime.run_action(request) -> AgentRunResult` with no LangChain types in its public return value.

- [ ] **Step 1: Write migration and persistence tests**

```python
def test_business_tools_do_not_import_langchain() -> None:
    offenders = sdk_imports_under(Path("src/abi/tools"), prefixes=("langchain", "langgraph"))
    assert offenders == []


@pytest.mark.asyncio
async def test_action_harness_resumes_same_thread(tmp_path: Path) -> None:
    runtime = fake_model_runtime(checkpoint_path=tmp_path / "graph-checkpoints.sqlite")
    first = await runtime.run_action(action_request(thread_id="run-1/a1/1"))
    second = await runtime.run_action(action_request(thread_id="run-1/a1/1", resume=True))
    assert first.outcome.kind == "paused"
    assert second.outcome.kind == "succeeded"
    assert runtime.fake_model_seen_prior_tool_result is True
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

Each `make_*_tools()` returns `list[ToolBinding]`. Put Pydantic input models beside their business handler. `providers.agent_runtime.tooling.to_langchain_tool()` is the only adapter:

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

Use `langchain.agents.create_agent`, `response_format=ActionOutcomeEnvelope`, and `AsyncSqliteSaver.from_conn_string`. Map graph recursion to `RetryableFailure(error_code="iteration_limit")`, budget to `Paused(reason="budget")`, declared transient provider errors to `RetryableFailure`, and possible side-effect timeouts to `Indeterminate`. Unknown exceptions become `PermanentFailure(error_code="unclassified_exception")`.

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

Expected: both commands pass; repository search finds no `create_react_agent`.

- [ ] **Step 7: Commit the runtime migration**

```bash
git add pyproject.toml uv.lock src/abi/providers src/abi/tools tests/test_agent_runtime.py tests/test_tool_boundaries.py tests/test_orchestrator_offline.py
git commit -m "feat: add durable LangChain v1 action harness"
```

## Task 7: Built-in Capability Catalog and Fail-Closed Validators

**Files:**
- Create: `src/abi/actions/builtins/__init__.py`
- Create: `src/abi/actions/builtins/inputs.py`
- Create: `src/abi/actions/builtins/catalog.py`
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
- Produces: `build_action_registry()`, `ActionPromptRegistry`, typed inputs for every built-in capability, and `validate_evidence(capability, project, parameters)`.

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
```

- [ ] **Step 2: Write fail-closed validator tests**

```python
def test_unknown_capability_never_passes(tmp_path: Path) -> None:
    project = scaffold_without_ledger(tmp_path)
    result = validate_evidence("invented.capability", project, EmptyInput())
    assert result.passed is False
    assert result.reason_code == "validator_not_registered"


def test_chapter_validator_checks_only_requested_chapters(tmp_path: Path) -> None:
    project = project_with_chapters(tmp_path, source=("001", "002"), translated=("001",))
    one = validate_evidence("chapter.translate", project, ChapterBatchInput(chapters=("001",)))
    two = validate_evidence("chapter.translate", project, ChapterBatchInput(chapters=("002",)))
    assert one.passed is True
    assert two.passed is False
```

- [ ] **Step 3: Run focused tests and confirm failure**

Run: `.venv/bin/pytest tests/test_builtin_actions.py tests/test_action_validators.py tests/test_action_permissions.py -v`

Expected: collection fails because the built-in catalog and validator router do not exist.

- [ ] **Step 4: Define typed inputs and registered capability effects**

Use separate models: `SourceIngestInput`, `SourceSplitInput`, `ResearchInput`, `ChapterBatchInput`, `ReviewBatchInput`, `BuildEpubInput`, `ReleaseInput`, and `EmptyInput`. Every ActionSpec declares `prerequisites`, `effects`, `expected_evidence`, `tool_allowlist`, `skill_refs`, `read_set`, `write_set`, retry policy, validator, estimated cost, and Action kind. Keep dependency rules as predicates, not list ordering.

- [ ] **Step 5: Port prompts and Action execution without `StageSpec`**

`ActionPromptRegistry.render(capability, parameters, snapshot)` selects by capability. Agent Actions execute through one `AgentActionExecutor`; deterministic Actions call existing parsers/builders/linters directly. Chapter Actions require an explicit chapter tuple so Scheduler can prove disjoint writes. Translation prompt construction enforces the source + five-to-eight style rules + matched-terms envelope. Resolve the registry's `tool_allowlist` through `ToolBelt`, then enforce the same read/write set again inside filesystem handlers. Composite review Actions allocate independent thread IDs and reuse the shared BudgetGate.

- [ ] **Step 6: Move validators and make the router exhaustive**

Port checks from `src/abi/stages/validators.py` into capability validators. Replace the old final `return _ok()` with an explicit failure:

```python
def validate_evidence(capability: str, project: BookProject, parameters: FrozenModel) -> GateDecision:
    validator = _VALIDATORS.get(capability)
    if validator is None:
        return GateDecision(
            passed=False,
            reason_code="validator_not_registered",
            message=f"No validator for {capability}; register one before authorizing this Action.",
        )
    return validator(project, parameters)
```

- [ ] **Step 7: Run catalog, validator, ingest, EPUB, and QA tests**

Run: `.venv/bin/pytest tests/test_builtin_actions.py tests/test_action_validators.py tests/test_action_permissions.py tests/test_ingest_txt.py tests/test_epub_build.py tests/test_eval.py -v`

Expected: all tests pass.

- [ ] **Step 8: Commit the capability catalog**

```bash
git add src/abi/actions src/abi/prompts src/abi/tools tests/test_builtin_actions.py tests/test_action_validators.py tests/test_action_permissions.py
git commit -m "feat: register ABI work as typed capabilities"
```

## Task 8: Scheduler, Dispatcher, Committer, and Dynamic Controller

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
- Consumes: Planner, PolicyEngine, RunLedger, ArtifactStore, ActionRegistry, AgentRuntime.
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
    rig = controller_rig(outcomes=(RepairRequired(
        defect_codes=("term_drift",), message="repair glossary"
    ), Succeeded(staging_relpath="state/staging/a2/1", evidence_refs=("gate",))))
    await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
    assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.COMPLETED
    assert await rig.ledger.event_names() == expected_replan_event_sequence()


@pytest.mark.asyncio
async def test_permanent_failure_blocks_without_retry() -> None:
    rig = controller_rig(outcomes=(PermanentFailure(
        error_code="copyright_denied", message="supply a license or use private mode"
    ),))
    await rig.runtime.run(run_id=rig.run_id, tick=rig.controller.tick)
    assert rig.dispatcher.attempts == 1
    assert (await rig.ledger.get_run(rig.run_id)).status is RunStatus.BLOCKED
```

- [ ] **Step 3: Run Scheduler and controller tests and confirm failure**

Run: `.venv/bin/pytest tests/test_scheduler.py tests/test_dynamic_controller.py -v`

Expected: collection fails because the Scheduler and dynamic controller do not exist.

- [ ] **Step 4: Implement batch selection and typed dispatch**

Scheduler sorts by priority then action ID, incrementally admits Actions whose dependencies are committed and whose read/write sets do not conflict with the batch. Dispatcher starts attempts in the ledger, resolves canonical parameters through Registry, runs deterministic/agent/composite executors, applies per-Action timeout, and always returns `ActionOutcomeEnvelope`.

- [ ] **Step 5: Implement commit and reconciliation routing**

Committer validates staging evidence before promotion and ledger success commit. Outcome routing is exact: retryable → bounded retry; repair → incident + replan; permanent → alternative capability or BLOCKED; indeterminate → registered probe Action only; paused → run pause; success → validate/promote/commit. Reconciler runs before every planning cycle and skips Action execution when ledger already committed it.

`OutboxProjector.flush()` reads undelivered ledger events in sequence order, appends them to `events.jsonl` through `EventLogger.append_record(event_id, record)`, updates metrics/status projections, then marks each outbox row delivered. `EventLogger` builds a seen-event-ID set from the existing JSONL file at startup and refuses a second append of the same ID, closing the crash window between file append and the delivered flag. Provider events also receive stable call/attempt IDs, so one file remains a deduplicated projection.

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

**Files:**
- Extend: `src/abi/orchestrator/reconcile.py`
- Extend: `src/abi/orchestrator/dispatcher.py`
- Extend: `src/abi/project/run_ledger.py`
- Create: `tests/test_orchestration_recovery.py`
- Create: `tests/test_failure_semantics.py`

**Interfaces:**
- Consumes: Task 8 control plane.
- Produces: deterministic crash-point hooks for tests, retry signature accounting, `ProbeResult`, and resume behavior for paused/blocked runs.

- [ ] **Step 1: Write the recovery matrix as executable tests**

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [
    "before_dispatch", "after_action_output", "after_promotion_intent",
    "after_artifact_rename", "after_ledger_commit", "before_graph_checkpoint",
])
async def test_crash_boundaries_do_not_duplicate_business_facts(
    tmp_path: Path, boundary: str
) -> None:
    rig = recovery_rig(tmp_path, crash_at=boundary)
    with pytest.raises(InjectedCrash):
        await rig.run()
    rig.disable_crash()
    await rig.resume()
    assert await rig.ledger.count_committed_actions("a1") == 1
    assert await rig.ledger.count_artifacts_for("a1") == 1
```

- [ ] **Step 2: Write failure classification and probe tests**

```python
@pytest.mark.asyncio
async def test_indeterminate_release_is_probed_not_reissued() -> None:
    rig = release_rig(first=Indeterminate(
        operation_key="release:abc", message="timeout after request"
    ), probe=ProbeResult.COMMITTED)
    await rig.run()
    assert rig.release_calls == 1
    assert rig.probe_calls == 1
    assert await rig.ledger.action_status("release") is ActionStatus.SUCCEEDED


def test_unknown_exception_classifies_fail_closed() -> None:
    outcome = classify_exception(RuntimeError("boom"), may_have_side_effects=False)
    assert outcome.kind == "permanent_failure"
```

- [ ] **Step 3: Run recovery tests and confirm red results**

Run: `.venv/bin/pytest tests/test_orchestration_recovery.py tests/test_failure_semantics.py -v`

Expected: tests fail at the unimplemented crash hooks, signature accounting, and probe routes.

- [ ] **Step 4: Implement retry signatures and external-operation probes**

Hash `(capability, canonical_parameters_json, error_code)` as the failure signature. Retry only errors named by `RetryPolicySpec.retryable_codes`, enforce max attempts and jittered backoff, and convert exhaustion to `RepairRequired` or `PermanentFailure` according to the ActionSpec. External Actions declare `may_have_side_effects=True` plus a probe executor; Dispatcher refuses to retry them while the prior attempt is indeterminate.

- [ ] **Step 5: Implement all recovery boundaries**

At startup reconcile ledger/checkpoint/artifacts in this order: open promotion intents, running attempts with no commit, committed Actions missing graph progress, indeterminate operations, outbox delivery. Emit `action.reconciled` for every correction and preserve the original incident.

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

- [ ] **Step 2: Run lifecycle tests and confirm they fail against old behavior**

Run: `.venv/bin/pytest tests/test_project_model.py tests/test_dynamic_cli.py tests/test_orchestrator_offline.py -v`

Expected: failures reference `pipeline_state.json`, `Status`, and absent CLI commands.

- [ ] **Step 3: Initialize the ledger during scaffold and build services with stable run ID**

Scaffold creates the directory contract, `state/run.db`, `state/staging`, and checkpoint parent. `make_book()` creates one ledger run, writes source, and starts the durable runtime. `resume()` loads the existing run ID and never creates a fresh business run. Observability uses that stable run ID across resumes.

- [ ] **Step 4: Replace status-based CLI commands**

Remove `--until` and the happy-path table. `inspect` prints run status, current plan version, authorized/running/recent Actions, gates, incidents, budget, and next recovery instruction. `approve` resumes one `PAUSED_HITL` interrupt by ID and records the human decision; `unblock` requires a reason, closes only externally resolved incidents, and transitions BLOCKED or `PAUSED_BUDGET` to RUNNING. `cancel` is idempotent and cannot reopen COMPLETED.

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
- Test: `tests/test_no_fixed_pipeline.py`
- Modify: `tests/test_eval.py`

**Interfaces:**
- Consumes: completed dynamic implementation.
- Produces: no fixed macro path in source, L1 eval over ledger events, and documentation whose architecture/flow diagrams match implemented modules.

- [ ] **Step 1: Write a structural test that forbids legacy control symbols**

```python
def test_fixed_macro_pipeline_is_absent() -> None:
    source = "\n".join(path.read_text() for path in Path("src/abi").rglob("*.py"))
    for forbidden in ("HAPPY_PATH", "STAGE_SEQUENCE", "StageSpec", "set_state", "record_gate"):
        assert forbidden not in source
    assert not Path("src/abi/project/state.py").exists()
    assert not Path("src/abi/stages").exists()
```

- [ ] **Step 2: Run the structural test and confirm old symbols are found**

Run: `.venv/bin/pytest tests/test_no_fixed_pipeline.py -v`

Expected: failure lists the remaining legacy symbols and files.

- [ ] **Step 3: Delete legacy modules and update imports**

Remove the fixed state/stage code and numbered sequence registry. `ToolContext` exposes project, services, run ID, and read-only snapshot callback; it has no `state()` or `save_state()`. Remove every import of `abi.project.state` and `abi.stages`.

- [ ] **Step 4: Rewrite L1 process eval against the ledger**

Gate integrity replays `gate_evidence` against artifact checksums and validator version. Path conformance becomes policy conformance: every committed Action was authorized, every release prerequisite was committed, no failed/paused Action was treated as success, and every plan rejection has reasons. Keep L2 translation and L3 EPUB scoring behavior unchanged.

- [ ] **Step 5: Synchronize all authoritative documentation**

Update layering, project layout, CLI behavior, checkpoint/recovery matrix, failure semantics, and event schema. Copy the approved Mermaid architecture and flow diagrams from `dynamic-agent-orchestration.md` into the current architecture overview where useful; do not create a second contradictory diagram. Change the design document status to `Implemented` only after the end-to-end tests pass.

- [ ] **Step 6: Run structural, eval, and documentation sanity checks**

Run: `.venv/bin/pytest tests/test_no_fixed_pipeline.py tests/test_eval.py -v`

Run: `git diff --check`

Run: `rg -n 'pipeline_state\.json|28.state|HAPPY_PATH|STAGE_SEQUENCE' ARCHITECTURE.md docs src/abi tests`

Expected: tests pass; diff check is clean; the search finds only historical explanation in the approved design document's “old design” discussion and no active instructions or source code.

- [ ] **Step 7: Commit legacy removal and documentation**

```bash
git add -A src/abi tests ARCHITECTURE.md docs AGENTS.md
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

Expected: all recovery boundaries, permanent failures, indeterminate probes, replanning, concurrency, and completion scenarios pass.

- [ ] **Step 4: Verify forbidden symbols and SDK boundaries**

Run: `rg -n 'create_react_agent|HAPPY_PATH|STAGE_SEQUENCE|StageSpec|def set_state|def record_gate' src tests`

Run: `rg -n '^(from|import) (langchain|langgraph|langfuse)' src/abi --glob '!providers/**'`

Expected: both searches return no matches.

- [ ] **Step 5: Record implementation evidence in the design document**

Add the implementation commit range, test counts, dependency versions from `uv.lock`, checkpoint database path, ledger schema version, and the exact recovery scenarios exercised. Set status to `Implemented` only now.

- [ ] **Step 6: Commit the verification record**

```bash
git add docs/design-docs/dynamic-agent-orchestration.md
git commit -m "docs: record dynamic orchestration verification"
```

- [ ] **Step 7: Request code review before integration**

Invoke `superpowers:requesting-code-review`, review the full diff from `main...codex/research-agent-loop`, resolve findings through `superpowers:receiving-code-review`, then rerun Steps 1–4 before using `superpowers:finishing-a-development-branch`.
