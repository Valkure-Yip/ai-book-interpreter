"""RunManifest + RunConfig — describe what a single execution does."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from abi.types._base import FrozenModel

OutputMode = Literal["translated", "bilingual", "annotated", "survey-only"]
QualityPreset = Literal["fast", "standard", "high"]


class WindowConfig(FrozenModel):
    before: int = 3
    after: int = 2
    glossary_max: int = 40
    token_budget: int = 6000
    chapter_abstract_max_chars: int = 600


class StyleConfig(FrozenModel):
    quote_style: str = "「」"
    punctuation: Literal["full", "half", "preserve"] = "full"
    register_override: str | None = None


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
    structured_output_strategy: Literal[
        "auto", "json_schema", "tool_calling", "json_mode", "prompt_only"
    ] = "auto"


class LangfuseConfig(FrozenModel):
    enabled: bool = True
    host: str = "https://cloud.langfuse.com"
    public_key_env: str = "LANGFUSE_PUBLIC_KEY"
    secret_key_env: str = "LANGFUSE_SECRET_KEY"
    upload_full_payload: bool = False


class RunConfig(FrozenModel):
    target_language: str = "zh"
    source_language: str | None = None
    modes: list[OutputMode] = Field(default_factory=lambda: ["translated"])
    quality: QualityPreset = "standard"
    concurrency: int = 4
    max_revision_rounds: int = 2
    max_retries: int = 3
    dry_run: bool = False
    force_rerun: bool = False
    # When True (default), run the LLM-based TOC refiner between Pass 0 (ingest)
    # and Pass 1 (survey). The refiner asks the model to identify the book's
    # real chapter/section structure, replacing the heuristic ingest output.
    # Disable with ``--no-refine-toc`` / ``ABI_TOC_REFINE=0`` for offline tests
    # or to save one LLM call when the heuristic structure is already correct.
    refine_toc: bool = True
    # (refine_toc declared above near force_rerun)
    # Pass 2 paragraphs-per-LLM-call. 1 = one paragraph per request (legacy);
    # >1 = pack K consecutive paragraphs into one prompt and parse a JSON array
    # of K translations. Larger values reduce wall time but lose intra-batch
    # sliding-window locality (translations are produced in parallel within
    # the batch instead of sequentially). Override via ``ABI_BATCH_SIZE``.
    batch_size: int = 1

    llm: LLMConfig = Field(default_factory=LLMConfig)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    window: WindowConfig = Field(default_factory=WindowConfig)
    style: StyleConfig = Field(default_factory=StyleConfig)
    cost: CostConfig = Field(default_factory=CostConfig)


class RunManifest(FrozenModel):
    run_id: str
    book_id: str
    created_at: datetime
    config: RunConfig
    pipeline_versions: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    capabilities: dict[str, str] = Field(default_factory=dict)
    git_sha: str = ""
