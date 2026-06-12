"""LangGraph tool-calling agent runtime.

This is the **only** module besides ``providers/llm`` that touches the model
SDK / agent framework. Business stages call :meth:`AgentRuntime.run` with a
system prompt, a user prompt, and a tool belt; every LLM call routes through a
cost callback that records tokens/cost to the shared :class:`BudgetGate`,
``events.jsonl`` and ``metrics.json``, plus the Langfuse handler — preserving
the observability/budget invariant for autonomous (and sub-agent) calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

from abi.providers.llm.budget import BudgetGate
from abi.providers.llm.pricing import estimate_cost_usd
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.providers.observability.langfuse_client import LangfuseStatus
from abi.types.run import LLMConfig


@dataclass
class AgentResult:
    """Outcome of one agent invocation."""

    final_text: str
    messages: list[BaseMessage]
    tool_calls: int
    llm_calls: int
    cost_usd: float
    stopped_reason: str = "completed"  # completed | recursion_limit | error
    tool_log: list[dict[str, Any]] = field(default_factory=list)


def _estimate_text_tokens(text: str) -> int:
    return max(1, len(text) // 3) if text else 0


class _CostCallback(BaseCallbackHandler):
    """Budget-gate + token/cost accounting for every model call in the loop."""

    def __init__(
        self,
        *,
        model: str,
        budget: BudgetGate,
        events: EventLogger,
        metrics: MetricsAggregator,
        agent_name: str,
        max_output_tokens: int,
    ) -> None:
        self._model = model
        self._budget = budget
        self._events = events
        self._metrics = metrics
        self._agent = agent_name
        self._max_out = max_output_tokens
        self.llm_calls = 0
        self.cost_usd = 0.0

    def on_chat_model_start(
        self, serialized: Any, messages: list[list[BaseMessage]], **kwargs: Any
    ) -> None:
        joined = ""
        for batch in messages:
            for m in batch:
                content = m.content if isinstance(m.content, str) else str(m.content)
                joined += content + "\n"
        est_in = _estimate_text_tokens(joined)
        est_cost = estimate_cost_usd(
            self._model, tokens_in=est_in, tokens_out=self._max_out
        )
        # Raises BudgetExceeded -> propagates out of the graph (graceful stop).
        self._budget.admit(est_cost)

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        tokens_in, tokens_out = self._extract_usage(response)
        cost = estimate_cost_usd(self._model, tokens_in=tokens_in, tokens_out=tokens_out)
        self._budget.record(cost)
        self.cost_usd += cost
        self.llm_calls += 1
        self._metrics.record_llm_call(
            tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost
        )
        self._events.event(
            "agent.call",
            agent=self._agent,
            model=self._model,
            tokens={"input": tokens_in, "output": tokens_out},
            cost_usd=round(cost, 6),
            outcome="ok",
        )

    def _extract_usage(self, response: Any) -> tuple[int, int]:
        # ChatOpenAI populates llm_output["token_usage"]; fall back to estimates.
        out = getattr(response, "llm_output", None) or {}
        usage = out.get("token_usage") or out.get("usage") or {}
        tin = int(usage.get("prompt_tokens", 0) or 0)
        tout = int(usage.get("completion_tokens", 0) or 0)
        if tin or tout:
            return tin, tout
        # Try usage_metadata on the generated messages.
        try:
            for gen_list in response.generations:
                for gen in gen_list:
                    msg = getattr(gen, "message", None)
                    um = getattr(msg, "usage_metadata", None) if msg else None
                    if um:
                        return int(um.get("input_tokens", 0)), int(
                            um.get("output_tokens", 0)
                        )
                    text = getattr(gen, "text", "") or ""
                    if text:
                        return 0, _estimate_text_tokens(text)
        except Exception:  # pragma: no cover - best-effort accounting
            pass
        return 0, self._max_out // 4


class AgentRuntime:
    """Builds and runs bounded LangGraph react agents sharing one run's budget."""

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
        self._models: dict[str, ChatOpenAI] = {}

    @property
    def budget(self) -> BudgetGate:
        return self._budget

    @property
    def langfuse_status(self) -> LangfuseStatus:
        return self._langfuse_status

    def _model_for(self, model_override: str | None) -> ChatOpenAI:
        name = model_override or self._config.model
        if name not in self._models:
            self._models[name] = ChatOpenAI(
                base_url=self._config.base_url,
                api_key=self._api_key,
                model=name,
                temperature=self._config.temperature,
                max_tokens=self._config.max_output_tokens,
                timeout=self._config.request_timeout_s,
                max_retries=2,
            )
        return self._models[name]

    async def run(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[BaseTool],
        agent_name: str,
        max_iterations: int = 40,
        model_override: str | None = None,
        thread_id: str | None = None,
    ) -> AgentResult:
        """Run one bounded tool-calling agent to completion.

        ``max_iterations`` caps reasoning<->tool cycles (each cycle is a model
        call plus a tool batch). Hitting the cap stops gracefully with
        ``stopped_reason="recursion_limit"`` so the caller's gate logic decides
        whether to retry.
        """
        model = self._model_for(model_override)
        cost_cb = _CostCallback(
            model=model_override or self._config.model,
            budget=self._budget,
            events=self._events,
            metrics=self._metrics,
            agent_name=agent_name,
            max_output_tokens=self._config.max_output_tokens,
        )
        callbacks: list[BaseCallbackHandler] = [cost_cb]
        if self._langfuse is not None:
            callbacks.append(self._langfuse)

        checkpointer = InMemorySaver()
        agent = create_react_agent(
            model,
            tools,
            prompt=system_prompt,
            checkpointer=checkpointer,
        )
        # recursion_limit counts graph super-steps; ~2 per reason/tool cycle.
        recursion_limit = max_iterations * 2 + 6
        cfg: dict[str, Any] = {
            "callbacks": callbacks,
            "recursion_limit": recursion_limit,
            "configurable": {"thread_id": thread_id or agent_name},
            "metadata": {"agent": agent_name},
            "tags": [agent_name],
            "run_name": agent_name,
        }

        self._events.event("agent.run.start", agent=agent_name, max_iterations=max_iterations)
        stopped = "completed"
        messages: list[BaseMessage] = []
        try:
            async with self._sem:
                result = await agent.ainvoke(
                    {"messages": [HumanMessage(content=user_prompt)]}, config=cfg
                )
            messages = list(result.get("messages", []))
        except Exception as exc:
            name = type(exc).__name__
            if "GraphRecursionError" in name or "recursion" in str(exc).lower():
                stopped = "recursion_limit"
                self._events.event("agent.run.capped", agent=agent_name, detail=str(exc)[:200])
            elif name == "BudgetExceeded":
                self._events.event("agent.run.budget", agent=agent_name, detail=str(exc)[:200])
                raise
            else:
                stopped = "error"
                self._events.event("agent.run.error", agent=agent_name, error=name,
                                   detail=str(exc)[:300])
                raise

        final_text, tool_calls, tool_log = self._summarize(messages)
        self._events.event(
            "agent.run.end",
            agent=agent_name,
            stopped_reason=stopped,
            llm_calls=cost_cb.llm_calls,
            tool_calls=tool_calls,
            cost_usd=round(cost_cb.cost_usd, 6),
        )
        return AgentResult(
            final_text=final_text,
            messages=messages,
            tool_calls=tool_calls,
            llm_calls=cost_cb.llm_calls,
            cost_usd=cost_cb.cost_usd,
            stopped_reason=stopped,
            tool_log=tool_log,
        )

    @staticmethod
    def _summarize(messages: list[BaseMessage]) -> tuple[str, int, list[dict[str, Any]]]:
        final_text = ""
        tool_calls = 0
        tool_log: list[dict[str, Any]] = []
        for m in messages:
            if isinstance(m, AIMessage):
                tc = getattr(m, "tool_calls", None) or []
                tool_calls += len(tc)
                for call in tc:
                    tool_log.append({"name": call.get("name"), "args": call.get("args")})
                if isinstance(m.content, str) and m.content.strip():
                    final_text = m.content
        return final_text, tool_calls, tool_log
