"""``Helsinki-NLP/news_commentary`` adapter.

OPUS News-Commentary is a Project Syndicate-style parallel corpus of expert
op-eds (economists, political scientists, policy researchers, etc.) translated
into many languages. The ``en-zh`` config has **69,206 sentence pairs**.

This is the closest thing on the Hub to an *academic-accessible* register
parallel corpus for English↔Chinese:

  - The source authors are subject-matter experts writing for educated
    laypersons — the register matches ABI's ``academic-accessible``.
  - Each row already has a stable ``id`` and the rows are in document order.

The dataset's one weakness is that it **lost the document boundaries** when
OPUS flattened the original Project Syndicate articles into a single split.
We re-derive them with a deliberately simple, eyeballed heuristic that lights
up on article-title rows:

    A row R is a TITLE (= start of a new document) iff
        len(R.en) < title_max_chars  (default 90)
        AND len(R+1.en) >= body_min_chars  (default 150)

Validated against 4 windows of 50 rows each spread across the corpus: every
detection is a real Project-Syndicate article title (``"1929 or 1989?"``,
``"The Scoundrels of Economic Patriotism"``, etc.) with no false positives.
False negatives merge two consecutive articles, which is mild — the eval
sampler may pull cross-document context for those few merged docs.

Spec format:

    news_commentary:en-zh:academic-accessible

Options accepted via ``:k=v`` suffix:

    limit_docs=N         keep only the first N inferred documents
    stub=true            return a tiny offline corpus (used by unit tests)
    title_max_chars=90   override the title length threshold
    body_min_chars=150   override the body length threshold
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections import defaultdict
from typing import Any

from abi.eval.datasets._base import (
    DatasetParagraph,
    DatasetSpec,
    EvalDataset,
    compute_book_id,
    register_adapter,
)

_log = logging.getLogger(__name__)

_HF_NAME = "Helsinki-NLP/news_commentary"

# Defaults validated against a 200-row sample (see adapter docstring).
_DEFAULT_TITLE_MAX = 90
_DEFAULT_BODY_MIN = 150


def _stable_paragraph_id(document_id: str, segment_id: str) -> str:
    """Deterministic paragraph_id from (document_id, segment_id).

    ``segment_id`` here is the upstream ``row.id`` (globally unique already)
    but we still include ``document_id`` so the hash stays consistent with
    ``wmt24pp.py`` and so re-grouping documents (e.g. tweaking the title
    heuristic) doesn't reshuffle Langfuse dataset items en masse.
    """
    return hashlib.sha1(
        f"news_commentary:{document_id}:{segment_id}".encode(),
        usedforsecurity=False,
    ).hexdigest()[:16]


def _detect_doc_boundaries(
    rows: list[dict[str, Any]],
    *,
    title_max_chars: int,
    body_min_chars: int,
) -> list[int]:
    """Return indices of rows that look like document titles.

    A title row marks the start of a new document. Each document spans
    ``[title_idx, next_title_idx)`` (or to the end of the corpus for the
    last document). If no title is detected before the first body row,
    a synthetic boundary at 0 is inserted so every row belongs to some
    document.
    """
    boundaries: list[int] = []
    for i, r in enumerate(rows):
        en = (r.get("translation") or {}).get("en") or ""
        en = en.strip()
        if not en or len(en) >= title_max_chars:
            continue
        next_en = (
            (rows[i + 1].get("translation") or {}).get("en") or ""
            if i + 1 < len(rows)
            else ""
        )
        if len(next_en.strip()) >= body_min_chars:
            boundaries.append(i)
    if not boundaries or boundaries[0] != 0:
        # Whatever rows precede the first title are an unnamed pre-title doc.
        boundaries.insert(0, 0)
    return boundaries


def _make_doc_id(title_row: dict[str, Any], idx: int) -> str:
    """Build a stable document id from a title row's English text.

    Falls back to a hash of the row index if the title is empty (the synthetic
    boundary inserted at position 0 when the corpus doesn't start on a title).
    """
    en = ((title_row.get("translation") or {}).get("en") or "").strip()
    if not en:
        return f"news_commentary_doc_{idx:06d}"
    slug = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in en)
    slug = slug.strip("_")[:60] or f"doc_{idx:06d}"
    return slug


def _build_dataset(
    spec: DatasetSpec,
    rows: list[dict[str, Any]],
) -> EvalDataset:
    """Group rows into documents and project them into an :class:`EvalDataset`."""
    target_lang = spec.language_pair.split("-")[1] if "-" in spec.language_pair else ""
    source_lang = (
        spec.language_pair.split("-")[0]
        if "-" in spec.language_pair
        else spec.language_pair
    )

    title_max = int(spec.options.get("title_max_chars", _DEFAULT_TITLE_MAX))
    body_min = int(spec.options.get("body_min_chars", _DEFAULT_BODY_MIN))
    if title_max < 1 or body_min < 1:
        raise ValueError("title_max_chars and body_min_chars must be positive")

    # Keep only rows with non-empty translation pairs in this language pair.
    filtered: list[dict[str, Any]] = []
    for r in rows:
        tr = r.get("translation") or {}
        if not (tr.get(source_lang) or "").strip():
            continue
        if not (tr.get(target_lang) or "").strip():
            continue
        filtered.append(r)

    boundaries = _detect_doc_boundaries(
        filtered, title_max_chars=title_max, body_min_chars=body_min
    )

    # Pair boundary i with the next boundary (or the end) to get doc ranges.
    doc_ranges: list[tuple[int, int]] = []
    for i, b in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else len(filtered)
        doc_ranges.append((b, end))

    limit_docs_raw = spec.options.get("limit_docs")
    if limit_docs_raw:
        try:
            limit_docs = max(1, int(limit_docs_raw))
            doc_ranges = doc_ranges[:limit_docs]
        except ValueError:
            pass

    paragraphs: list[DatasetParagraph] = []
    doc_to_paragraphs: dict[str, list[DatasetParagraph]] = defaultdict(list)
    position = 0
    used_doc_ids: set[str] = set()
    for r_idx, (start, end) in enumerate(doc_ranges):
        doc_id = _make_doc_id(filtered[start], r_idx)
        # Disambiguate the rare case where two articles share an identical slug.
        base_doc_id = doc_id
        suffix = 2
        while doc_id in used_doc_ids:
            doc_id = f"{base_doc_id}__{suffix}"
            suffix += 1
        used_doc_ids.add(doc_id)

        for j in range(start, end):
            row = filtered[j]
            segment_id = str(row.get("id", j))
            tr = row.get("translation") or {}
            src = str(tr.get(source_lang) or "").strip()
            ref = str(tr.get(target_lang) or "").strip()
            p = DatasetParagraph(
                paragraph_id=_stable_paragraph_id(doc_id, segment_id),
                source_text=src,
                reference_text=ref,
                document_id=doc_id,
                segment_id=segment_id,
                position=position,
            )
            paragraphs.append(p)
            doc_to_paragraphs[doc_id].append(p)
            position += 1

    book_id = compute_book_id(spec)
    title_parts = [spec.name, spec.language_pair, spec.register]
    if limit_docs_raw:
        title_parts.append(f"limit_docs={limit_docs_raw}")
    return EvalDataset(
        book_id=book_id,
        title=" / ".join(title_parts),
        source_language=source_lang,
        target_language=target_lang,
        register=spec.register,
        paragraphs=paragraphs,
        doc_to_paragraphs=dict(doc_to_paragraphs),
    )


def _stub_rows(spec: DatasetSpec) -> list[dict[str, Any]]:
    """Tiny offline corpus used by unit tests (``stub=true`` option).

    Layout mirrors what we see in the real corpus:
      - id 0      = title of article A (short)
      - id 1..2   = body of article A (long paragraphs)
      - id 3      = title of article B (short)
      - id 4..5   = body of article B (long paragraphs)
    """
    src_l = spec.language_pair.split("-")[0]
    tgt_l = spec.language_pair.split("-")[1]
    pairs = [
        ("1929 or 1989?",
         "1929年还是1989年?"),
        ("PARIS – As the economic crisis deepens and widens, the world has been "
         "searching for historical analogies to help us understand what has been "
         "happening. At the start of the crisis, many people likened it to 1982 "
         "or 1973, which was reassuring, because both dates refer to classical "
         "cyclical downturns.",
         "巴黎——随着经济危机不断加深和蔓延，整个世界一直在寻找历史上的类似情形以求帮助"
         "我们理解究竟发生了什么。"),
        ("Today, the mood is much grimmer, with references to 1929 and 1989 "
         "beginning to abound, even in such respectable places as the Financial "
         "Times and The Wall Street Journal.",
         "今天，悲观的情绪要弥漫得多，关于 1929 年和 1989 年的类比开始占据上风。"),
        ("What Failed in 2008?",
         "2008年究竟为何失败?"),
        ("You actually have to implement the solution – and be willing to change "
         "course if it turns out that you did not really have the right answer "
         "after all.",
         "你真的得去执行这个方案——并且要愿意在事实证明你的答案并不正确时改变路线。"),
        ("Three new books take up the challenge of explaining the why and how of "
         "the crisis, and what should be done to prevent a recurrence.",
         "有三本新书勇敢地承担起了解释这次危机的原因、过程，以及应该如何防止再次发生这一难题。"),
    ]
    return [
        {
            "id": i,
            "translation": {src_l: en, tgt_l: zh},
        }
        for i, (en, zh) in enumerate(pairs)
    ]


def _load_hf_rows(spec: DatasetSpec) -> list[dict[str, Any]]:
    """Pull rows from the Hugging Face Hub. May download on first run."""
    from datasets import load_dataset

    cache = os.environ.get("HF_DATASETS_CACHE")
    ds = load_dataset(_HF_NAME, spec.language_pair, split="train", cache_dir=cache)
    return [dict(row) for row in ds]


def load_news_commentary(spec: DatasetSpec) -> EvalDataset:
    """Adapter entry point — registered as ``news_commentary``."""
    if spec.options.get("stub", "").lower() in {"1", "true", "yes"}:
        return _build_dataset(spec, _stub_rows(spec))

    _log.info(
        "loading %s lp=%s register=%s (limit_docs=%s)",
        _HF_NAME,
        spec.language_pair,
        spec.register,
        spec.options.get("limit_docs"),
    )
    rows = _load_hf_rows(spec)
    return _build_dataset(spec, rows)


# Discoverable via the unified ``load_eval_dataset`` entry.
register_adapter("news_commentary", load_news_commentary)
