"""Paragraph splitting + source<->translation alignment (eval-standard.md §4.2).

The agentic pipeline emits plain-markdown translations (no runtime
``TranslationUnit``), so paragraph-level scoring must align source paragraphs to
translated paragraphs *after the fact*. Strategy:

1. equal length            -> position alignment
2. otherwise               -> Needleman-Wunsch on paragraph character lengths
3. low match rate          -> ``chapter_align_failed`` (caller falls back to
   chapter-level mechanical scoring, which is robust to paragraph merging/splitting)

Real translations routinely merge boilerplate or split long paragraphs, so a raw
count difference is *not* treated as failure: NW handles insertions/deletions via
gaps, and only a genuinely low match rate trips the chapter-level fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BLOCK_SPLIT_RE = re.compile(r"\n\s*\n")
_MIN_INFO_CHARS = 30


def split_paragraphs(md: str, *, drop_headings: bool = True) -> list[str]:
    """Split markdown into paragraph blocks, skipping headings + zero-info blocks."""
    out: list[str] = []
    for raw in _BLOCK_SPLIT_RE.split(md):
        block = raw.strip()
        if not block:
            continue
        if drop_headings and block.startswith("#"):
            continue
        out.append(block)
    return out


def is_low_info(text: str) -> bool:
    """Passthrough-ish blocks (too short / code / pure markup) skipped from scoring."""
    return len(text.strip()) < _MIN_INFO_CHARS


@dataclass(frozen=True)
class AlignedPair:
    index: int
    source: str
    target: str | None  # None when the source paragraph has no aligned target


@dataclass(frozen=True)
class Alignment:
    pairs: list[AlignedPair]
    chapter_align_failed: bool


def _nw_align(src: list[str], tgt: list[str]) -> list[int | None]:
    """Needleman-Wunsch on paragraph *lengths*. Returns, for each src index, the
    matched tgt index (or None for a gap). Score rewards similar lengths."""
    n, m = len(src), len(tgt)
    neg = float("-inf")
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    back = [[(0, 0)] * (m + 1) for _ in range(n + 1)]
    gap = -1.0
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + gap
        back[i][0] = (i - 1, 0)
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + gap
        back[0][j] = (0, j - 1)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            ls, lt = len(src[i - 1]), len(tgt[j - 1])
            sim = 1.0 - abs(ls - lt) / max(ls, lt, 1)  # 1.0 identical length, ->0 divergent
            diag = dp[i - 1][j - 1] + sim
            up = dp[i - 1][j] + gap
            left = dp[i][j - 1] + gap
            best = max(diag, up, left)
            dp[i][j] = best
            if best == diag:
                back[i][j] = (i - 1, j - 1)
            elif best == up:
                back[i][j] = (i - 1, j)
            else:
                back[i][j] = (i, j - 1)
            _ = neg
    matches: list[int | None] = [None] * n
    i, j = n, m
    while i > 0 or j > 0:
        pi, pj = back[i][j]
        if pi == i - 1 and pj == j - 1:
            matches[i - 1] = j - 1
        i, j = pi, pj
    return matches


# Below this fraction of source paragraphs matched, per-paragraph alignment is
# untrustworthy and the caller should score the chapter as a single unit.
_MIN_MATCH_RATE = 0.9


def align_paragraphs(source_md: str, target_md: str) -> Alignment:
    """Align source paragraphs to translated paragraphs (see module docstring)."""
    src = split_paragraphs(source_md)
    tgt = split_paragraphs(target_md)
    if not src:
        return Alignment(pairs=[], chapter_align_failed=False)
    if not tgt:
        pairs = [AlignedPair(i, s, None) for i, s in enumerate(src)]
        return Alignment(pairs=pairs, chapter_align_failed=True)

    if len(src) == len(tgt):
        pairs = [AlignedPair(i, s, t) for i, (s, t) in enumerate(zip(src, tgt, strict=True))]
        return Alignment(pairs=pairs, chapter_align_failed=False)

    matches = _nw_align(src, tgt)
    pairs = [
        AlignedPair(i, s, tgt[mi] if mi is not None else None)
        for i, (s, mi) in enumerate(zip(src, matches, strict=True))
    ]
    matched = sum(1 for m in matches if m is not None)
    match_rate = matched / len(src)
    return Alignment(pairs=pairs, chapter_align_failed=match_rate < _MIN_MATCH_RATE)
