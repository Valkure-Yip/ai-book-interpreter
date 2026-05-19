"""Verify the judge model selection priority: CLI > EVAL_JUDGE_MODEL > LLM_MODEL.

Also asserts that ``LLMRouter.invoke_structured(model_override=...)`` swaps
the underlying chat model's ``model`` field while reusing the same base_url
and api_key.
"""

from __future__ import annotations

from abi.eval.pipeline import _resolve_judge_model
from abi.providers.llm.factory import LLMRouter
from abi.types.eval import EvalConfig
from abi.types.run import LLMConfig, RunConfig


class TestResolveJudgeModel:
    def _config(self, llm_model: str = "translator-model") -> RunConfig:
        return RunConfig(
            target_language="zh",
            llm=LLMConfig(model=llm_model, base_url="https://x/v1"),
        )

    def test_default_falls_back_to_translator(self, monkeypatch) -> None:
        monkeypatch.delenv("EVAL_JUDGE_MODEL", raising=False)
        assert (
            _resolve_judge_model(self._config(), EvalConfig())
            == "translator-model"
        )

    def test_env_var_overrides_translator(self, monkeypatch) -> None:
        monkeypatch.setenv("EVAL_JUDGE_MODEL", "judge-from-env")
        assert (
            _resolve_judge_model(self._config(), EvalConfig())
            == "judge-from-env"
        )

    def test_eval_config_overrides_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("EVAL_JUDGE_MODEL", "judge-from-env")
        cfg = EvalConfig(judge_model="judge-from-cli")
        assert _resolve_judge_model(self._config(), cfg) == "judge-from-cli"

    def test_empty_env_var_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("EVAL_JUDGE_MODEL", "")
        assert (
            _resolve_judge_model(self._config(), EvalConfig())
            == "translator-model"
        )


class TestRouterModelOverride:
    def _build_router(self) -> LLMRouter:
        # Build a minimal router with the in-memory observability bits.
        import asyncio
        import tempfile
        from pathlib import Path

        from abi.providers.llm.budget import BudgetGate
        from abi.providers.observability.events import (
            EventLogger,
            MetricsAggregator,
        )
        from abi.providers.observability.langfuse_client import LangfuseStatus

        tmp = Path(tempfile.mkdtemp())
        events = EventLogger(tmp / "events.jsonl", run_id="t")
        metrics = MetricsAggregator(
            tmp / "metrics.json", run_id="t", book_id="b"
        )
        return LLMRouter(
            config=LLMConfig(model="primary-model", base_url="https://x/v1"),
            api_key="sk-test",
            budget=BudgetGate(None),
            events=events,
            metrics=metrics,
            langfuse_handler=None,
            langfuse_status=LangfuseStatus(False, False, "h", reason=""),
            sem=asyncio.Semaphore(1),
        )

    def test_default_returns_primary_chat(self) -> None:
        router = self._build_router()
        chat = router._chat_for(None)
        assert chat is router._chat
        chat_same = router._chat_for("primary-model")
        assert chat_same is router._chat

    def test_override_returns_different_chat(self) -> None:
        router = self._build_router()
        chat_override = router._chat_for("judge-model")
        assert chat_override is not router._chat
        # And it uses the override's model name.
        assert getattr(chat_override, "model_name", getattr(chat_override, "model", "")) == "judge-model"
        # base_url is preserved.
        # langchain-openai stores it on ``openai_api_base`` or ``base_url``.
        base_attr = (
            getattr(chat_override, "openai_api_base", None)
            or getattr(chat_override, "base_url", None)
        )
        assert base_attr is not None
        assert "x/v1" in str(base_attr)

    def test_override_is_cached(self) -> None:
        router = self._build_router()
        first = router._chat_for("judge-model")
        second = router._chat_for("judge-model")
        assert first is second
