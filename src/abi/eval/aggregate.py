"""Aggregate per-sample judge results into a :class:`JudgeAggregate`.

Handles both the 2-way path (ABI vs Baseline only) and the 3-way path
(ABI / Baseline / Reference). Per-sample fields that aren't present
contribute nothing to their aggregate — e.g. ``likert_reference`` only
factors in when the underlying judge produced 3-way scores.
"""

from __future__ import annotations

from abi.types.eval import JudgeAggregate, JudgeSampleResult, LikertScore

_DIMENSIONS = ("adequacy", "fluency", "coherence", "style")


def _mean_dim(scores: list[LikertScore], dim: str) -> float:
    if not scores:
        return 0.0
    return round(sum(float(getattr(s, dim)) for s in scores) / len(scores), 3)


def _likert_block(scores: list[LikertScore]) -> dict[str, float]:
    if not scores:
        return {}
    out = {d: _mean_dim(scores, d) for d in _DIMENSIONS}
    out["mean"] = round(sum(out.values()) / len(_DIMENSIONS), 3)
    return out


def aggregate(results: list[JudgeSampleResult]) -> JudgeAggregate:
    """Compute mean Likert per dimension + pairwise win-rates.

    The 3-way fields stay zero when no sample has a reference.
    """
    if not results:
        return JudgeAggregate(samples=0)

    abi_scores = [r.likert_abi for r in results]
    base_scores = [r.likert_baseline for r in results]
    ref_scores = [r.likert_reference for r in results if r.likert_reference is not None]

    likert_abi = _likert_block(abi_scores)
    likert_baseline = _likert_block(base_scores)
    likert_reference = _likert_block(ref_scores)
    likert_delta = {
        d: round(likert_abi.get(d, 0.0) - likert_baseline.get(d, 0.0), 3)
        for d in (*_DIMENSIONS, "mean")
    }

    # ABI vs Baseline (legacy).
    abi_wins = sum(1 for r in results if r.pairwise_verdict == r.abi_label)
    base_wins = sum(
        1 for r in results if r.pairwise_verdict not in (r.abi_label, "tie")
    )
    ties = sum(1 for r in results if r.pairwise_verdict == "tie")
    n = len(results)
    abi_winrate = (abi_wins + 0.5 * ties) / n

    # ABI vs Reference. ``pairwise_abi_vs_ref`` is normalized so "A" = ABI side.
    ar_results = [r for r in results if r.pairwise_abi_vs_ref is not None]
    ar_n = len(ar_results)
    ar_abi_wins = sum(1 for r in ar_results if r.pairwise_abi_vs_ref == "A")
    ar_ref_wins = sum(1 for r in ar_results if r.pairwise_abi_vs_ref == "B")
    ar_ties = sum(1 for r in ar_results if r.pairwise_abi_vs_ref == "tie")
    ar_abi_winrate = (
        (ar_abi_wins + 0.5 * ar_ties) / ar_n if ar_n else 0.0
    )

    # Baseline vs Reference. ``pairwise_baseline_vs_ref`` has "A" = Baseline.
    br_results = [r for r in results if r.pairwise_baseline_vs_ref is not None]
    br_n = len(br_results)
    br_base_wins = sum(1 for r in br_results if r.pairwise_baseline_vs_ref == "A")
    br_ref_wins = sum(1 for r in br_results if r.pairwise_baseline_vs_ref == "B")
    br_ties = sum(1 for r in br_results if r.pairwise_baseline_vs_ref == "tie")
    br_base_winrate = (
        (br_base_wins + 0.5 * br_ties) / br_n if br_n else 0.0
    )

    return JudgeAggregate(
        samples=n,
        likert_abi=likert_abi,
        likert_baseline=likert_baseline,
        likert_reference=likert_reference,
        likert_delta=likert_delta,
        pairwise_abi_wins=abi_wins,
        pairwise_baseline_wins=base_wins,
        pairwise_ties=ties,
        pairwise_abi_winrate=round(abi_winrate, 3),
        pairwise_abi_vs_ref_wins=ar_abi_wins,
        pairwise_abi_vs_ref_losses=ar_ref_wins,
        pairwise_abi_vs_ref_ties=ar_ties,
        pairwise_abi_vs_ref_winrate=round(ar_abi_winrate, 3),
        pairwise_baseline_vs_ref_wins=br_base_wins,
        pairwise_baseline_vs_ref_losses=br_ref_wins,
        pairwise_baseline_vs_ref_ties=br_ties,
        pairwise_baseline_vs_ref_winrate=round(br_base_winrate, 3),
    )
