"""Shared run services: build the structured router and the agent runtime once.

Both the structured-output ``LLMRouter`` and the tool-calling ``AgentRuntime``
share a single :class:`BudgetGate`, Langfuse handler, ``EventLogger`` and
``MetricsAggregator`` for a run, so cost caps and traces span structured calls
*and* autonomous/sub-agent calls uniformly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from abi.providers.agent_runtime.runner import AgentRuntime
from abi.providers.llm.budget import BudgetGate
from abi.providers.llm.factory import LLMRouter, _ensure_api_key
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.providers.observability.langfuse_client import build_langfuse_handler
from abi.types.run import RunConfig


@dataclass
class RunServices:
    router: LLMRouter
    agent: AgentRuntime
    budget: BudgetGate
    events: EventLogger
    metrics: MetricsAggregator

    @property
    def total_cost(self) -> float:
        return self.budget.spent

    def flush(self) -> None:
        self.router.flush()
        self.metrics.flush()


def build_run_services(
    *, config: RunConfig, events: EventLogger, metrics: MetricsAggregator
) -> RunServices:
    api_key = _ensure_api_key(config.llm.api_key_env)
    handler, status = build_langfuse_handler(config.langfuse)
    events.event(
        "observability.langfuse",
        enabled=status.enabled,
        host=status.host,
        full_payload=status.full_payload,
        reason=status.reason,
    )
    budget = BudgetGate(config.cost.hard_cap_usd)
    sem = asyncio.Semaphore(config.llm.max_concurrency)

    router = LLMRouter(
        config=config.llm,
        api_key=api_key,
        budget=budget,
        events=events,
        metrics=metrics,
        langfuse_handler=handler,
        langfuse_status=status,
        sem=sem,
    )
    agent = AgentRuntime(
        config=config.llm,
        api_key=api_key,
        budget=budget,
        events=events,
        metrics=metrics,
        langfuse_handler=handler,
        langfuse_status=status,
        sem=sem,
    )
    return RunServices(
        router=router, agent=agent, budget=budget, events=events, metrics=metrics
    )
