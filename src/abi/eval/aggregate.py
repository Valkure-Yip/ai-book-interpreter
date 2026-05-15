"""Aggregate per-sample judge results into a :class:`JudgeAggregate`."""

from __future__ import annotations

from abi.types.eval import JudgeAggregate, JudgeSampleResult, LikertScore

_DIMENSIONS = ("adequacy", "fluency", "coherence", "style")


def _mean_dim(scores: list[LikertScore], dim: str) -> float:
    if not scores:
        return 0.0
    return round(sum(getattr(s, dim) for s in scores) / len(scores), 3)


def aggregate(results: list[JudgeSampleResult]) -> JudgeAggregate:
    """Compute mean Likert per dimension + pairwise win-rate."""
    if not results:
        return JudgeAggregate(samples=0)

    abi_scores = [r.likert_abi for r in results]
    base_scores = [r.likert_baseline for r in results]

    likert_abi = {d: _mean_dim(abi_scores, d) for d in _DIMENSIONS}
    likert_baseline = {d: _mean_dim(base_scores, d) for d in _DIMENSIONS}
    likert_delta = {
        d: round(likert_abi[d] - likert_baseline[d], 3) for d in _DIMENSIONS
    }
    likert_abi["mean"] = round(sum(likert_abi.values()) / len(_DIMENSIONS), 3)
    likert_baseline["mean"] = round(
        sum(likert_baseline.values()) / len(_DIMENSIONS), 3
    )
    likert_delta["mean"] = round(likert_abi["mean"] - likert_baseline["mean"], 3)

    abi_wins = sum(1 for r in results if r.pairwise_verdict == r.abi_label)
    base_wins = sum(
        1 for r in results if r.pairwise_verdict not in (r.abi_label, "tie")
    )
    ties = sum(1 for r in results if r.pairwise_verdict == "tie")

    # Win-rate counts ties as half-wins on each side (standard chess scoring),
    # giving a single number in [0, 1].
    n = len(results)
    abi_winrate = (abi_wins + 0.5 * ties) / n

    return JudgeAggregate(
        samples=n,
        likert_abi=likert_abi,
        likert_baseline=likert_baseline,
        likert_delta=likert_delta,
        pairwise_abi_wins=abi_wins,
        pairwise_baseline_wins=base_wins,
        pairwise_ties=ties,
        pairwise_abi_winrate=round(abi_winrate, 3),
    )
