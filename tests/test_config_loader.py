"""Behavioral tests for explicit provider compatibility configuration."""

from __future__ import annotations

from abi.config.loader import build_run_config


def test_environment_can_disable_provider_thinking_for_agent_tools(
    monkeypatch,
) -> None:
    """Catch the endpoint compatibility switch being absent from CLI config."""
    monkeypatch.setenv("ABI_LLM_THINKING", "disabled")

    config = build_run_config()

    assert config.llm.thinking_mode == "disabled"


def test_environment_bounds_semantic_repair_generations(monkeypatch) -> None:
    monkeypatch.setenv("ABI_MAX_SEMANTIC_REPAIR_ATTEMPTS", "2")

    config = build_run_config()

    assert config.orchestration.max_semantic_repair_attempts == 2
