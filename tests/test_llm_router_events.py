"""Regression tests for stable, collision-free LLM provider event identity."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage, HumanMessage

from abi.providers.llm.budget import BudgetGate
from abi.providers.llm.factory import LLMRouter
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.providers.observability.langfuse_client import LangfuseStatus
from abi.types._base import FrozenModel
from abi.types.run import LLMConfig


class _Reply(FrozenModel):
    answer: str


class _StructuredModel:
    def with_retry(self, **kwargs: object) -> _StructuredModel:
        return self

    async def ainvoke(self, messages: object, config: object) -> _Reply:
        return _Reply(answer="same deterministic answer")


class _ChatModel:
    def with_structured_output(self, schema: object, *, method: str) -> _StructuredModel:
        return _StructuredModel()


class _ConfigRecordingStructuredModel(_StructuredModel):
    def __init__(self) -> None:
        self.config: object | None = None

    async def ainvoke(self, messages: object, config: object) -> _Reply:
        self.config = config
        return await super().ainvoke(messages, config)


class _ConfigRecordingChatModel:
    def __init__(self) -> None:
        self.structured = _ConfigRecordingStructuredModel()

    def with_structured_output(
        self, schema: object, *, method: str
    ) -> _ConfigRecordingStructuredModel:
        return self.structured


class _JsonTokenRequiredStructuredModel:
    def with_retry(self, **kwargs: object) -> _JsonTokenRequiredStructuredModel:
        return self

    async def ainvoke(self, messages: object, config: object) -> _Reply:
        if not isinstance(messages, list) or not any(
            isinstance(message, BaseMessage) and "json" in str(message.content).lower()
            for message in messages
        ):
            raise ValueError("provider requires an explicit json output instruction")
        if not any(
            isinstance(message, BaseMessage) and '"answer"' in str(message.content)
            for message in messages
        ):
            raise ValueError("provider requires the requested json schema")
        return _Reply(answer="provider accepted the structured request")


class _JsonTokenRequiredChatModel:
    def with_structured_output(
        self, schema: object, *, method: str
    ) -> _JsonTokenRequiredStructuredModel:
        return _JsonTokenRequiredStructuredModel()


@pytest.mark.asyncio
async def test_structured_router_supplies_provider_owned_json_schema_instruction(
    tmp_path: Path,
) -> None:
    """Catch JSON-mode calls that rely on incidental wording in business prompts."""
    router = LLMRouter(
        config=LLMConfig(model="gpt-4o-mini", max_output_tokens=64),
        api_key="test-key",
        budget=BudgetGate(None),
        events=EventLogger(tmp_path / "events.jsonl", "run-1"),
        metrics=MetricsAggregator(tmp_path / "metrics.json", "run-1", "book-1"),
        langfuse_handler=None,
        langfuse_status=LangfuseStatus(False, False, "", reason="test"),
        sem=asyncio.Semaphore(1),
    )
    router._chat = _JsonTokenRequiredChatModel()  # type: ignore[assignment]
    router._chat_by_model = {router.model: router._chat}

    reply, _ = await router.invoke_structured(
        _Reply,
        [HumanMessage(content="choose the next eligible action")],
        agent_name="orchestration.planner",
        metadata={"logical_invocation_id": "planner:run-1:plan:1"},
        max_retries=0,
    )

    assert reply == _Reply(answer="provider accepted the structured request")


@pytest.mark.asyncio
async def test_structured_router_groups_trace_by_run_session(tmp_path: Path) -> None:
    router = LLMRouter(
        config=LLMConfig(model="gpt-4o-mini", max_output_tokens=64),
        api_key="test-key",
        budget=BudgetGate(None),
        events=EventLogger(tmp_path / "events.jsonl", "run-1"),
        metrics=MetricsAggregator(tmp_path / "metrics.json", "run-1", "book-1"),
        langfuse_handler=None,
        langfuse_status=LangfuseStatus(False, False, "", reason="test"),
        sem=asyncio.Semaphore(1),
    )
    chat = _ConfigRecordingChatModel()
    router._chat = chat  # type: ignore[assignment]
    router._chat_by_model = {router.model: router._chat}

    await router.invoke_structured(
        _Reply,
        [HumanMessage(content="choose the next eligible action")],
        agent_name="orchestration.planner",
        metadata={"run_id": "run-1", "logical_invocation_id": "planner:run-1:plan:1"},
        max_retries=0,
    )

    assert isinstance(chat.structured.config, dict)
    assert chat.structured.config["run_name"] == "abi.structured-call"
    metadata = chat.structured.config["metadata"]
    assert metadata["langfuse_session_id"] == "run-1"
    assert metadata["langfuse_trace_name"] == "abi.structured-call"


@pytest.mark.asyncio
async def test_identical_legitimate_calls_have_distinct_stable_provider_events(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    metrics = MetricsAggregator(tmp_path / "metrics.json", "run-1", "book-1")
    budget = BudgetGate(None)
    router = LLMRouter(
        config=LLMConfig(model="gpt-4o-mini", max_output_tokens=64),
        api_key="test-key",
        budget=budget,
        events=EventLogger(events_path, "run-1"),
        metrics=metrics,
        langfuse_handler=None,
        langfuse_status=LangfuseStatus(False, False, "", reason="test"),
        sem=asyncio.Semaphore(1),
    )
    router._chat = _ChatModel()  # type: ignore[assignment]
    router._chat_by_model = {router.model: router._chat}
    messages: list[BaseMessage] = [HumanMessage(content="translate the same text")]

    _, first = await router.invoke_structured(
        _Reply,
        messages,
        agent_name="translator",
        metadata={"logical_invocation_id": "chapter-1:paragraph-1"},
        max_retries=0,
    )
    _, second = await router.invoke_structured(
        _Reply,
        messages,
        agent_name="translator",
        metadata={"logical_invocation_id": "chapter-1:paragraph-2"},
        max_retries=0,
    )

    calls = tuple(
        record
        for record in (
            json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()
        )
        if record["event"] == "agent.call"
    )
    observed = (
        len(calls),
        len({record["event_id"] for record in calls}),
        len({record["call_id"] for record in calls}),
        metrics.snapshot()["llm_calls"],
        budget.spent,
    )
    assert observed == (2, 2, 2, 2, first.cost_usd + second.cost_usd)
    assert tuple(record["logical_invocation_id"] for record in calls) == (
        "chapter-1:paragraph-1",
        "chapter-1:paragraph-2",
    )
