"""Unit tests for the Langfuse payload mask.

We only test the mask function itself — actually building a CallbackHandler
requires real network/keys and is covered by the smoke test.
"""

from __future__ import annotations

from abi.providers.observability.langfuse_client import (
    _REDACTED,
    _redacting_mask,
    build_langfuse_handler,
    callback_handler,
    flush_handler,
)
from abi.types.run import LangfuseConfig


class TestRedactingMask:
    def test_redacts_string(self) -> None:
        assert _redacting_mask("secret text") == _REDACTED

    def test_passes_through_primitives(self) -> None:
        assert _redacting_mask(42) == 42
        assert _redacting_mask(3.14) == 3.14
        assert _redacting_mask(True) is True
        assert _redacting_mask(None) is None

    def test_redacts_list(self) -> None:
        result = _redacting_mask(["a", "b", "c"])
        assert result == [_REDACTED, _REDACTED, _REDACTED]

    def test_preserves_role_and_type_keys(self) -> None:
        msg = {"role": "user", "content": "private text", "type": "human"}
        result = _redacting_mask(msg)
        assert result["role"] == "user"
        assert result["type"] == "human"
        assert result["content"] == _REDACTED

    def test_redacts_nested(self) -> None:
        data = {
            "messages": [
                {"role": "system", "content": "you are a helper"},
                {"role": "user", "content": "translate this"},
            ],
            "model": "gpt-x",
        }
        result = _redacting_mask(data)
        assert result["messages"][0]["role"] == "system"
        assert result["messages"][0]["content"] == _REDACTED
        assert result["model"] == _REDACTED


class TestBuildLangfuseHandler:
    def test_disabled_returns_none(self) -> None:
        cfg = LangfuseConfig(enabled=False)
        handler, status = build_langfuse_handler(cfg)
        assert handler is None
        assert status.enabled is False
        assert "disabled" in status.reason

    def test_missing_keys_returns_none(self, monkeypatch) -> None:
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
        cfg = LangfuseConfig(enabled=True)
        handler, status = build_langfuse_handler(cfg)
        assert handler is None
        assert status.enabled is False
        assert "missing env" in status.reason


class _FakeObserver:
    def __init__(self) -> None:
        self.callbacks = 0
        self.flushes = 0

    def callback_handler(self) -> object:
        self.callbacks += 1
        return object()

    def flush(self) -> None:
        self.flushes += 1


def test_callbacks_are_invocation_scoped_and_flush_is_run_scoped() -> None:
    observer = _FakeObserver()

    first = callback_handler(observer)  # type: ignore[arg-type]
    second = callback_handler(observer)  # type: ignore[arg-type]
    flush_handler(observer)  # type: ignore[arg-type]

    assert first is not second
    assert observer.callbacks == 2
    assert observer.flushes == 1
