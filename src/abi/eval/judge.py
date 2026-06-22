"""L3 LLM-as-judge: Likert + pairwise comparison (eval-standard.md §5.3).

Judge calls go through ``LLMRouter.invoke_structured`` (Langfuse trace + budget
gate + structured pydantic output). Slots are randomised so the model never
learns which side is ABI; the slot->system mapping stays internal. Designed to
be unit-testable offline by injecting any object exposing ``invoke_structured``.
"""

from __future__ import annotations

import random
from typing import Any, Protocol

from pydantic import BaseModel, Field

from abi.eval.types import EvalTriple
from abi.providers.llm.factory import system_message, user_message

JUDGE_PROMPT_VERSION = "eval_judge/v1"


class SlotScore(BaseModel):
    """Likert 1-5 scores for one anonymised translation slot."""

    adequacy: int = Field(ge=1, le=5)
    fluency: int = Field(ge=1, le=5)
    coherence: int = Field(ge=1, le=5)
    style: int = Field(ge=1, le=5)


class LikertOutput(BaseModel):
    a: SlotScore
    b: SlotScore


class PairwiseOutput(BaseModel):
    prefer: str = Field(description="One of 'A', 'B', or 'tie'.")
    rationale: str = ""


class JudgeResult(BaseModel):
    paragraph_id: str
    abi: SlotScore
    baseline: SlotScore
    prefer_system: str  # "abi" | "baseline" | "tie"
    rationale: str


class _RouterLike(Protocol):
    async def invoke_structured(
        self,
        schema: type[Any],
        messages: list[Any],
        *,
        agent_name: str,
        prompt_version: str = ...,
        metadata: dict[str, Any] | None = ...,
        max_retries: int = ...,
        model_override: str | None = ...,
    ) -> tuple[Any, Any]: ...


_SYSTEM = (
    "You are a meticulous bilingual translation quality judge. You will see a "
    "source passage and two candidate translations labelled A and B. Score each "
    "on adequacy, fluency, coherence, and style (1-5). Be calibrated and critical; "
    "do not assume either side is machine or human. Output strict JSON only."
)


def _likert_prompt(triple: EvalTriple, a: str, b: str) -> list[Any]:
    ref = f"\n\nReference (context only, do not score): {triple.reference}" if triple.reference else ""
    body = (
        f"Source ({triple.source_lang}):\n{triple.source}{ref}\n\n"
        f"Translation A ({triple.target_lang}):\n{a}\n\n"
        f"Translation B ({triple.target_lang}):\n{b}\n\n"
        "Return JSON: {\"a\": {\"adequacy\":n,\"fluency\":n,\"coherence\":n,\"style\":n}, "
        "\"b\": {\"adequacy\":n,\"fluency\":n,\"coherence\":n,\"style\":n}}."
    )
    return [system_message(_SYSTEM), user_message(body)]


def _pairwise_prompt(triple: EvalTriple, a: str, b: str) -> list[Any]:
    body = (
        f"Source ({triple.source_lang}):\n{triple.source}\n\n"
        f"Translation A:\n{a}\n\nTranslation B:\n{b}\n\n"
        "Which translation is better overall? Return JSON: "
        "{\"prefer\": \"A\"|\"B\"|\"tie\", \"rationale\": \"...\"}."
    )
    return [system_message(_SYSTEM), user_message(body)]


async def judge_triple(
    router: _RouterLike,
    triple: EvalTriple,
    *,
    rng: random.Random,
    judge_model: str | None = None,
) -> JudgeResult | None:
    """Score ABI vs baseline for one triple. Returns None if a side is missing."""
    if not triple.abi or not triple.baseline:
        return None

    # Randomise which system sits in slot A vs B.
    abi_in_a = rng.random() < 0.5
    a_text = triple.abi if abi_in_a else triple.baseline
    b_text = triple.baseline if abi_in_a else triple.abi

    likert, _ = await router.invoke_structured(
        LikertOutput,
        _likert_prompt(triple, a_text, b_text),
        agent_name="eval_judge_likert",
        prompt_version=JUDGE_PROMPT_VERSION,
        metadata={"paragraph_id": triple.paragraph_id},
        model_override=judge_model,
    )
    pairwise, _ = await router.invoke_structured(
        PairwiseOutput,
        _pairwise_prompt(triple, a_text, b_text),
        agent_name="eval_judge_pairwise",
        prompt_version=JUDGE_PROMPT_VERSION,
        metadata={"paragraph_id": triple.paragraph_id},
        model_override=judge_model,
    )

    abi_score = likert.a if abi_in_a else likert.b
    baseline_score = likert.b if abi_in_a else likert.a
    prefer = pairwise.prefer.strip().upper()
    if prefer == "A":
        prefer_system = "abi" if abi_in_a else "baseline"
    elif prefer == "B":
        prefer_system = "baseline" if abi_in_a else "abi"
    else:
        prefer_system = "tie"

    return JudgeResult(
        paragraph_id=triple.paragraph_id,
        abi=abi_score,
        baseline=baseline_score,
        prefer_system=prefer_system,
        rationale=pairwise.rationale[:500],
    )
