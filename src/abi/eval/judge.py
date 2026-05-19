"""LLM-as-judge.

Two paths, picked per sample based on ``triple.has_reference``:

- **2-way** (legacy, no reference): one Likert call scoring A and B + one
  pairwise call picking A/B/tie. ABI vs Baseline.
- **3-way** (dataset-driven): one Likert call scoring A, B, C + one pairwise
  call emitting three verdicts (a_vs_b, a_vs_c, b_vs_c).

A/B/C labels are randomized per sample so the judge cannot tell which side
is ABI vs Baseline vs Reference. The mapping is stored on
:class:`JudgeSampleResult.label_mapping` for mechanical de-anonymization.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass

from abi.eval._schemas import (
    JudgeLikert3Output,
    JudgeLikertOutput,
    JudgePairwise3Output,
    JudgePairwiseOutput,
)
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


def _build_likert_3way_prompt(
    triple: AlignedTriple,
    prev_source: str,
    next_source: str,
    text_a: str,
    text_b: str,
    text_c: str,
    ctx: JudgeContext,
) -> str:
    registry = get_registry()
    return registry.render(
        "eval_judge_likert_3way",
        source_language=ctx.source_language,
        target_language=ctx.target_language,
        register=ctx.register,
        prev_source=prev_source,
        next_source=next_source,
        source_text=triple.source_text,
        text_a=text_a,
        text_b=text_b,
        text_c=text_c,
    )


def _build_pairwise_3way_prompt(
    triple: AlignedTriple,
    prev_source: str,
    next_source: str,
    text_a: str,
    text_b: str,
    text_c: str,
    ctx: JudgeContext,
) -> str:
    registry = get_registry()
    return registry.render(
        "eval_judge_pairwise_3way",
        source_language=ctx.source_language,
        target_language=ctx.target_language,
        register=ctx.register,
        prev_source=prev_source,
        next_source=next_source,
        source_text=triple.source_text,
        text_a=text_a,
        text_b=text_b,
        text_c=text_c,
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
    model_override: str | None = None,
) -> JudgeSampleResult | None:
    """Score one aligned triple. Returns ``None`` on hard LLM failure.

    Routes through the 3-way path when ``triple.has_reference`` is set.
    ``model_override``, if given, is passed through to the router as a
    per-call model swap so the judge can use a different model than the
    translation pipeline.
    """
    if triple.has_reference and triple.reference_text:
        return await _judge_3way(
            triple=triple,
            prev_source=prev_source,
            next_source=next_source,
            router=router,
            events=events,
            ctx=ctx,
            rng=rng,
            model_override=model_override,
        )
    return await _judge_2way(
        triple=triple,
        prev_source=prev_source,
        next_source=next_source,
        router=router,
        events=events,
        ctx=ctx,
        rng=rng,
        model_override=model_override,
    )


async def _judge_2way(
    *,
    triple: AlignedTriple,
    prev_source: str,
    next_source: str,
    router: LLMRouter,
    events: EventLogger,
    ctx: JudgeContext,
    rng: random.Random,
    model_override: str | None,
) -> JudgeSampleResult | None:
    abi_is_a = rng.random() < 0.5
    text_a = triple.abi_text if abi_is_a else triple.baseline_text
    text_b = triple.baseline_text if abi_is_a else triple.abi_text
    abi_label = "A" if abi_is_a else "B"
    label_mapping = {
        abi_label: "abi",
        ("B" if abi_is_a else "A"): "baseline",
    }

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
            model_override=model_override,
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
            model_override=model_override,
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
        abi_label=abi_label,
        likert_abi=likert_abi,
        likert_baseline=likert_baseline,
        pairwise_verdict=pairwise_parsed.verdict,
        pairwise_rationale=pairwise_parsed.rationale,
        label_mapping=label_mapping,
        judge_model=ctx.judge_model_name,
        judge_latency_ms=latency_ms,
        judge_cost_usd=total_cost,
    )


async def _judge_3way(
    *,
    triple: AlignedTriple,
    prev_source: str,
    next_source: str,
    router: LLMRouter,
    events: EventLogger,
    ctx: JudgeContext,
    rng: random.Random,
    model_override: str | None,
) -> JudgeSampleResult | None:
    # Randomize a permutation of {abi, baseline, reference} onto slots {A, B, C}.
    systems = ["abi", "baseline", "reference"]
    rng.shuffle(systems)
    slot_to_system = dict(zip(("A", "B", "C"), systems, strict=True))
    system_to_slot = {v: k for k, v in slot_to_system.items()}
    texts = {
        "abi": triple.abi_text,
        "baseline": triple.baseline_text,
        "reference": triple.reference_text,
    }
    text_a = texts[slot_to_system["A"]]
    text_b = texts[slot_to_system["B"]]
    text_c = texts[slot_to_system["C"]]
    abi_label = system_to_slot["abi"]

    likert_prompt = _build_likert_3way_prompt(
        triple, prev_source, next_source, text_a, text_b, text_c, ctx
    )
    pairwise_prompt = _build_pairwise_3way_prompt(
        triple, prev_source, next_source, text_a, text_b, text_c, ctx
    )

    registry = get_registry()
    judge_v_likert = registry.version_for("eval_judge_likert_3way")
    judge_v_pair = registry.version_for("eval_judge_pairwise_3way")

    t0 = time.perf_counter()
    try:
        likert_task = router.invoke_structured(
            JudgeLikert3Output,
            [
                system_message("You output strict JSON only. No prose, no markdown fences."),
                user_message(likert_prompt),
            ],
            agent_name="eval_judge_likert_3way",
            prompt_version=judge_v_likert,
            metadata={"paragraph_id": triple.paragraph_id, "abi_label": abi_label},
            model_override=model_override,
        )
        pairwise_task = router.invoke_structured(
            JudgePairwise3Output,
            [
                system_message("You output strict JSON only. No prose, no markdown fences."),
                user_message(pairwise_prompt),
            ],
            agent_name="eval_judge_pairwise_3way",
            prompt_version=judge_v_pair,
            metadata={"paragraph_id": triple.paragraph_id, "abi_label": abi_label},
            model_override=model_override,
        )
        (likert_parsed, likert_resp), (pairwise_parsed, pairwise_resp) = await asyncio.gather(
            likert_task, pairwise_task
        )
    except Exception as exc:
        _log.warning("judge (3-way) failed for %s: %s", triple.paragraph_id, exc)
        events.event(
            "eval.judge.failed",
            paragraph_id=triple.paragraph_id,
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return None

    latency_ms = int((time.perf_counter() - t0) * 1000)
    total_cost = likert_resp.cost_usd + pairwise_resp.cost_usd

    slot_to_likert = {
        "A": _to_likert_score(likert_parsed.a),
        "B": _to_likert_score(likert_parsed.b),
        "C": _to_likert_score(likert_parsed.c),
    }
    likert_abi = slot_to_likert[system_to_slot["abi"]]
    likert_baseline = slot_to_likert[system_to_slot["baseline"]]
    likert_reference = slot_to_likert[system_to_slot["reference"]]

    # De-anonymize the three slot-level pairwise verdicts onto system-pairs.
    # Each returns the WINNING system name ("abi" | "baseline" | "reference" | "tie").
    winner_ab = _decode_verdict_pair(
        pairwise_parsed,
        system_to_slot["abi"], system_to_slot["baseline"],
        slot_to_system,
    )
    winner_ar = _decode_verdict_pair(
        pairwise_parsed,
        system_to_slot["abi"], system_to_slot["reference"],
        slot_to_system,
    )
    winner_br = _decode_verdict_pair(
        pairwise_parsed,
        system_to_slot["baseline"], system_to_slot["reference"],
        slot_to_system,
    )

    events.event(
        "eval.judge.sample",
        paragraph_id=triple.paragraph_id,
        abi_label=abi_label,
        likert_abi_mean=round(likert_abi.mean(), 3),
        likert_baseline_mean=round(likert_baseline.mean(), 3),
        likert_reference_mean=round(likert_reference.mean(), 3),
        verdict_abi_vs_baseline=winner_ab,
        verdict_abi_vs_ref=winner_ar,
        verdict_baseline_vs_ref=winner_br,
        latency_ms=latency_ms,
        cost_usd=round(total_cost, 6),
    )

    # Normalize each verdict into {"A" = left side, "B" = right side, "tie"}.
    # ``pairwise_verdict`` keeps the legacy meaning: ABI vs Baseline, where
    # "A" means ABI won and "B" means Baseline won.
    verdict_ab = "tie" if winner_ab == "tie" else ("A" if winner_ab == "abi" else "B")
    verdict_ar = "tie" if winner_ar == "tie" else ("A" if winner_ar == "abi" else "B")
    verdict_br = "tie" if winner_br == "tie" else ("A" if winner_br == "baseline" else "B")

    return JudgeSampleResult(
        paragraph_id=triple.paragraph_id,
        position=triple.position,
        section_id=triple.section_id,
        abi_label=abi_label,
        likert_abi=likert_abi,
        likert_baseline=likert_baseline,
        likert_reference=likert_reference,
        pairwise_verdict=verdict_ab,
        pairwise_rationale=pairwise_parsed.rationale,
        pairwise_abi_vs_ref=verdict_ar,
        pairwise_baseline_vs_ref=verdict_br,
        label_mapping={
            "A": slot_to_system["A"],
            "B": slot_to_system["B"],
            "C": slot_to_system["C"],
        },
        judge_model=ctx.judge_model_name,
        judge_latency_ms=latency_ms,
        judge_cost_usd=total_cost,
    )


def _decode_verdict_pair(
    parsed: JudgePairwise3Output,
    slot_left: str,
    slot_right: str,
    slot_to_system: dict[str, str],
) -> str:
    """Resolve the LLM's pairwise verdict for ``(slot_left, slot_right)`` to a
    system-name (``"abi"`` / ``"baseline"`` / ``"reference"``) or ``"tie"``.

    The prompt produces three fixed verdicts keyed by canonical slot order
    (A < B < C). We look up whichever of the three covers the requested
    pair regardless of orientation, then translate the winning slot back to
    the underlying system via ``slot_to_system``. Out-of-vocab letters fall
    back to ``"tie"``.
    """
    pair_to_verdict = {
        ("A", "B"): parsed.a_vs_b,
        ("A", "C"): parsed.a_vs_c,
        ("B", "C"): parsed.b_vs_c,
    }
    if (slot_left, slot_right) in pair_to_verdict:
        raw = pair_to_verdict[(slot_left, slot_right)]
    elif (slot_right, slot_left) in pair_to_verdict:
        raw = pair_to_verdict[(slot_right, slot_left)]
    else:
        return "tie"
    raw_str = str(raw)
    if raw_str == "tie":
        return "tie"
    if raw_str in (slot_left, slot_right):
        return slot_to_system.get(raw_str, "tie")
    return "tie"


def _to_likert_score(side) -> LikertScore:  # type: ignore[no-untyped-def]
    return LikertScore(
        adequacy=float(side.adequacy),
        fluency=float(side.fluency),
        coherence=float(side.coherence),
        style=float(side.style),
        rationale=side.rationale,
    )
