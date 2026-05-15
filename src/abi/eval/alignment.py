"""Align baseline output paragraphs ↔ ABI source paragraphs.

The baseline is a flat blob of text. ABI's units are paragraph-keyed. To
score them side by side we need a paragraph-level mapping.

Strategy (in order):

1. **Positional**: if ``len(baseline) == len(source)``, just zip them.
2. **Soft**: if counts differ by ≤10%, do a length-weighted dynamic-programming
   alignment (Needleman–Wunsch style) over paragraph lengths. Cheap, no
   embeddings, robust to baseline merging/splitting a handful of paragraphs.
3. **Failed**: if the gap is larger, refuse to align and flag the whole
   baseline as ``align_failed``. Per-paragraph judge scores skip the
   baseline; only the document-level summary applies.

The output is a list of :class:`AlignedTriple` covering every source paragraph
in order. When alignment is missing for an index, ``aligned=False`` and
``baseline_text=""``.
"""

from __future__ import annotations

from abi.types.book import Book, Paragraph
from abi.types.eval import AlignedTriple, AlignmentReport
from abi.types.translation import TranslationUnit


def _abi_text_for(p: Paragraph, units: dict[str, TranslationUnit]) -> str:
    unit = units.get(p.paragraph_id)
    if unit is None:
        return ""
    return unit.translated_text.strip()


def _flat_source(book: Book) -> list[Paragraph]:
    """Return every prose paragraph in book order, skipping empty ones.

    Must match what :mod:`abi.eval.baseline` fed to the LLM, otherwise the
    positional alignment will silently misalign by one or more rows.
    """
    out: list[Paragraph] = []
    for p in book.iter_paragraphs():
        if p.source_text.strip():
            out.append(p)
    return out


def _soft_align(source_lens: list[int], baseline_lens: list[int]) -> list[int]:
    """Position-aware DP alignment for cross-language paragraphs.

    Returns ``mapping[i] = j`` where source paragraph ``i`` maps to baseline
    paragraph ``j``, or ``-1`` if no good match.

    We deliberately do NOT compare raw character lengths between source and
    baseline — en→zh translations are ~0.3× source length, so that signal
    is dominated by language, not by alignment quality. Instead we use:

    - **Length-ratio similarity**: each side's length normalized by its own
      total acts as a position estimate; matches that are near-diagonal are
      cheap. This works for ANY language pair because we never compare
      across languages.
    - **Drop / insert cost** = 1.0 (a full slot worth of pain).

    Net effect: when ``len(source) == len(baseline)``, we get the identity
    mapping. When baseline merged 2→1 or split 1→2 a few times, the DP
    routes around the gaps and recovers the rest of the alignment exactly.
    """
    n, m = len(source_lens), len(baseline_lens)
    if n == 0 or m == 0:
        return [-1] * n

    sum_src = max(sum(source_lens), 1)
    sum_base = max(sum(baseline_lens), 1)
    # Cumulative *fractional position* arrays in [0, 1].
    cum_src = [0.0]
    acc = 0
    for length in source_lens:
        acc += length
        cum_src.append(acc / sum_src)
    cum_base = [0.0]
    acc = 0
    for length in baseline_lens:
        acc += length
        cum_base.append(acc / sum_base)

    inf = float("inf")
    dp: list[list[float]] = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[str]] = [[""] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            best = inf
            op = ""
            if i > 0 and j > 0:
                # Position-similarity cost: how far apart in normalized
                # book coordinates the two paragraph midpoints are.
                mid_s = (cum_src[i - 1] + cum_src[i]) / 2
                mid_b = (cum_base[j - 1] + cum_base[j]) / 2
                match_cost = abs(mid_s - mid_b) * 2  # scale ~ [0, 2]
                if dp[i - 1][j - 1] + match_cost < best:
                    best = dp[i - 1][j - 1] + match_cost
                    op = "M"
            if i > 0 and dp[i - 1][j] + 1.0 < best:
                best = dp[i - 1][j] + 1.0
                op = "D"
            if j > 0 and dp[i][j - 1] + 1.0 < best:
                best = dp[i][j - 1] + 1.0
                op = "I"
            dp[i][j] = best
            back[i][j] = op

    mapping = [-1] * n
    i, j = n, m
    while i > 0 and j > 0:
        op = back[i][j]
        if op == "M":
            # Accept the match unless its position-distance is huge.
            mid_s = (cum_src[i - 1] + cum_src[i]) / 2
            mid_b = (cum_base[j - 1] + cum_base[j]) / 2
            if abs(mid_s - mid_b) * 2 < 0.5:
                mapping[i - 1] = j - 1
            i -= 1
            j -= 1
        elif op == "D":
            i -= 1
        elif op == "I":
            j -= 1
        else:
            break
    return mapping


def align(
    *,
    book: Book,
    units: dict[str, TranslationUnit],
    baseline_paragraphs: list[str],
    soft_threshold: float = 0.1,
) -> tuple[list[AlignedTriple], AlignmentReport]:
    """Align ABI source paragraphs with baseline output paragraphs.

    ``soft_threshold`` is the maximum allowed |Δlen| / len(source) before we
    fall back to "failed" mode.
    """
    source_paras = _flat_source(book)
    n_src = len(source_paras)
    n_base = len(baseline_paragraphs)

    if n_src == 0:
        return [], AlignmentReport(
            strategy="failed",
            source_paragraphs=0,
            abi_paragraphs=len(units),
            baseline_paragraphs=n_base,
            aligned_pairs=0,
            unaligned_pairs=0,
        )

    if n_src == n_base:
        strategy = "positional"
        mapping = list(range(n_src))
    elif abs(n_src - n_base) <= max(1, int(soft_threshold * n_src)):
        strategy = "soft"
        mapping = _soft_align(
            [len(p.source_text) for p in source_paras],
            [len(t) for t in baseline_paragraphs],
        )
    else:
        strategy = "failed"
        mapping = [-1] * n_src

    triples: list[AlignedTriple] = []
    aligned_count = 0
    for i, p in enumerate(source_paras):
        j = mapping[i] if i < len(mapping) else -1
        baseline_text = (
            baseline_paragraphs[j].strip()
            if 0 <= j < n_base
            else ""
        )
        aligned = baseline_text != ""
        if aligned:
            aligned_count += 1
        triples.append(
            AlignedTriple(
                paragraph_id=p.paragraph_id,
                position=p.position,
                section_id=p.section_id,
                heading_trail=_heading_trail_for(book, p.section_id),
                source_text=p.source_text,
                abi_text=_abi_text_for(p, units),
                baseline_text=baseline_text,
                aligned=aligned,
            )
        )

    report = AlignmentReport(
        strategy=strategy,  # type: ignore[arg-type]
        source_paragraphs=n_src,
        abi_paragraphs=len(units),
        baseline_paragraphs=n_base,
        aligned_pairs=aligned_count,
        unaligned_pairs=n_src - aligned_count,
    )
    return triples, report


def _heading_trail_for(book: Book, section_id: str) -> list[str]:
    for s in book.iter_sections():
        if s.section_id == section_id:
            return list(s.heading_trail)
    return []
