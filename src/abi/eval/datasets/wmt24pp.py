"""``google/wmt24pp`` adapter.

WMT24++ is the human-authored extension of the WMT24 General translation
test sets. Each row holds:

- ``lp``           — language-pair code, e.g. ``"en-zh_CN"``
- ``domain``       — document genre, e.g. ``"literary"``
- ``document_id``  — the source document this segment belongs to
- ``segment_id``   — stable per-segment id (string of integers)
- ``is_bad_source``— skip flag for noisy / boilerplate segments
- ``source``       — the English (or whatever) source sentence/paragraph
- ``target``       — the post-edited human reference (the canonical one)
- ``original_target`` — un-edited human reference; keep for completeness

We pick ``target`` (post-edited) as ``reference_text`` per the dataset
authors' recommendation.

The adapter is **stub-friendly**: passing ``stub=true`` as a spec option
returns a tiny in-memory dataset so unit tests can exercise the path without
network access.
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

_HF_NAME = "google/wmt24pp"


def _stable_paragraph_id(document_id: str, segment_id: str) -> str:
    """Deterministic paragraph_id from (document_id, segment_id).

    ``segment_id`` is only unique *within* a document, so the document_id
    must be in the hash input to avoid collisions across docs (which would
    silently drop dataset items when the Langfuse experiment dedupes by id).
    """
    return hashlib.sha1(
        f"wmt24pp:{document_id}:{segment_id}".encode(),
        usedforsecurity=False,
    ).hexdigest()[:16]


def _build_dataset(
    spec: DatasetSpec,
    rows: list[dict[str, Any]],
) -> EvalDataset:
    """Filter, group, and order rows into an :class:`EvalDataset`."""
    target_lang = spec.language_pair.split("-")[1] if "-" in spec.language_pair else ""
    source_lang = spec.language_pair.split("-")[0] if "-" in spec.language_pair else spec.language_pair

    filtered = [
        r for r in rows
        if r.get("domain") == spec.register
        and not r.get("is_bad_source", False)
        and (r.get("source") or "").strip()
        and (r.get("target") or "").strip()
    ]

    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in filtered:
        by_doc[str(r.get("document_id", ""))].append(r)

    for doc_id in by_doc:
        by_doc[doc_id].sort(key=lambda r: _segment_sort_key(r.get("segment_id", "")))

    limit_docs_raw = spec.options.get("limit_docs")
    if limit_docs_raw:
        try:
            limit_docs = max(1, int(limit_docs_raw))
        except ValueError:
            limit_docs = None
    else:
        limit_docs = None

    doc_ids_in_order = sorted(by_doc.keys())
    if limit_docs is not None:
        doc_ids_in_order = doc_ids_in_order[:limit_docs]

    paragraphs: list[DatasetParagraph] = []
    doc_to_paragraphs: dict[str, list[DatasetParagraph]] = {}
    position = 0
    for doc_id in doc_ids_in_order:
        doc_paras: list[DatasetParagraph] = []
        for r in by_doc[doc_id]:
            segment_id = str(r.get("segment_id", ""))
            p = DatasetParagraph(
                paragraph_id=_stable_paragraph_id(doc_id, segment_id),
                source_text=str(r.get("source", "")).strip(),
                reference_text=str(r.get("target", "")).strip(),
                document_id=doc_id,
                segment_id=segment_id,
                position=position,
            )
            paragraphs.append(p)
            doc_paras.append(p)
            position += 1
        if doc_paras:
            doc_to_paragraphs[doc_id] = doc_paras

    book_id = compute_book_id(spec)
    title_parts = [spec.name, spec.language_pair, spec.register]
    if limit_docs is not None:
        title_parts.append(f"limit_docs={limit_docs}")

    return EvalDataset(
        book_id=book_id,
        title=" / ".join(title_parts),
        source_language=source_lang,
        target_language=target_lang,
        register=spec.register,
        paragraphs=paragraphs,
        doc_to_paragraphs=doc_to_paragraphs,
    )


def _segment_sort_key(segment_id: str) -> tuple[int, str]:
    """Sort segments numerically when possible, else lexically.

    Many WMT24++ segments use numeric ids; some literary subsets have
    dotted ids like ``"3.1"``. Fall back to a (large-int, str) tuple so
    purely numeric ids sort first and consistently.
    """
    try:
        return (int(segment_id), "")
    except ValueError:
        return (1 << 30, segment_id)


def _stub_rows(spec: DatasetSpec) -> list[dict[str, Any]]:
    """Tiny offline corpus used by unit tests (``stub=true`` option)."""
    return [
        {
            "lp": spec.language_pair,
            "domain": spec.register,
            "document_id": "doc-A",
            "segment_id": "1",
            "is_bad_source": False,
            "source": "It was a bright cold day in April, and the clocks were striking thirteen.",
            "target": "四月里一个晴朗寒冷的日子，钟敲了十三下。",
            "original_target": "四月里一个晴朗而寒冷的日子，钟敲了十三下。",
        },
        {
            "lp": spec.language_pair,
            "domain": spec.register,
            "document_id": "doc-A",
            "segment_id": "2",
            "is_bad_source": False,
            "source": "Winston Smith, his chin nuzzled into his breast in an effort to escape the vile wind, slipped quickly through the glass doors of Victory Mansions.",
            "target": "温斯顿·史密斯把下巴贴在胸前，以躲避刺骨的风，迅速钻进了胜利大厦的玻璃门。",
            "original_target": "温斯顿·史密斯下巴贴胸，躲着寒风，迅速穿过胜利大厦的玻璃门。",
        },
        {
            "lp": spec.language_pair,
            "domain": "news",  # filtered out — wrong register
            "document_id": "doc-B",
            "segment_id": "1",
            "is_bad_source": False,
            "source": "Markets opened higher today.",
            "target": "今日市场高开。",
            "original_target": "市场今日高开。",
        },
        {
            "lp": spec.language_pair,
            "domain": spec.register,
            "document_id": "doc-A",
            "segment_id": "3",
            "is_bad_source": True,  # filtered out
            "source": "[noise]",
            "target": "[噪声]",
            "original_target": "[噪声]",
        },
        {
            "lp": spec.language_pair,
            "domain": spec.register,
            "document_id": "doc-C",
            "segment_id": "1",
            "is_bad_source": False,
            "source": "The old man was thin and gaunt with deep wrinkles in the back of his neck.",
            "target": "老人瘦削憔悴，颈后布满深深的皱纹。",
            "original_target": "老人骨瘦如柴，脖子后面满是深深的皱纹。",
        },
    ]


def _load_hf_rows(spec: DatasetSpec) -> list[dict[str, Any]]:
    """Pull rows from the Hugging Face Hub. May download on first run."""
    from datasets import load_dataset

    cache = os.environ.get("HF_DATASETS_CACHE")
    ds = load_dataset(_HF_NAME, spec.language_pair, split="train", cache_dir=cache)
    return [dict(row) for row in ds]


def load_wmt24pp(spec: DatasetSpec) -> EvalDataset:
    """Adapter entry point — registered as ``wmt24pp``."""
    if spec.options.get("stub", "").lower() in {"1", "true", "yes"}:
        return _build_dataset(spec, _stub_rows(spec))

    _log.info(
        "loading %s lp=%s register=%s (limit_docs=%s)",
        _HF_NAME, spec.language_pair, spec.register, spec.options.get("limit_docs"),
    )
    rows = _load_hf_rows(spec)
    return _build_dataset(spec, rows)


# Make the adapter discoverable via the unified ``load_eval_dataset`` entry.
register_adapter("wmt24pp", load_wmt24pp)
