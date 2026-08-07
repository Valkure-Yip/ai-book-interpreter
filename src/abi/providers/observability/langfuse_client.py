"""Langfuse v4 client and per-invocation callback factory.

Returns a shared client wrapper when keys are configured, else ``None``. The
wrapper creates a fresh LangChain ``CallbackHandler`` for every invocation so
parallel Actions never share callback-local run state. Payload masking lives
on the shared v4 client.

See SECURITY.md and design-docs/tech-stack.md §3 for the payload policy.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from abi.types.run import LangfuseConfig

_log = logging.getLogger(__name__)

_REDACTED = "[REDACTED: enable LANGFUSE_FULL_PAYLOAD=1 to view content in Langfuse]"


def _redacting_mask(data: Any, **kwargs: dict[str, Any]) -> Any:
    """Langfuse ``mask`` callable: replace any structured payload with a sentinel.

    Langfuse calls this on every input/output before persisting. We replace
    the *content* but preserve the structural shape so the UI still works.
    """
    if isinstance(data, str):
        return _REDACTED
    if isinstance(data, list):
        return [_redacting_mask(x) for x in data]
    if isinstance(data, dict):
        # Keep keys (often role/type which are useful metadata) but mask values.
        out: dict[str, Any] = {}
        for k, v in data.items():
            if k in {"role", "type"}:
                out[k] = v
            else:
                out[k] = _redacting_mask(v)
        return out
    if isinstance(data, (int, float, bool)) or data is None:
        return data
    return _REDACTED


@dataclass
class LangfuseStatus:
    enabled: bool
    full_payload: bool
    host: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class LangfuseObserver:
    """One run-scoped v4 client with invocation-scoped LangChain callbacks."""

    client: Any
    public_key: str

    def callback_handler(self) -> Any:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler(public_key=self.public_key)

    def flush(self) -> None:
        self.client.flush()


def build_langfuse_handler(
    config: LangfuseConfig,
) -> tuple[LangfuseObserver | None, LangfuseStatus]:
    """Return ``(observer, status)``. Observer is ``None`` when disabled.

    Failures are logged and swallowed — observability must never break the run.
    """
    if not config.enabled:
        return None, LangfuseStatus(False, False, config.host, reason="disabled in config")

    public_key = os.environ.get(config.public_key_env, "")
    secret_key = os.environ.get(config.secret_key_env, "")
    if not public_key or not secret_key:
        return None, LangfuseStatus(
            False, False, config.host,
            reason=f"missing env: {config.public_key_env} or {config.secret_key_env}",
        )

    try:
        from langfuse import Langfuse
    except Exception as exc:  # pragma: no cover
        return None, LangfuseStatus(False, False, config.host, reason=f"import failed: {exc}")

    # auth_check — give the user a fast, friendly error if creds are bad.
    try:
        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=config.host,
            mask=None if config.upload_full_payload else _redacting_mask,
            environment=os.environ.get("ABI_ENVIRONMENT", "development"),
        )
        if not client.auth_check():
            return None, LangfuseStatus(
                False, False, config.host, reason="auth_check failed (invalid keys?)"
            )
    except Exception as exc:  # pragma: no cover
        _log.warning("langfuse auth_check raised: %s", exc)
        return None, LangfuseStatus(
            False,
            False,
            config.host,
            reason=f"auth_check raised: {type(exc).__name__}",
        )

    try:
        observer = LangfuseObserver(client=client, public_key=public_key)
        observer.callback_handler()
    except Exception as exc:  # pragma: no cover
        return None, LangfuseStatus(False, False, config.host, reason=f"init failed: {exc}")

    return observer, LangfuseStatus(
        enabled=True,
        full_payload=config.upload_full_payload,
        host=config.host,
    )


def callback_handler(observer: LangfuseObserver | None) -> Any | None:
    """Return a fresh callback for one invocation, or ``None`` when disabled."""
    if observer is None:
        return None
    try:
        return observer.callback_handler()
    except Exception as exc:  # pragma: no cover
        _log.debug("langfuse callback creation ignored: %s", exc)
        return None


@contextmanager
def observation_context(
    observer: LangfuseObserver | None,
    *,
    session_id: str | None,
    trace_name: str,
    tags: list[str],
    metadata: dict[str, Any],
) -> Iterator[None]:
    """Propagate trace attributes through nested LangGraph/LangChain runs."""
    if observer is None:
        yield
        return
    from langfuse import propagate_attributes

    with propagate_attributes(
        session_id=session_id,
        trace_name=trace_name,
        tags=tags,
        metadata=metadata,
    ):
        yield


def flush_handler(observer: LangfuseObserver | None) -> None:
    """Block until pending traces are sent. Safe to call with ``None``."""
    if observer is None:
        return
    try:
        observer.flush()
    except Exception as exc:  # pragma: no cover
        _log.debug("langfuse flush ignored: %s", exc)


def get_langfuse_client(config: LangfuseConfig) -> Any | None:
    """Return a low-level :class:`langfuse.Langfuse` client or ``None``.

    Used by the eval pipeline for Dataset / Score / Dataset-Run APIs that
    the LangChain ``CallbackHandler`` doesn't expose. Reuses the same env
    vars and host as :func:`build_langfuse_handler` and returns ``None``
    when keys are missing — observability must never break the run.
    """
    if not config.enabled:
        return None
    public_key = os.environ.get(config.public_key_env, "")
    secret_key = os.environ.get(config.secret_key_env, "")
    if not public_key or not secret_key:
        return None
    try:
        from langfuse import Langfuse

        return Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=config.host,
            mask=None if config.upload_full_payload else _redacting_mask,
        )
    except Exception as exc:  # pragma: no cover
        _log.warning("langfuse client init failed: %s", exc)
        return None
