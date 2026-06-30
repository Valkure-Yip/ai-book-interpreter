"""Build a RunConfig by merging defaults < user config < project config < CLI overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from abi.types.run import RunConfig


def load_dotenv(paths: list[Path] | None = None) -> list[Path]:
    """Load ``.env`` files into ``os.environ`` (without overriding existing vars).

    Looks at ``./.env`` and the repo-root ``.env`` by default. Minimal parser:
    ``KEY=VALUE`` lines, ``#`` comments, optional surrounding quotes. Returns the
    files that were loaded so the CLI can report them.
    """
    if paths is None:
        cwd = Path.cwd()
        paths = [cwd / ".env"]
        repo_env = Path(__file__).resolve().parents[3] / ".env"
        if repo_env not in paths:
            paths.append(repo_env)
    loaded: list[Path] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
        loaded.append(path)
    return loaded


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

    Endpoint:      LLM_BASE_URL, LLM_MODEL
    Observability: LANGFUSE_HOST, LANGFUSE_FULL_PAYLOAD
    Tuning:        ABI_MAX_CONCURRENCY, ABI_MAX_STAGE_ATTEMPTS
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

    if (n := _env_int("ABI_MAX_CONCURRENCY")) is not None and n >= 1:
        out.setdefault("llm", {})["max_concurrency"] = n
    if (n := _env_int("ABI_MAX_STAGE_ATTEMPTS")) is not None and n >= 1:
        out["max_stage_attempts"] = n
    if os.environ.get("ABI_TOC_REFINE") == "0":
        out["refine_toc"] = False
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
