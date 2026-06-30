"""RunConfig — runtime configuration for an agentic book run.

The old sliding-window / batch / output-mode knobs are gone; the agentic
pipeline's behaviour is driven by the staged prompts and deterministic gates.
What remains is endpoint + observability + cost + orchestration limits.
"""

from __future__ import annotations

from pydantic import Field

from abi.types._base import FrozenModel


class CostConfig(FrozenModel):
    hard_cap_usd: float | None = 50.0
    warn_at_usd: float | None = 10.0


class LLMConfig(FrozenModel):
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "LLM_API_KEY"
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_output_tokens: int = 8192
    request_timeout_s: int = 240
    max_concurrency: int = 4


class LangfuseConfig(FrozenModel):
    enabled: bool = True
    host: str = "https://cloud.langfuse.com"
    public_key_env: str = "LANGFUSE_PUBLIC_KEY"
    secret_key_env: str = "LANGFUSE_SECRET_KEY"
    upload_full_payload: bool = False


class RunConfig(FrozenModel):
    # Optional override; normally derived from the {source}-{target} template.
    target_language: str | None = None
    # Max agent retries per stage before the orchestrator marks the run blocked.
    max_stage_attempts: int = 3
    # Pass 0.5 LLM TOC refinement (default on). Disable with --no-refine-toc
    # or ABI_TOC_REFINE=0 for offline tests / known-good heuristic results.
    refine_toc: bool = True

    llm: LLMConfig = Field(default_factory=LLMConfig)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
