"""Render eval results as human-readable Markdown.

Two render paths sharing the same skeleton:

- ``2-way`` (no human reference): ABI vs Baseline only.
- ``3-way`` (dataset with references): adds Reference columns to the
  mechanical table, a third Likert row, and two extra pairwise sections.
"""

from __future__ import annotations

from abi.types.eval import EvalReport, MechanicalScore


def render_markdown(r: EvalReport) -> str:
    out: list[str] = []
    add = out.append
    has_ref = r.mechanical.reference is not None and r.reference_paragraphs > 0

    add(f"# Evaluation report — {r.book_id}")
    add("")
    add(f"- eval_id: `{r.eval_id}`")
    add(f"- abi_run_id: `{r.abi_run_id}`")
    add(f"- created_at: {r.created_at.isoformat()}")
    add(f"- translate model: `{r.translate_model or '?'}`")
    add(f"- judge model: `{r.judge_model}`")
    if r.dataset_spec:
        add(f"- dataset: `{r.dataset_spec}`")
    if r.langfuse_dataset_run_url:
        add(f"- langfuse run: <{r.langfuse_dataset_run_url}>")
    elif r.langfuse_dataset_name:
        add(f"- langfuse dataset: `{r.langfuse_dataset_name}`")
    add(f"- samples: {r.judge.samples}")
    add(f"- source paragraphs: {r.source_paragraphs}")
    if has_ref:
        add(f"- reference paragraphs: {r.reference_paragraphs}")
    add("")

    add("## Alignment")
    a = r.alignment
    add(f"- strategy: **{a.strategy}**")
    add(f"- source paragraphs: {a.source_paragraphs}")
    add(f"- ABI paragraphs: {a.abi_paragraphs}")
    add(f"- baseline paragraphs: {a.baseline_paragraphs}")
    if has_ref:
        add(f"- reference paragraphs: {a.reference_paragraphs}")
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
    if has_ref:
        ref: MechanicalScore = r.mechanical.reference  # type: ignore[assignment]
        add("| metric | ABI | Baseline | Reference | Δ(abi - base) | Δ(abi - ref) |")
        add("|---|---:|---:|---:|---:|---:|")
        abi = r.mechanical.abi
        base = r.mechanical.baseline
        rows = [
            ("glossary compliance", abi.glossary_compliance, base.glossary_compliance, ref.glossary_compliance),
            ("length-ratio in band", abi.length_ratio_ok, base.length_ratio_ok, ref.length_ratio_ok),
            ("length-ratio mean", abi.length_ratio_mean, base.length_ratio_mean, ref.length_ratio_mean),
            ("anchor preservation", abi.anchor_preservation, base.anchor_preservation, ref.anchor_preservation),
            ("completeness", abi.completeness, base.completeness, ref.completeness),
        ]
        for label, x, y, z in rows:
            add(
                f"| {label} | {x:.3f} | {y:.3f} | {z:.3f} | "
                f"{x - y:+.3f} | {x - z:+.3f} |"
            )
    else:
        add("| metric | ABI | Baseline | Δ (abi - base) |")
        add("|---|---:|---:|---:|")
        abi = r.mechanical.abi
        base = r.mechanical.baseline
        rows2 = [
            ("glossary compliance", abi.glossary_compliance, base.glossary_compliance),
            ("length-ratio in band", abi.length_ratio_ok, base.length_ratio_ok),
            ("length-ratio mean", abi.length_ratio_mean, base.length_ratio_mean),
            ("anchor preservation", abi.anchor_preservation, base.anchor_preservation),
            ("completeness", abi.completeness, base.completeness),
        ]
        for label, x, y in rows2:
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
    j = r.judge
    if has_ref:
        add("| dimension | ABI | Baseline | Reference | Δ(abi - base) |")
        add("|---|---:|---:|---:|---:|")
        for dim in ("adequacy", "fluency", "coherence", "style", "mean"):
            a_v = j.likert_abi.get(dim, 0.0)
            b_v = j.likert_baseline.get(dim, 0.0)
            ref_v = j.likert_reference.get(dim, 0.0)
            d_v = j.likert_delta.get(dim, 0.0)
            bold = "**" if dim == "mean" else ""
            add(
                f"| {bold}{dim}{bold} | "
                f"{bold}{a_v:.2f}{bold} | "
                f"{bold}{b_v:.2f}{bold} | "
                f"{bold}{ref_v:.2f}{bold} | "
                f"{bold}{d_v:+.2f}{bold} |"
            )
    else:
        add("| dimension | ABI | Baseline | Δ |")
        add("|---|---:|---:|---:|")
        for dim in ("adequacy", "fluency", "coherence", "style", "mean"):
            a_v = j.likert_abi.get(dim, 0.0)
            b_v = j.likert_baseline.get(dim, 0.0)
            d_v = j.likert_delta.get(dim, 0.0)
            bold = "**" if dim == "mean" else ""
            add(
                f"| {bold}{dim}{bold} | "
                f"{bold}{a_v:.2f}{bold} | "
                f"{bold}{b_v:.2f}{bold} | "
                f"{bold}{d_v:+.2f}{bold} |"
            )
    add("")

    add("## LLM-as-Judge — Pairwise preference")
    add("")
    add("### ABI vs Baseline")
    add(f"- ABI wins: **{j.pairwise_abi_wins}**")
    add(f"- Baseline wins: {j.pairwise_baseline_wins}")
    add(f"- Ties: {j.pairwise_ties}")
    add(
        f"- ABI win-rate (ties = ½): **{j.pairwise_abi_winrate:.1%}** "
        f"of {j.samples} samples"
    )
    add("")
    if has_ref:
        add("### ABI vs Reference")
        add(f"- ABI wins: {j.pairwise_abi_vs_ref_wins}")
        add(f"- Reference wins: {j.pairwise_abi_vs_ref_losses}")
        add(f"- Ties: {j.pairwise_abi_vs_ref_ties}")
        add(
            f"- ABI win-rate vs reference (ties = ½): "
            f"**{j.pairwise_abi_vs_ref_winrate:.1%}**"
        )
        add("")
        add("### Baseline vs Reference")
        add(f"- Baseline wins: {j.pairwise_baseline_vs_ref_wins}")
        add(f"- Reference wins: {j.pairwise_baseline_vs_ref_losses}")
        add(f"- Ties: {j.pairwise_baseline_vs_ref_ties}")
        add(
            f"- Baseline win-rate vs reference (ties = ½): "
            f"**{j.pairwise_baseline_vs_ref_winrate:.1%}**"
        )
        add("")

    add("## Interpretation")
    delta_mean = j.likert_delta.get("mean", 0.0)
    winrate = j.pairwise_abi_winrate
    verdict = _verdict(delta_mean, winrate)
    add(verdict)
    if has_ref:
        add("")
        add(_reference_verdict(j.pairwise_abi_vs_ref_winrate, j.pairwise_baseline_vs_ref_winrate))
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


def _reference_verdict(abi_vs_ref: float, base_vs_ref: float) -> str:
    """Comment on the ABI / Baseline distance to the human reference."""
    closeness_abi = abi_vs_ref  # winrate vs reference. 0.5 = parity.
    closeness_base = base_vs_ref
    if closeness_abi >= 0.45:
        ref_phrase = (
            "ABI reaches **near-parity with the human reference** on this "
            "subset — the judge can't reliably tell them apart."
        )
    elif closeness_abi >= 0.35:
        ref_phrase = (
            "ABI is **within striking distance** of the human reference; "
            "the gap is visible but narrow."
        )
    elif closeness_abi >= 0.20:
        ref_phrase = (
            "The human reference still **clearly outperforms ABI**. There "
            "is real headroom in adequacy / style for the multi-pass pipeline."
        )
    else:
        ref_phrase = (
            "ABI is **far below** the human reference on this subset. "
            "Inspect prompts, glossary, and rendering of literary devices."
        )

    if closeness_base >= closeness_abi + 0.05:
        ref_phrase += (
            " Note: baseline is currently *closer* to the reference than ABI "
            "— this is a regression signal worth investigating."
        )
    return ref_phrase
