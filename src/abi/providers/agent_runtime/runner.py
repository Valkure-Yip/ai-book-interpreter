"""LangChain v1 Action harness with durable, thread-scoped checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
from _thread import LockType
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast
from uuid import UUID, uuid4

from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelResponse,
    wrap_model_call,
)
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import EmptyInputError, GraphRecursionError
from langgraph.types import Command, Interrupt, StateSnapshot
from pydantic import Field, ValidationError, model_validator

from abi.providers.agent_runtime.tooling import to_langchain_tool
from abi.providers.llm.budget import BudgetExceeded, BudgetGate
from abi.providers.llm.factory import _PERMANENT_LLM_ERRORS, _TRANSIENT_LLM_ERRORS
from abi.providers.llm.pricing import estimate_cost_usd
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.providers.observability.langfuse_client import (
    LangfuseStatus,
    callback_handler,
    observation_context,
)
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionOutcome,
    AgentCompleted,
    AgentRunResult,
    Indeterminate,
    Paused,
    PendingHitlActionReview,
    PendingHitlInterrupt,
    PermanentFailure,
    RepairRequired,
    RetryableFailure,
    ToolCallRecord,
)
from abi.types.run import LLMConfig
from abi.types.tools import ToolBinding

_READ_ONLY_AGENT_TOOLS = frozenset({"grep", "read_file"})


def _limit_tool_call_batch(response: ModelResponse[Any]) -> ModelResponse[Any]:
    """Allow parallel reads, but serialize every state-changing tool request."""
    result: list[BaseMessage] = []
    for message in response.result:
        if isinstance(message, AIMessage) and len(message.tool_calls) > 1:
            non_read_calls = [
                call
                for call in message.tool_calls
                if call.get("name") not in _READ_ONLY_AGENT_TOOLS
            ]
            if non_read_calls:
                message = message.model_copy(update={"tool_calls": non_read_calls[:1]})
        result.append(message)
    return ModelResponse(result=result, structured_response=response.structured_response)


def _response_is_content_filtered(response: ModelResponse[Any]) -> bool:
    return any(
        isinstance(message, AIMessage)
        and message.response_metadata.get("finish_reason") == "content_filter"
        for message in response.result
    )


async def _invoke_with_content_filter_retries(
    request: Any, handler: Any
) -> ModelResponse[Any]:
    response = await handler(request)
    for _ in range(2):
        if not _response_is_content_filtered(response):
            break
        response = await handler(request)
    return response


@wrap_model_call(name="abi_serial_mutation_tools")
async def _serial_mutation_tool_middleware(
    request: Any, handler: Any
) -> ModelResponse[Any]:
    response = await _invoke_with_content_filter_retries(request, handler)
    return _limit_tool_call_batch(response)


class CheckpointResume(FrozenModel):
    """Resume the durable graph from its latest checkpoint without new input."""

    kind: Literal["checkpoint"] = "checkpoint"


class HitlDecision(FrozenModel):
    """One ordered human decision for a pending business tool call."""

    decision: Literal["approve", "reject"]
    feedback: str | None = None


class HitlInterruptDecision(FrozenModel):
    """Ordered decisions addressed to one concrete LangGraph interrupt."""

    interrupt_id: str = Field(min_length=1)
    decisions: tuple[HitlDecision, ...] = Field(min_length=1)


class HitlResume(FrozenModel):
    """Typed decisions for the exact set of pending graph interrupts."""

    kind: Literal["hitl"] = "hitl"
    interrupts: tuple[HitlInterruptDecision, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _interrupt_ids_are_unique(self) -> HitlResume:
        interrupt_ids = [item.interrupt_id for item in self.interrupts]
        if len(interrupt_ids) != len(set(interrupt_ids)):
            raise ValueError("HITL interrupt ids must be unique")
        return self


class HitlCheckpointInspection(FrozenModel):
    """Read-only classification of one claimed HITL checkpoint."""

    disposition: Literal["outcome", "not_started", "indeterminate"]
    outcome: ActionOutcome | AgentCompleted | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _typed_outcome(self) -> HitlCheckpointInspection:
        if (self.disposition == "outcome") != (self.outcome is not None):
            raise ValueError("only an outcome checkpoint inspection may carry an outcome")
        return self


AgentResume: TypeAlias = CheckpointResume | HitlResume


class _CheckpointActionRequest(FrozenModel):
    """Validated public HITL action payload stored in an interrupt."""

    name: str
    args: dict[str, object]
    description: str | None = None


class _CheckpointReviewConfig(FrozenModel):
    """Validated decision policy paired with one interrupted action."""

    action_name: str
    allowed_decisions: tuple[Literal["approve", "edit", "reject", "respond"], ...] = Field(
        min_length=1
    )
    args_schema: dict[str, object] | None = None


class _CheckpointHitlRequest(FrozenModel):
    """Current LangChain HITL request shape carried by ``Interrupt.value``."""

    action_requests: tuple[_CheckpointActionRequest, ...] = Field(min_length=1)
    review_configs: tuple[_CheckpointReviewConfig, ...] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class _PendingHitlAction:
    """One validated tool review inside a pending interrupt."""

    tool_name: str
    arguments_json: str
    description: str | None
    allowed_decisions: tuple[Literal["approve", "reject"], ...]


@dataclass(frozen=True, slots=True)
class _PendingHitlInterrupt:
    """One unresolved checkpoint interrupt with task identity preserved."""

    task_id: str
    interrupt_id: str
    actions: tuple[_PendingHitlAction, ...]


_SUPPORTED_HITL_DECISIONS: tuple[Literal["approve", "reject"], ...] = (
    "approve",
    "reject",
)


def _parse_pending_hitl_interrupt(
    interrupt_value: Interrupt,
    *,
    task_id: str,
) -> _PendingHitlInterrupt | None:
    """Parse one provider interrupt into the stable internal HITL shape."""
    if not interrupt_value.id:
        return None
    try:
        request = _CheckpointHitlRequest.model_validate(interrupt_value.value)
    except ValidationError:
        return None
    if len(request.action_requests) != len(request.review_configs):
        return None
    actions: list[_PendingHitlAction] = []
    for action, review in zip(request.action_requests, request.review_configs, strict=True):
        if action.name != review.action_name:
            return None
        supported_decisions = tuple(
            decision
            for decision in _SUPPORTED_HITL_DECISIONS
            if decision in review.allowed_decisions
        )
        if not supported_decisions:
            return None
        actions.append(
            _PendingHitlAction(
                tool_name=action.name,
                arguments_json=json.dumps(action.args, ensure_ascii=False, sort_keys=True),
                description=action.description,
                allowed_decisions=supported_decisions,
            )
        )
    return _PendingHitlInterrupt(
        task_id=task_id,
        interrupt_id=interrupt_value.id,
        actions=tuple(actions),
    )


def _public_pending_hitl_interrupts(
    pending: tuple[_PendingHitlInterrupt, ...],
) -> tuple[PendingHitlInterrupt, ...]:
    """Remove provider task identity and expose only ABI-owned review models."""
    return tuple(
        PendingHitlInterrupt(
            interrupt_id=interrupt.interrupt_id,
            action_reviews=tuple(
                PendingHitlActionReview(
                    tool_name=action.tool_name,
                    arguments_json=action.arguments_json,
                    description=action.description,
                    allowed_decisions=action.allowed_decisions,
                )
                for action in interrupt.actions
            ),
        )
        for interrupt in pending
    )


def _pending_hitl_interrupt_values(
    values: object,
) -> tuple[_PendingHitlInterrupt, ...] | None:
    """Parse the current interrupts returned by a graph invocation."""
    if not isinstance(values, (list, tuple)):
        return None
    pending: list[_PendingHitlInterrupt] = []
    interrupt_ids: set[str] = set()
    for value in values:
        if not isinstance(value, Interrupt):
            return None
        parsed = _parse_pending_hitl_interrupt(value, task_id="")
        if parsed is None or parsed.interrupt_id in interrupt_ids:
            return None
        interrupt_ids.add(parsed.interrupt_id)
        pending.append(parsed)
    return tuple(pending) or None


@dataclass(frozen=True, slots=True)
class AgentActionRequest:
    """One isolated Action invocation; callables remain outside persisted state."""

    system_prompt: str
    user_prompt: str
    tools: tuple[ToolBinding, ...]
    agent_name: str
    thread_id: str
    checkpoint_path: Path
    max_iterations: int
    may_have_side_effects: bool = False
    resume: AgentResume | None = None
    approval_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.thread_id:
            raise ValueError(
                "thread_id must be stable and non-empty; derive it from the Action attempt"
            )
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be at least 1; configure a bounded Action loop")
        if self.resume is not None and not isinstance(self.resume, (CheckpointResume, HitlResume)):
            raise TypeError("resume must be a typed CheckpointResume or HitlResume")
        tool_names = {tool.name for tool in self.tools}
        if len(tool_names) != len(self.tools):
            duplicates = sorted(
                name for name in tool_names if sum(tool.name == name for tool in self.tools) > 1
            )
            raise ValueError(f"duplicate tool names are forbidden: {', '.join(duplicates)}")
        unknown_approval_tools = set(self.approval_tools) - tool_names
        if unknown_approval_tools:
            unknown = ", ".join(sorted(unknown_approval_tools))
            raise ValueError(f"approval_tools must name bound tools; unknown: {unknown}")


def _estimate_text_tokens(text: str) -> int:
    return max(1, len(text) // 3) if text else 0


def _provider_error_classification(error: BaseException) -> str | None:
    """Return a provider classification, giving HTTP status the first vote."""
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        if status_code == 429 or 500 <= status_code <= 599:
            return "transient_provider_error"
        if 400 <= status_code <= 499:
            return "permanent_provider_error"
    if isinstance(error, TimeoutError):
        return "provider_timeout"
    if isinstance(error, _PERMANENT_LLM_ERRORS):
        return "permanent_provider_error"
    if isinstance(error, _TRANSIENT_LLM_ERRORS):
        return "transient_provider_error"
    return None


def _hitl_mismatch_counts(
    error: BaseException,
    resume: AgentResume | None,
) -> tuple[str, str] | None:
    if not isinstance(resume, HitlResume) or not isinstance(error, ValueError):
        return None
    mismatch = re.search(
        r"Number of human decisions \((\d+)\).*tool calls \((\d+)\)",
        str(error),
    )
    if mismatch is None:
        return None
    supplied, pending = mismatch.groups()
    return supplied, pending


def _runtime_error_classification(
    error: BaseException,
    *,
    resume: AgentResume | None = None,
) -> str:
    """Classify graph/model errors once, with provider evidence first."""
    provider_classification = _provider_error_classification(error)
    if provider_classification is not None:
        return provider_classification
    if isinstance(error, BudgetExceeded):
        return "budget"
    if isinstance(error, GraphRecursionError):
        return "iteration_limit"
    if _hitl_mismatch_counts(error, resume) is not None:
        return "hitl_decision_count_mismatch"
    if isinstance(error, EmptyInputError):
        return "checkpoint_not_resumable"
    return "unclassified_exception"


def _empty_result(outcome: ActionOutcome) -> AgentRunResult:
    """Build a control-plane result before any graph, model, or tool work."""
    return AgentRunResult(
        outcome=outcome,
        llm_calls=0,
        tool_calls=0,
        cost_usd=0.0,
        stopped_reason="error",
    )


def _terminal_completion(values: object) -> AgentCompleted | None:
    """Extract a powerless terminal assistant message from graph state."""
    if not isinstance(values, dict):
        return None
    messages = values.get("messages")
    if not isinstance(messages, (list, tuple)):
        return None
    for message in reversed(messages):
        if not isinstance(message, BaseMessage) or message.type != "ai":
            continue
        if getattr(message, "tool_calls", ()):
            return None
        content = message.content
        summary = (
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False, default=str)
        )
        return AgentCompleted(summary=summary)
    return None


def _integrity_repair(defect_code: str, message: str) -> RepairRequired:
    return RepairRequired(
        repair_class="integrity",
        repair_source="integrity_guard",
        reason_code=defect_code,
        defect_codes=(defect_code,),
        message=message,
    )


def _checkpoint_not_resumable() -> AgentRunResult:
    return _empty_result(
        _integrity_repair(
            "checkpoint_not_resumable",
            (
                "This thread has no compatible checkpoint to resume. "
                "Start a fresh Action invocation instead."
            ),
        )
    )


def _checkpoint_path_unsafe() -> AgentRunResult:
    return _empty_result(
        _integrity_repair(
            "checkpoint_path_unsafe",
            (
                "The checkpoint path contains a symbolic link or unsafe component. "
                "Use a direct regular-file path before resuming."
            ),
        )
    )


def _checkpoint_foreign_database() -> AgentRunResult:
    return _empty_result(
        _integrity_repair(
            "checkpoint_foreign_database",
            (
                "The checkpoint path is not an ABI Action checkpoint database. "
                "Use the checkpoint created by a fresh ABI Action invocation."
            ),
        )
    )


def _sqlite_codes(*names: str) -> frozenset[int]:
    """Return available platform SQLite constants without assuming extensions."""
    return frozenset(
        value for name in names if isinstance(value := getattr(sqlite3, name, None), int)
    )


def _checkpoint_read_failure(error: BaseException) -> AgentRunResult:
    """Classify checkpoint storage failures without provider inference or text."""
    raw_sqlite_code = getattr(error, "sqlite_errorcode", None)
    sqlite_code = raw_sqlite_code & 0xFF if isinstance(raw_sqlite_code, int) else None
    if isinstance(error, TimeoutError):
        return _empty_result(
            RetryableFailure(
                error_code="checkpoint_read_timeout",
                message="The checkpoint read timed out. Retry the Action continuation.",
            )
        )
    if raw_sqlite_code in _sqlite_codes("SQLITE_IOERR_ACCESS", "SQLITE_IOERR_AUTH"):
        return _empty_result(
            _integrity_repair(
                "checkpoint_permission_denied",
                ("The checkpoint store is not readable. Repair its permissions before resuming."),
            )
        )
    if raw_sqlite_code in _sqlite_codes("SQLITE_IOERR_CORRUPTFS", "SQLITE_IOERR_DATA"):
        return _empty_result(
            _integrity_repair(
                "checkpoint_corrupt",
                (
                    "The checkpoint data is malformed or corrupt. Restore a valid "
                    "checkpoint before resuming."
                ),
            )
        )
    if raw_sqlite_code in _sqlite_codes(
        "SQLITE_IOERR_READ",
        "SQLITE_IOERR_SHORT_READ",
        "SQLITE_IOERR_FSTAT",
        "SQLITE_IOERR_SEEK",
        "SQLITE_IOERR_MMAP",
        "SQLITE_IOERR_CONVPATH",
        "SQLITE_IOERR_VNODE",
    ):
        return _empty_result(
            _integrity_repair(
                "checkpoint_read_failed",
                (
                    "The checkpoint could not be read safely. Repair the checkpoint store "
                    "before resuming."
                ),
            )
        )
    if sqlite_code in _sqlite_codes("SQLITE_BUSY", "SQLITE_LOCKED"):
        return _empty_result(
            RetryableFailure(
                error_code="checkpoint_busy",
                message="The checkpoint store is busy. Retry the Action continuation.",
            )
        )
    if isinstance(error, PermissionError) or sqlite_code in {
        *_sqlite_codes("SQLITE_AUTH", "SQLITE_CANTOPEN", "SQLITE_PERM", "SQLITE_READONLY")
    }:
        return _empty_result(
            _integrity_repair(
                "checkpoint_permission_denied",
                ("The checkpoint store is not readable. Repair its permissions before resuming."),
            )
        )
    if sqlite_code in _sqlite_codes("SQLITE_CORRUPT", "SQLITE_NOTADB"):
        return _empty_result(
            _integrity_repair(
                "checkpoint_corrupt",
                (
                    "The checkpoint data is malformed or corrupt. Restore a valid "
                    "checkpoint before resuming."
                ),
            )
        )
    if sqlite_code in _sqlite_codes("SQLITE_IOERR"):
        return _empty_result(
            _integrity_repair(
                "checkpoint_read_failed",
                (
                    "The checkpoint could not be read safely. Repair the checkpoint store "
                    "before resuming."
                ),
            )
        )
    if isinstance(
        error,
        (sqlite3.DatabaseError, UnicodeError, ValueError, TypeError, KeyError),
    ):
        return _empty_result(
            _integrity_repair(
                "checkpoint_corrupt",
                (
                    "The checkpoint data is malformed or corrupt. Restore a valid "
                    "checkpoint before resuming."
                ),
            )
        )
    return _empty_result(
        _integrity_repair(
            "checkpoint_read_failed",
            (
                "The checkpoint could not be read safely. Repair the checkpoint store "
                "before resuming."
            ),
        )
    )


def _absolute_checkpoint_path(checkpoint_path: Path) -> Path | AgentRunResult:
    if ".." in checkpoint_path.parts:
        return _checkpoint_path_unsafe()
    return checkpoint_path if checkpoint_path.is_absolute() else Path.cwd() / checkpoint_path


def _inspect_checkpoint_path(
    checkpoint_path: Path,
    *,
    require_exists: bool,
) -> tuple[Path, bool] | AgentRunResult:
    """Use lstat on every existing component and never follow path aliases."""
    absolute_result = _absolute_checkpoint_path(checkpoint_path)
    if isinstance(absolute_result, AgentRunResult):
        return absolute_result
    absolute_path = absolute_result
    anchor = Path(absolute_path.anchor)
    components = [anchor]
    current = anchor
    for part in absolute_path.parts[1:]:
        current /= part
        components.append(current)
    for index, component in enumerate(components):
        is_final = index == len(components) - 1
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            if require_exists:
                return _checkpoint_not_resumable()
            return absolute_path, False
        except OSError as error:
            return _checkpoint_read_failure(error)
        if stat.S_ISLNK(mode):
            return _checkpoint_path_unsafe()
        if is_final:
            if not stat.S_ISREG(mode):
                return _checkpoint_path_unsafe()
        elif not stat.S_ISDIR(mode):
            return _checkpoint_path_unsafe()
    return absolute_path, True


_CHECKPOINT_MARKER_KEY = "abi_action_checkpoint"
_CHECKPOINT_FORMAT_VERSION = 1
_CHECKPOINT_OWNER_BYTES = b"abi_action_checkpoint:1\n"


@dataclass(slots=True)
class _CheckpointInitializationEntry:
    """One process/thread lock shared by every runtime using the same local path."""

    lock: LockType
    users: int = 0


@dataclass(slots=True)
class _CheckpointInitializationLease:
    """Reference-counted lease for a path initialization lock."""

    key: str
    entry: _CheckpointInitializationEntry
    released: bool = False

    def release(self) -> None:
        with _CHECKPOINT_INITIALIZATIONS_GUARD:
            if self.released:
                return
            self.released = True
        self.entry.lock.release()
        _drop_checkpoint_initialization_user(self.key, self.entry)


_CHECKPOINT_INITIALIZATIONS: dict[str, _CheckpointInitializationEntry] = {}
_CHECKPOINT_INITIALIZATIONS_GUARD = threading.Lock()
_CHECKPOINT_INITIALIZATION_POLL_SECONDS = 0.005


def _drop_checkpoint_initialization_user(
    key: str,
    entry: _CheckpointInitializationEntry,
) -> None:
    """Drop one holder/waiter reference and remove an unused registry entry."""
    with _CHECKPOINT_INITIALIZATIONS_GUARD:
        entry.users -= 1
        if entry.users == 0 and _CHECKPOINT_INITIALIZATIONS.get(key) is entry:
            del _CHECKPOINT_INITIALIZATIONS[key]


async def _acquire_checkpoint_initialization(
    checkpoint_path: Path,
) -> _CheckpointInitializationLease:
    """Serialize only first-use inspection and marker publication per local path."""
    key = os.fspath(checkpoint_path)
    with _CHECKPOINT_INITIALIZATIONS_GUARD:
        entry = _CHECKPOINT_INITIALIZATIONS.get(key)
        if entry is None:
            entry = _CheckpointInitializationEntry(lock=threading.Lock())
            _CHECKPOINT_INITIALIZATIONS[key] = entry
        entry.users += 1
    acquired = False
    try:
        while not entry.lock.acquire(blocking=False):
            await asyncio.sleep(_CHECKPOINT_INITIALIZATION_POLL_SECONDS)
        acquired = True
    except BaseException:
        if acquired:
            entry.lock.release()
        _drop_checkpoint_initialization_user(key, entry)
        raise
    return _CheckpointInitializationLease(key=key, entry=entry)


def _checkpoint_owner_path(checkpoint_path: Path) -> Path:
    return checkpoint_path.with_name(f"{checkpoint_path.name}.abi-owner")


def _inspect_checkpoint_owner(checkpoint_path: Path) -> bool | AgentRunResult:
    """Return whether the durable ABI ownership sidecar is valid without following it."""
    owner_path = _checkpoint_owner_path(checkpoint_path)
    try:
        mode = owner_path.lstat().st_mode
    except FileNotFoundError:
        return False
    except OSError as error:
        return _checkpoint_read_failure(error)
    if not stat.S_ISREG(mode):
        return _checkpoint_path_unsafe()
    try:
        owner_bytes = owner_path.read_bytes()
    except OSError as error:
        return _checkpoint_read_failure(error)
    if owner_bytes != _CHECKPOINT_OWNER_BYTES:
        return _checkpoint_foreign_database()
    return True


def _claim_checkpoint_owner(checkpoint_path: Path) -> AgentRunResult | None:
    """Atomically publish the ABI ownership sidecar before SQLite initialization."""
    owner_path = _checkpoint_owner_path(checkpoint_path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    temp_path: Path | None = None
    file_descriptor = -1
    for _attempt in range(8):
        candidate = owner_path.with_name(f".{owner_path.name}.{uuid4().hex}.tmp")
        try:
            file_descriptor = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue
        except OSError as error:
            return _checkpoint_read_failure(error)
        temp_path = candidate
        break
    if temp_path is None:
        return _checkpoint_read_failure(OSError("could not allocate ownership temp file"))

    published_identity: tuple[int, int] | None = None
    try:
        view = memoryview(_CHECKPOINT_OWNER_BYTES)
        while view:
            written = os.write(file_descriptor, view)
            if written <= 0:
                raise OSError("ownership sidecar write made no progress")
            view = view[written:]
        os.fsync(file_descriptor)
        temp_stat = os.fstat(file_descriptor)
        published_identity = (temp_stat.st_dev, temp_stat.st_ino)
        try:
            os.link(temp_path, owner_path, follow_symlinks=False)
        except FileExistsError:
            inspected = _inspect_checkpoint_owner(checkpoint_path)
            if inspected is True:
                return None
            if isinstance(inspected, AgentRunResult):
                return inspected
            return _checkpoint_foreign_database()
        temp_path.unlink()
        temp_path = None
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_descriptor = os.open(owner_path.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        if published_identity is not None:
            try:
                owner_stat = owner_path.lstat()
                if (owner_stat.st_dev, owner_stat.st_ino) == published_identity:
                    owner_path.unlink()
                    directory_descriptor = os.open(
                        owner_path.parent,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    )
                    try:
                        os.fsync(directory_descriptor)
                    finally:
                        os.close(directory_descriptor)
            except OSError:
                pass
        return _checkpoint_read_failure(error)
    finally:
        with suppress(OSError):
            os.close(file_descriptor)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return None


async def _preflight_checkpoint_database(
    checkpointer: AsyncSqliteSaver,
) -> AgentRunResult | None:
    """Validate schema and ABI ownership using SELECT-only operations."""
    try:
        required_queries = (
            "SELECT thread_id, checkpoint_ns, checkpoint_id, "
            "parent_checkpoint_id, type, checkpoint, metadata "
            "FROM checkpoints WHERE 0",
            "SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, idx, "
            "channel, type, value FROM writes WHERE 0",
        )
        for query in required_queries:
            async with checkpointer.conn.execute(query) as cursor:
                await cursor.fetchone()
        async with checkpointer.conn.execute(
            "SELECT format_version FROM abi_checkpoint_metadata WHERE marker_key = ?",
            (_CHECKPOINT_MARKER_KEY,),
        ) as cursor:
            marker = await cursor.fetchone()
    except sqlite3.DatabaseError as error:
        raw_code = getattr(error, "sqlite_errorcode", None)
        base_code = raw_code & 0xFF if isinstance(raw_code, int) else None
        if base_code in _sqlite_codes("SQLITE_ERROR"):
            return _checkpoint_foreign_database()
        return _checkpoint_read_failure(error)
    except Exception as error:
        return _checkpoint_read_failure(error)
    if marker != (_CHECKPOINT_FORMAT_VERSION,):
        return _checkpoint_foreign_database()
    return None


async def _initialize_checkpoint_database(
    checkpointer: AsyncSqliteSaver,
) -> None:
    """Create LangGraph storage and mark a newly owned ABI database."""
    await checkpointer.setup()
    async with checkpointer.lock, checkpointer.conn.cursor() as cursor:
        await cursor.execute(
            "CREATE TABLE IF NOT EXISTS abi_checkpoint_metadata ("
            "marker_key TEXT PRIMARY KEY, format_version INTEGER NOT NULL)"
        )
        await cursor.execute(
            "INSERT OR REPLACE INTO abi_checkpoint_metadata "
            "(marker_key, format_version) VALUES (?, ?)",
            (_CHECKPOINT_MARKER_KEY, _CHECKPOINT_FORMAT_VERSION),
        )
        await checkpointer.conn.commit()


def _pending_hitl_interrupts(
    state_snapshot: StateSnapshot,
) -> tuple[_PendingHitlInterrupt, ...] | None:
    """Parse the current public task interrupts without consulting write history."""
    pending: list[_PendingHitlInterrupt] = []
    interrupt_ids: set[str] = set()
    for task in state_snapshot.tasks:
        # LangGraph maps a historical resume write to {} while the same task re-interrupts.
        empty_resume_result = isinstance(task.result, dict) and not task.result
        if task.error is not None or (task.result is not None and not empty_resume_result):
            continue
        for interrupt_value in task.interrupts:
            parsed = _parse_pending_hitl_interrupt(interrupt_value, task_id=task.id)
            if parsed is None or parsed.interrupt_id in interrupt_ids:
                return None
            interrupt_ids.add(parsed.interrupt_id)
            pending.append(parsed)
    return tuple(pending) or None


def _validate_hitl_resume(
    request: AgentActionRequest,
    state_snapshot: StateSnapshot,
) -> AgentRunResult | None:
    """Fail closed unless this exact checkpoint has an allowed HITL interrupt."""
    if not isinstance(request.resume, HitlResume):
        return None
    pending = _pending_hitl_interrupts(state_snapshot)
    if pending is None:
        return _checkpoint_not_resumable()
    pending_by_id = {item.interrupt_id: item for item in pending}
    supplied_by_id = {item.interrupt_id: item for item in request.resume.interrupts}
    if supplied_by_id.keys() != pending_by_id.keys():
        return _empty_result(
            _integrity_repair(
                "hitl_interrupt_id_mismatch",
                (
                    "The supplied HITL interrupt ids do not exactly match the pending "
                    "checkpoint interrupts. Refresh the pending approvals and retry."
                ),
            )
        )
    count_mismatch = next(
        (
            (supplied, pending_interrupt)
            for interrupt_id, pending_interrupt in pending_by_id.items()
            if len(supplied := supplied_by_id[interrupt_id].decisions)
            != len(pending_interrupt.actions)
        ),
        None,
    )
    if count_mismatch is not None:
        supplied, pending_interrupt = count_mismatch
        return _empty_result(
            _integrity_repair(
                "hitl_decision_count_mismatch",
                (
                    f"Provided {len(supplied)} HITL decisions for "
                    f"{len(pending_interrupt.actions)} pending tools. Provide one "
                    "ordered approve/reject "
                    "decision per pending tool."
                ),
            )
        )
    allowed_tools = frozenset(request.approval_tools)
    if any(
        action.tool_name not in allowed_tools
        for interrupt in pending
        for action in interrupt.actions
    ):
        return _empty_result(
            _integrity_repair(
                "hitl_tool_not_allowed",
                (
                    "A pending HITL tool is no longer in this Action's approval allowlist. "
                    "Start a fresh authorized Action invocation."
                ),
            )
        )
    if any(
        decision.decision not in action.allowed_decisions
        for interrupt in pending
        for decision, action in zip(
            supplied_by_id[interrupt.interrupt_id].decisions,
            interrupt.actions,
            strict=True,
        )
    ):
        return _empty_result(
            _integrity_repair(
                "hitl_decision_not_allowed",
                (
                    "A supplied HITL decision is not allowed by the pending review policy. "
                    "Provide an allowed ordered decision."
                ),
            )
        )
    return None


def _hitl_resume_command(resume: HitlResume) -> Command[Any]:
    """Map every ABI decision group to its addressed interrupt id."""
    provider_resume: dict[str, dict[str, list[dict[str, str]]]] = {}
    for interrupt in resume.interrupts:
        provider_decisions: list[dict[str, str]] = []
        for item in interrupt.decisions:
            decision: dict[str, str] = {"type": item.decision}
            if item.decision == "reject" and item.feedback:
                decision["message"] = item.feedback
            provider_decisions.append(decision)
        provider_resume[interrupt.interrupt_id] = {"decisions": provider_decisions}
    return Command(resume=provider_resume)


class _CostCallback(BaseCallbackHandler):
    """Budget gate and structured accounting for every Action model call."""

    raise_error = True

    def __init__(
        self,
        *,
        model: str,
        budget: BudgetGate,
        events: EventLogger,
        metrics: MetricsAggregator,
        agent_name: str,
        max_output_tokens: int,
        attempt_id: str = "provider-attempt",
        invocation_id: str = "provider-invocation",
    ) -> None:
        self._model = model
        self._budget = budget
        self._events = events
        self._metrics = metrics
        self._agent = agent_name
        self._max_out = max_output_tokens
        self._attempt_id = attempt_id
        self._invocation_id = invocation_id
        self.llm_calls = 0
        self.cost_usd = 0.0
        self.tool_log: list[ToolCallRecord] = []
        self._started_llm_runs: set[UUID] = set()
        self._finalized_llm_runs: set[UUID] = set()
        self._call_ordinals: dict[UUID, int] = {}

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        if run_id in self._started_llm_runs:
            return
        self._started_llm_runs.add(run_id)
        self.llm_calls += 1
        self._call_ordinals[run_id] = self.llm_calls
        joined = "\n".join(str(message.content) for batch in messages for message in batch)
        estimated_input = _estimate_text_tokens(joined)
        estimated_cost = estimate_cost_usd(
            self._model,
            tokens_in=estimated_input,
            tokens_out=self._max_out,
        )
        self._budget.admit(estimated_cost)

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        if run_id not in self._started_llm_runs or run_id in self._finalized_llm_runs:
            return
        self._finalized_llm_runs.add(run_id)
        tokens_in, tokens_out = self._extract_usage(response)
        cost = estimate_cost_usd(self._model, tokens_in=tokens_in, tokens_out=tokens_out)
        self._budget.record(cost)
        self.cost_usd += cost
        self._metrics.record_llm_call(tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost)
        call_id = f"{self._invocation_id}:call:{self._call_ordinals[run_id]}"
        self._events.event(
            "agent.call",
            event_id=f"agent.call:{call_id}",
            agent=self._agent,
            attempt_id=self._attempt_id,
            call_id=call_id,
            model=self._model,
            tokens={"input": tokens_in, "output": tokens_out},
            cost_usd=round(cost, 6),
            outcome="ok",
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._record_failed_attempt(run_id, _runtime_error_classification(error))

    def record_pending_failure(self, classification: str) -> None:
        """Account for callback-start failures that bypass ``on_llm_error``."""
        for run_id in self._started_llm_runs - self._finalized_llm_runs:
            self._record_failed_attempt(run_id, classification)

    def _record_failed_attempt(self, run_id: UUID, classification: str) -> None:
        if run_id not in self._started_llm_runs or run_id in self._finalized_llm_runs:
            return
        self._finalized_llm_runs.add(run_id)
        self._metrics.record_llm_call(tokens_in=0, tokens_out=0, cost_usd=0.0)
        call_id = f"{self._invocation_id}:call:{self._call_ordinals[run_id]}"
        self._events.event(
            "agent.call",
            event_id=f"agent.call:{call_id}",
            agent=self._agent,
            attempt_id=self._attempt_id,
            call_id=call_id,
            model=self._model,
            tokens={"input": 0, "output": 0},
            cost_usd=0.0,
            outcome="error",
            error_classification=classification,
        )

    def record_tool_start(self, binding: ToolBinding, arguments: dict[str, object]) -> None:
        """Record a validated ABI handler immediately before it is invoked."""
        self.tool_log.append(
            ToolCallRecord(
                name=binding.name,
                arguments_json=json.dumps(arguments, ensure_ascii=False, sort_keys=True),
            )
        )

    def _extract_usage(self, response: Any) -> tuple[int, int]:
        output = getattr(response, "llm_output", None) or {}
        usage = output.get("token_usage") or output.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens", 0) or 0)
        tokens_out = int(usage.get("completion_tokens", 0) or 0)
        if tokens_in or tokens_out:
            return tokens_in, tokens_out
        for generation_list in getattr(response, "generations", ()):
            for generation in generation_list:
                message = getattr(generation, "message", None)
                metadata = getattr(message, "usage_metadata", None) if message else None
                if metadata:
                    return int(metadata.get("input_tokens", 0)), int(
                        metadata.get("output_tokens", 0)
                    )
                text = getattr(generation, "text", "") or ""
                if text:
                    return 0, _estimate_text_tokens(text)
        return 0, self._max_out // 4


class AgentRuntime:
    """Run bounded Action agents while sharing budget and observability services."""

    def __init__(
        self,
        *,
        config: LLMConfig,
        api_key: str,
        budget: BudgetGate,
        events: EventLogger,
        metrics: MetricsAggregator,
        langfuse_handler: Any | None,
        langfuse_status: LangfuseStatus,
        sem: asyncio.Semaphore,
    ) -> None:
        self._config = config
        self._api_key = api_key
        self._budget = budget
        self._events = events
        self._metrics = metrics
        self._langfuse = langfuse_handler
        self._langfuse_status = langfuse_status
        self._sem = sem
        self._model: BaseChatModel | None = None

    @property
    def budget(self) -> BudgetGate:
        return self._budget

    @property
    def langfuse_status(self) -> LangfuseStatus:
        return self._langfuse_status

    def _get_model(self) -> BaseChatModel:
        if self._model is None:
            provider_options: dict[str, Any] = {}
            if self._config.thinking_mode == "disabled":
                provider_options["extra_body"] = {"thinking": {"type": "disabled"}}
            self._model = ChatOpenAI(
                base_url=self._config.base_url,
                api_key=self._api_key,
                model=self._config.model,
                temperature=self._config.temperature,
                max_tokens=self._config.max_output_tokens,
                timeout=self._config.request_timeout_s,
                max_retries=0,
                # Keep tool messages strictly one request/response pair at a
                # time for OpenAI-compatible providers with fragile parallel
                # tool-call history validation. Actions can still run in parallel.
                model_kwargs={"parallel_tool_calls": False},
                **provider_options,
            )
        return self._model

    async def inspect_hitl_checkpoint(
        self, request: AgentActionRequest
    ) -> HitlCheckpointInspection:
        """Inspect current public graph state without invoking a model or tool."""
        if not isinstance(request.resume, HitlResume):
            raise TypeError("HITL checkpoint inspection requires a typed HitlResume")
        absolute = _absolute_checkpoint_path(request.checkpoint_path)
        if isinstance(absolute, AgentRunResult):
            return HitlCheckpointInspection(
                disposition="indeterminate",
                detail="checkpoint path is not safely inspectable",
            )
        path_inspection = _inspect_checkpoint_path(absolute, require_exists=True)
        if isinstance(path_inspection, AgentRunResult):
            return HitlCheckpointInspection(
                disposition="indeterminate",
                detail="checkpoint file is absent or invalid",
            )
        checkpoint_path, _ = path_inspection
        owner = _inspect_checkpoint_owner(checkpoint_path)
        if isinstance(owner, AgentRunResult):
            return HitlCheckpointInspection(
                disposition="indeterminate",
                detail="checkpoint ownership cannot be proven",
            )
        config: RunnableConfig = {
            "configurable": {"thread_id": request.thread_id},
            "recursion_limit": request.max_iterations * 2 + 6,
        }
        try:
            async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
                control = await _preflight_checkpoint_database(checkpointer)
                if control is not None:
                    return HitlCheckpointInspection(
                        disposition="indeterminate",
                        detail="checkpoint database failed ABI preflight",
                    )
                agent = create_agent(
                    model=self._get_model(),
                    tools=[to_langchain_tool(tool) for tool in request.tools],
                    system_prompt=request.system_prompt,
                    checkpointer=checkpointer,
                    name=request.agent_name,
                    middleware=(
                        HumanInTheLoopMiddleware(
                            interrupt_on={
                                name: {"allowed_decisions": ["approve", "reject"]}
                                for name in request.approval_tools
                            }
                        ),
                    ),
                )
                snapshot = await agent.aget_state(config)
        except Exception as exc:
            return HitlCheckpointInspection(
                disposition="indeterminate",
                detail=f"checkpoint inspection failed: {type(exc).__name__}",
            )
        pending = _pending_hitl_interrupts(snapshot)
        if pending is not None:
            pending_ids = {item.interrupt_id for item in pending}
            claimed_ids = {item.interrupt_id for item in request.resume.interrupts}
            if pending_ids == claimed_ids:
                return HitlCheckpointInspection(disposition="not_started")
            return HitlCheckpointInspection(
                disposition="outcome",
                outcome=Paused(
                    reason="hitl",
                    message="Action paused for approval before a tool call.",
                    pending_hitl_interrupts=_public_pending_hitl_interrupts(pending),
                ),
            )
        values = snapshot.values
        completion = _terminal_completion(values)
        if completion is not None:
            return HitlCheckpointInspection(disposition="outcome", outcome=completion)
        return HitlCheckpointInspection(
            disposition="indeterminate",
            detail="checkpoint has neither a current interrupt nor a typed terminal outcome",
        )

    async def run_action(self, request: AgentActionRequest) -> AgentRunResult:
        """Execute or resume an Action and return only ABI-owned frozen models."""
        resume_key = (
            "fresh"
            if request.resume is None
            else hashlib.sha256(request.resume.model_dump_json().encode()).hexdigest()[:12]
        )
        invocation_id = f"{request.thread_id}:{resume_key}"
        callback = _CostCallback(
            model=self._config.model,
            budget=self._budget,
            events=self._events,
            metrics=self._metrics,
            agent_name=request.agent_name,
            max_output_tokens=self._config.max_output_tokens,
            attempt_id=request.thread_id,
            invocation_id=invocation_id,
        )
        callbacks: list[BaseCallbackHandler] = [callback]
        langfuse_callback = callback_handler(self._langfuse)
        if langfuse_callback is not None:
            callbacks.append(langfuse_callback)
        thread_parts = request.thread_id.split("/", 2)
        run_id = thread_parts[0]
        action_id = thread_parts[1] if len(thread_parts) > 1 else ""
        attempt = thread_parts[2] if len(thread_parts) > 2 else ""
        config: RunnableConfig = {
            "configurable": {"thread_id": request.thread_id},
            "recursion_limit": request.max_iterations * 2 + 6,
            "callbacks": callbacks,
            "metadata": {
                "agent": request.agent_name,
                "run_id": run_id,
                "action_id": action_id,
                "attempt": attempt,
                "langfuse_session_id": run_id,
                "langfuse_trace_name": "abi.agent-action",
                "langfuse_tags": ["abi", "agent-action", request.agent_name],
            },
            "tags": ["abi", "agent-action", request.agent_name],
            "run_name": "abi.agent-action",
        }
        self._events.event(
            "agent.run.start",
            event_id=f"agent.run.start:{invocation_id}",
            agent=request.agent_name,
            thread_id=request.thread_id,
            attempt_id=request.thread_id,
            max_iterations=request.max_iterations,
        )

        def finish(result: AgentRunResult) -> AgentRunResult:
            self._events.event(
                "agent.run.end",
                event_id=(
                    f"agent.run.end:{invocation_id}:{result.outcome.kind}:{result.stopped_reason}"
                ),
                agent=request.agent_name,
                thread_id=request.thread_id,
                attempt_id=request.thread_id,
                outcome=result.outcome.kind,
                stopped_reason=result.stopped_reason,
                llm_calls=result.llm_calls,
                tool_calls=result.tool_calls,
                cost_usd=round(result.cost_usd, 6),
            )
            return result

        absolute_result = _absolute_checkpoint_path(request.checkpoint_path)
        if isinstance(absolute_result, AgentRunResult):
            return finish(absolute_result)
        initialization_lease = await _acquire_checkpoint_initialization(absolute_result)

        checkpoint_phase: Literal[
            "saver_enter", "checkpoint_read", "graph_invoke", "saver_exit", "graph_result"
        ] = "saver_enter"
        try:
            path_inspection = _inspect_checkpoint_path(
                absolute_result,
                require_exists=request.resume is not None,
            )
            if isinstance(path_inspection, AgentRunResult):
                return finish(path_inspection)
            checkpoint_path, checkpoint_existed = path_inspection
            if not checkpoint_existed:
                try:
                    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                except OSError as error:
                    return finish(_checkpoint_read_failure(error))
            owner_inspection = _inspect_checkpoint_owner(checkpoint_path)
            if isinstance(owner_inspection, AgentRunResult):
                return finish(owner_inspection)
            owner_claimed = owner_inspection
            if not checkpoint_existed and not owner_claimed:
                claim_failure = _claim_checkpoint_owner(checkpoint_path)
                if claim_failure is not None:
                    return finish(claim_failure)
                owner_claimed = True

            raw_result: dict[str, Any] | None = None
            control_result: AgentRunResult | None = None
            async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
                checkpoint_phase = "checkpoint_read"
                if checkpoint_existed:
                    control_result = await _preflight_checkpoint_database(checkpointer)
                    if (
                        request.resume is None
                        and owner_claimed
                        and control_result is not None
                        and isinstance(control_result.outcome, RepairRequired)
                        and control_result.outcome.defect_codes == ("checkpoint_foreign_database",)
                    ):
                        await _initialize_checkpoint_database(checkpointer)
                        control_result = None
                else:
                    await _initialize_checkpoint_database(checkpointer)
                initialization_lease.release()
                if control_result is None and request.resume is not None:
                    checkpoint_tuple = await checkpointer.aget_tuple(config)
                    if checkpoint_tuple is None:
                        control_result = _checkpoint_not_resumable()
                if control_result is None:
                    hitl_middleware_tools = (
                        tuple(tool.name for tool in request.tools)
                        if isinstance(request.resume, HitlResume)
                        else request.approval_tools
                    )
                    agent = create_agent(
                        model=self._get_model(),
                        tools=[
                            to_langchain_tool(tool, on_actual_start=callback.record_tool_start)
                            for tool in request.tools
                        ],
                        system_prompt=request.system_prompt,
                        checkpointer=checkpointer,
                        name=request.agent_name,
                        middleware=(
                            [
                                HumanInTheLoopMiddleware(
                                    interrupt_on={
                                        name: {"allowed_decisions": ["approve", "reject"]}
                                        for name in hitl_middleware_tools
                                    }
                                )
                            ]
                            if hitl_middleware_tools or isinstance(request.resume, HitlResume)
                            else [_serial_mutation_tool_middleware]
                        ),
                    )
                    if isinstance(request.resume, HitlResume):
                        state_snapshot = await agent.aget_state(config)
                        control_result = _validate_hitl_resume(request, state_snapshot)
                if control_result is None:
                    checkpoint_phase = "graph_invoke"
                    graph_input: Any
                    if request.resume is None:
                        graph_input = {
                            "messages": [{"role": "user", "content": request.user_prompt}]
                        }
                    elif isinstance(request.resume, CheckpointResume):
                        graph_input = None
                    else:
                        graph_input = _hitl_resume_command(request.resume)
                    with observation_context(
                        self._langfuse,
                        session_id=run_id,
                        trace_name="abi.agent-action",
                        tags=["abi", "agent-action", request.agent_name],
                        metadata={
                            "agent": request.agent_name,
                            "run_id": run_id,
                            "action_id": action_id,
                            "attempt": attempt,
                        },
                    ):
                        async with self._sem:
                            raw_result = cast(
                                dict[str, Any],
                                await agent.ainvoke(graph_input, config=config),
                            )
                checkpoint_phase = "saver_exit"
            checkpoint_phase = "graph_result"
            if control_result is not None:
                return finish(control_result)
            if raw_result is None:
                raise RuntimeError("graph invocation returned no result")
            if raw_interrupts := raw_result.get("__interrupt__"):
                pending_interrupts = _pending_hitl_interrupt_values(raw_interrupts)
                if pending_interrupts is None:
                    return finish(_checkpoint_not_resumable())
                result = AgentRunResult(
                    outcome=Paused(
                        reason="hitl",
                        message="Action paused for approval before a tool call.",
                        pending_hitl_interrupts=_public_pending_hitl_interrupts(pending_interrupts),
                    ),
                    llm_calls=callback.llm_calls,
                    tool_calls=len(callback.tool_log),
                    cost_usd=callback.cost_usd,
                    stopped_reason="paused",
                    tool_log=tuple(callback.tool_log),
                )
            else:
                tool_log = tuple(callback.tool_log)
                completion = _terminal_completion(raw_result)
                if completion is None:
                    raise ValueError("agent graph ended without a terminal assistant message")
                result = AgentRunResult(
                    outcome=completion,
                    llm_calls=callback.llm_calls,
                    tool_calls=len(tool_log),
                    cost_usd=callback.cost_usd,
                    stopped_reason="completed",
                    tool_log=tool_log,
                )
        except Exception as error:
            if checkpoint_phase in {"saver_enter", "checkpoint_read", "saver_exit"}:
                return finish(_checkpoint_read_failure(error))
            classification = _runtime_error_classification(error, resume=request.resume)
            callback.record_pending_failure(classification)
            failure: ActionOutcome
            stopped_reason: Literal["iteration_limit", "paused", "error"] = "error"
            if classification == "provider_timeout":
                if request.may_have_side_effects and callback.tool_log:
                    failure = Indeterminate(
                        operation_key=request.thread_id,
                        error_code=classification,
                        failure_signature=hashlib.sha256(
                            f"{request.thread_id}:{classification}".encode()
                        ).hexdigest(),
                        message=(
                            "The model provider timed out after a business tool started. "
                            "Reconcile the side effect before retrying."
                        ),
                    )
                else:
                    failure = RetryableFailure(
                        error_code=classification,
                        message=(
                            "The model provider timed out before completion. Retry the Action."
                        ),
                    )
            elif classification == "transient_provider_error":
                failure = RetryableFailure(
                    error_code=classification,
                    message=("The model provider is temporarily unavailable. Retry the Action."),
                )
            elif classification == "permanent_provider_error":
                failure = PermanentFailure(
                    error_code=classification,
                    message=(
                        "The model provider permanently rejected this Action request. "
                        "Repair the request before retrying."
                    ),
                )
            elif classification == "budget":
                failure = Paused(
                    reason="budget",
                    message=(
                        "The Action budget is exhausted. Increase the budget before resuming."
                    ),
                )
                stopped_reason = "paused"
            elif classification == "iteration_limit":
                failure = RetryableFailure(
                    error_code="iteration_limit",
                    message=(
                        "The Action reached its iteration limit. Resume from the checkpoint "
                        "with a higher bounded limit."
                    ),
                )
                stopped_reason = "iteration_limit"
            elif classification == "hitl_decision_count_mismatch":
                counts = _hitl_mismatch_counts(error, request.resume)
                supplied, pending = counts if counts is not None else ("unknown", "unknown")
                failure = _integrity_repair(
                    "hitl_decision_count_mismatch",
                    (
                        f"Provided {supplied} HITL decisions for {pending} pending tools. "
                        "Provide one ordered approve/reject decision per pending tool."
                    ),
                )
            elif classification == "checkpoint_not_resumable":
                failure = _integrity_repair(
                    "checkpoint_not_resumable",
                    (
                        "This thread has no compatible checkpoint to resume. "
                        "Start a fresh Action invocation instead."
                    ),
                )
            else:
                failure = PermanentFailure(
                    error_code="unclassified_exception",
                    message="The Action failed with an unclassified internal error.",
                )
            result = AgentRunResult(
                outcome=failure,
                llm_calls=callback.llm_calls,
                tool_calls=len(callback.tool_log),
                cost_usd=callback.cost_usd,
                stopped_reason=stopped_reason,
                tool_log=tuple(callback.tool_log),
            )
        finally:
            initialization_lease.release()
        return finish(result)


def _set_model_for_testing(runtime: AgentRuntime, model: BaseChatModel) -> AgentRuntime:
    """Inject a fake provider model without expanding the public constructor."""
    runtime._model = model
    return runtime
