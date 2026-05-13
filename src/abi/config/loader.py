"""Build a RunConfig by merging defaults < user config < project config < CLI overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from abi.types.run import RunConfig


def load_yaml_config(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config at {path} must be a YAML mapping")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_int(name: str) -> int | None:
    """Parse a non-negative int from env; warn-quietly on bad values."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        v = int(raw)
        return v if v >= 0 else None
    except ValueError:
        return None


def _env_overrides() -> dict[str, Any]:
    """Map well-known environment variables into config keys.

    Tuning knobs:
        ABI_WINDOW_BEFORE / ABI_WINDOW_AFTER : sliding-window paragraph counts
        ABI_BATCH_SIZE                       : paragraphs per LLM call (1 = legacy)
        ABI_CONCURRENCY                      : top-level task concurrency
    """
    out: dict[str, Any] = {}
    if base := os.environ.get("LLM_BASE_URL"):
        out.setdefault("llm", {})["base_url"] = base
    if model := os.environ.get("LLM_MODEL"):
        out.setdefault("llm", {})["model"] = model
    if host := os.environ.get("LANGFUSE_HOST"):
        out.setdefault("langfuse", {})["host"] = host
    if os.environ.get("LANGFUSE_FULL_PAYLOAD") == "1":
        out.setdefault("langfuse", {})["upload_full_payload"] = True

    if (n := _env_int("ABI_WINDOW_BEFORE")) is not None:
        out.setdefault("window", {})["before"] = n
    if (n := _env_int("ABI_WINDOW_AFTER")) is not None:
        out.setdefault("window", {})["after"] = n
    if (n := _env_int("ABI_BATCH_SIZE")) is not None and n >= 1:
        out["batch_size"] = n
    if (n := _env_int("ABI_CONCURRENCY")) is not None and n >= 1:
        out["concurrency"] = n
    if (flag := os.environ.get("ABI_TOC_REFINE")) is not None and flag != "":
        out["refine_toc"] = flag not in {"0", "false", "False", "no", "off"}
    return out


def build_run_config(
    user_config_path: Path | None = None,
    project_config_path: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> RunConfig:
    """Merge config layers and return a validated RunConfig."""
    user = load_yaml_config(user_config_path)
    project = load_yaml_config(project_config_path)
    env = _env_overrides()
    overrides = cli_overrides or {}

    merged: dict[str, Any] = {}
    for layer in (user, project, env, overrides):
        merged = _deep_merge(merged, layer)

    return RunConfig.model_validate(merged)


def default_user_config_path() -> Path:
    return Path(os.environ.get("ABI_HOME", Path.home() / ".abi")) / "config.yaml"


def default_project_config_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / "abi.yaml"
