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
