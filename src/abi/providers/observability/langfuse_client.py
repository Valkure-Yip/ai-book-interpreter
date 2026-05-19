"""Langfuse callback handler factory.

Returns a LangChain ``CallbackHandler`` when keys are configured, else ``None``.
Honors the ``upload_full_payload`` flag via Langfuse's ``mask`` hook.

See SECURITY.md and design-docs/tech-stack.md §3 for the payload policy.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from abi.types.run import LangfuseConfig

_log = logging.getLogger(__name__)

_REDACTED = "[REDACTED: enable LANGFUSE_FULL_PAYLOAD=1 to view content in Langfuse]"


def _redacting_mask(data: Any) -> Any:
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


def build_langfuse_handler(config: LangfuseConfig) -> tuple[Any | None, LangfuseStatus]:
    """Return ``(handler, status)``. Handler is ``None`` when disabled.

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
        from langfuse.callback import CallbackHandler
    except Exception as exc:  # pragma: no cover
        return None, LangfuseStatus(False, False, config.host, reason=f"import failed: {exc}")

    # auth_check — give the user a fast, friendly error if creds are bad.
    try:
        probe = Langfuse(public_key=public_key, secret_key=secret_key, host=config.host)
        if not probe.auth_check():
            return None, LangfuseStatus(
                False, False, config.host, reason="auth_check failed (invalid keys?)"
            )
    except Exception as exc:  # pragma: no cover
        _log.warning("langfuse auth_check raised: %s", exc)
        # Continue anyway — auth_check failure shouldn't necessarily break tracing.

    mask = None if config.upload_full_payload else _redacting_mask
    try:
        handler = CallbackHandler(
            public_key=public_key,
            secret_key=secret_key,
            host=config.host,
            mask=mask,
        )
    except Exception as exc:  # pragma: no cover
        return None, LangfuseStatus(False, False, config.host, reason=f"init failed: {exc}")

    return handler, LangfuseStatus(
        enabled=True,
        full_payload=config.upload_full_payload,
        host=config.host,
    )


def flush_handler(handler: Any | None) -> None:
    """Block until pending traces are sent. Safe to call with ``None``."""
    if handler is None:
        return
    try:
        # Langfuse's CallbackHandler exposes the underlying client at ``langfuse``.
        client = getattr(handler, "langfuse", None) or getattr(handler, "client", None)
        if client is not None and hasattr(client, "flush"):
            client.flush()
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
        return Langfuse(public_key=public_key, secret_key=secret_key, host=config.host)
    except Exception as exc:  # pragma: no cover
        _log.warning("langfuse client init failed: %s", exc)
        return None
