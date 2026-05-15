"""Render eval results as human-readable Markdown."""

from __future__ import annotations

from abi.types.eval import EvalReport


def render_markdown(r: EvalReport) -> str:
    out: list[str] = []
    add = out.append

    add(f"# Evaluation report — {r.book_id}")
    add("")
    add(f"- eval_id: `{r.eval_id}`")
    add(f"- abi_run_id: `{r.abi_run_id}`")
    add(f"- created_at: {r.created_at.isoformat()}")
    add(f"- judge model: `{r.judge_model}`")
    add(f"- samples: {r.judge.samples}")
    add(f"- source paragraphs: {r.source_paragraphs}")
    add("")

    add("## Alignment")
    a = r.alignment
    add(f"- strategy: **{a.strategy}**")
    add(f"- source paragraphs: {a.source_paragraphs}")
    add(f"- ABI paragraphs: {a.abi_paragraphs}")
    add(f"- baseline paragraphs: {a.baseline_paragraphs}")
    add(f"- aligned pairs: {a.aligned_pairs}")
    add(f"- unaligned: {a.unaligned_pairs}")
    add("")

    add("## Baseline generation")
    b = r.baseline
    add(f"- model: `{b.model}` @ {b.base_url}")
    add(f"- chunks: {b.chunks} (token budget per chunk = {b.chunk_token_budget})")
    add(f"- tokens in / out: {b.tokens_in:,} / {b.tokens_out:,}")
    add(f"- latency: {b.latency_ms / 1000:.1f}s")
    add(f"- cost: $ {b.cost_usd:.4f}")
    add("")

    add("## Mechanical metrics")
    add("")
    add("| metric | ABI | Baseline | Δ (abi - base) |")
    add("|---|---:|---:|---:|")
    abi = r.mechanical.abi
    base = r.mechanical.baseline
    rows = [
        ("glossary compliance", abi.glossary_compliance, base.glossary_compliance),
        ("length-ratio in band", abi.length_ratio_ok, base.length_ratio_ok),
        ("length-ratio mean", abi.length_ratio_mean, base.length_ratio_mean),
        ("anchor preservation", abi.anchor_preservation, base.anchor_preservation),
        ("completeness", abi.completeness, base.completeness),
    ]
    for label, x, y in rows:
        add(f"| {label} | {x:.3f} | {y:.3f} | {x - y:+.3f} |")
    add("")
    add(
        f"- glossary checked / violations — ABI: {abi.glossary_checked}/"
        f"{abi.glossary_violations}  baseline: {base.glossary_checked}/"
        f"{base.glossary_violations}"
    )
    add(
        f"- anchor checked paragraphs — ABI: {abi.anchor_checked}  "
        f"baseline: {base.anchor_checked}"
    )
    add(
        f"- completeness missing — ABI: {abi.completeness_missing}  "
        f"baseline: {base.completeness_missing}"
    )
    add("")

    add("## LLM-as-Judge — Likert (1-5, 5 = best)")
    add("")
    add("| dimension | ABI | Baseline | Δ |")
    add("|---|---:|---:|---:|")
    j = r.judge
    for dim in ("adequacy", "fluency", "coherence", "style", "mean"):
        a_v = j.likert_abi.get(dim, 0.0)
        b_v = j.likert_baseline.get(dim, 0.0)
        d_v = j.likert_delta.get(dim, 0.0)
        bold_open = "**" if dim == "mean" else ""
        bold_close = "**" if dim == "mean" else ""
        add(
            f"| {bold_open}{dim}{bold_close} | "
            f"{bold_open}{a_v:.2f}{bold_close} | "
            f"{bold_open}{b_v:.2f}{bold_close} | "
            f"{bold_open}{d_v:+.2f}{bold_close} |"
        )
    add("")

    add("## LLM-as-Judge — Pairwise preference")
    add("")
    add(f"- ABI wins: **{j.pairwise_abi_wins}**")
    add(f"- Baseline wins: {j.pairwise_baseline_wins}")
    add(f"- Ties: {j.pairwise_ties}")
    add(
        f"- ABI win-rate (ties = ½): **{j.pairwise_abi_winrate:.1%}** "
        f"of {j.samples} samples"
    )
    add("")

    add("## Interpretation")
    delta_mean = j.likert_delta.get("mean", 0.0)
    winrate = j.pairwise_abi_winrate
    verdict = _verdict(delta_mean, winrate)
    add(verdict)
    add("")

    if r.notes:
        add("## Notes")
        for n in r.notes:
            add(f"- {n}")
        add("")

    return "\n".join(out)


def _verdict(delta_mean: float, winrate: float) -> str:
    if winrate >= 0.6 and delta_mean >= 0.2:
        return (
            "ABI **clearly outperforms** the naive baseline on both Likert "
            "average and pairwise preference."
        )
    if winrate >= 0.55 or delta_mean >= 0.1:
        return (
            "ABI is **modestly better** than the naive baseline; the "
            "multi-pass machinery is paying for itself on this book."
        )
    if 0.45 <= winrate <= 0.55 and abs(delta_mean) < 0.1:
        return (
            "ABI and baseline are **statistically indistinguishable** at this "
            "sample size — the gains from survey/glossary/sliding-window are "
            "not visible to the judge on this corpus."
        )
    return (
        "Baseline is winning. Inspect samples and glossary handling — likely "
        "either the test corpus is too easy or ABI is over-constraining."
    )
