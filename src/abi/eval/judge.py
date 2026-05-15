"""LLM-as-judge.

For each sampled paragraph, runs two calls in parallel:

1. Likert: judge scores BOTH systems on adequacy/fluency/coherence/style.
2. Pairwise: judge picks the better translation (or "tie").

A/B labels are randomized per sample so the judge cannot tell which side is
ABI vs baseline. The mapping is stored on the :class:`JudgeSampleResult` so
we can de-anonymize during aggregation.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

from abi.eval._schemas import JudgeLikertOutput, JudgePairwiseOutput
from abi.prompts import get_registry
from abi.providers.llm.factory import LLMRouter, system_message, user_message
from abi.providers.observability.events import EventLogger
from abi.types.eval import (
    AlignedTriple,
    JudgeSampleResult,
    LikertScore,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JudgeContext:
    source_language: str
    target_language: str
    register: str
    judge_model_name: str


def _build_likert_prompt(
    triple: AlignedTriple,
    prev_source: str,
    next_source: str,
    text_a: str,
    text_b: str,
    ctx: JudgeContext,
) -> str:
    registry = get_registry()
    return registry.render(
        "eval_judge_likert",
        source_language=ctx.source_language,
        target_language=ctx.target_language,
        register=ctx.register,
        prev_source=prev_source,
        next_source=next_source,
        source_text=triple.source_text,
        text_a=text_a,
        text_b=text_b,
    )


def _build_pairwise_prompt(
    triple: AlignedTriple,
    prev_source: str,
    next_source: str,
    text_a: str,
    text_b: str,
    ctx: JudgeContext,
) -> str:
    registry = get_registry()
    return registry.render(
        "eval_judge_pairwise",
        source_language=ctx.source_language,
        target_language=ctx.target_language,
        register=ctx.register,
        prev_source=prev_source,
        next_source=next_source,
        source_text=triple.source_text,
        text_a=text_a,
        text_b=text_b,
    )


async def judge_sample(
    *,
    triple: AlignedTriple,
    prev_source: str,
    next_source: str,
    router: LLMRouter,
    events: EventLogger,
    ctx: JudgeContext,
    rng: random.Random,
) -> JudgeSampleResult | None:
    """Score one aligned triple. Returns ``None`` on hard LLM failure."""
    # Randomize which side is ABI vs baseline.
    abi_is_a = rng.random() < 0.5
    text_a = triple.abi_text if abi_is_a else triple.baseline_text
    text_b = triple.baseline_text if abi_is_a else triple.abi_text
    abi_label = "A" if abi_is_a else "B"

    likert_prompt = _build_likert_prompt(
        triple, prev_source, next_source, text_a, text_b, ctx
    )
    pairwise_prompt = _build_pairwise_prompt(
        triple, prev_source, next_source, text_a, text_b, ctx
    )

    registry = get_registry()
    judge_v_likert = registry.version_for("eval_judge_likert")
    judge_v_pair = registry.version_for("eval_judge_pairwise")

    t0 = time.perf_counter()
    try:
        likert_task = router.invoke_structured(
            JudgeLikertOutput,
            [
                system_message("You output strict JSON only. No prose, no markdown fences."),
                user_message(likert_prompt),
            ],
            agent_name="eval_judge_likert",
            prompt_version=judge_v_likert,
            metadata={"paragraph_id": triple.paragraph_id, "abi_label": abi_label},
        )
        pairwise_task = router.invoke_structured(
            JudgePairwiseOutput,
            [
                system_message("You output strict JSON only. No prose, no markdown fences."),
                user_message(pairwise_prompt),
            ],
            agent_name="eval_judge_pairwise",
            prompt_version=judge_v_pair,
            metadata={"paragraph_id": triple.paragraph_id, "abi_label": abi_label},
        )
        (likert_parsed, likert_resp), (pairwise_parsed, pairwise_resp) = await asyncio.gather(
            likert_task, pairwise_task
        )
    except Exception as exc:
        _log.warning("judge failed for %s: %s", triple.paragraph_id, exc)
        events.event(
            "eval.judge.failed",
            paragraph_id=triple.paragraph_id,
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return None

    latency_ms = int((time.perf_counter() - t0) * 1000)
    total_cost = likert_resp.cost_usd + pairwise_resp.cost_usd

    likert_abi = (
        _to_likert_score(likert_parsed.a) if abi_is_a else _to_likert_score(likert_parsed.b)
    )
    likert_baseline = (
        _to_likert_score(likert_parsed.b) if abi_is_a else _to_likert_score(likert_parsed.a)
    )

    events.event(
        "eval.judge.sample",
        paragraph_id=triple.paragraph_id,
        abi_label=abi_label,
        likert_abi_mean=round(likert_abi.mean(), 3),
        likert_baseline_mean=round(likert_baseline.mean(), 3),
        verdict=pairwise_parsed.verdict,
        latency_ms=latency_ms,
        cost_usd=round(total_cost, 6),
    )

    return JudgeSampleResult(
        paragraph_id=triple.paragraph_id,
        position=triple.position,
        section_id=triple.section_id,
        abi_label=abi_label,  # type: ignore[arg-type]
        likert_abi=likert_abi,
        likert_baseline=likert_baseline,
        pairwise_verdict=pairwise_parsed.verdict,
        pairwise_rationale=pairwise_parsed.rationale,
        judge_model=ctx.judge_model_name,
        judge_latency_ms=latency_ms,
        judge_cost_usd=total_cost,
    )


def _to_likert_score(side) -> LikertScore:
    return LikertScore(
        adequacy=float(side.adequacy),
        fluency=float(side.fluency),
        coherence=float(side.coherence),
        style=float(side.style),
        rationale=side.rationale,
    )
