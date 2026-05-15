"""Baseline translator.

Implements the comparison baseline: take the entire book source, push it into
ONE model call (or as few chunks as needed for context-window limits), ask for
the translation, return the concatenated output as plain text.

Deliberately bypasses all ABI machinery: no survey, no glossary, no sliding
window, no anchor preservation contract. The whole point is to measure how
much value the multi-pass pipeline adds.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from abi.eval._schemas import BaselineChunkOutput
from abi.prompts import get_registry
from abi.providers.llm.factory import LLMRouter, system_message, user_message
from abi.providers.observability.events import EventLogger
from abi.types.book import Book
from abi.types.eval import BaselineMeta

_log = logging.getLogger(__name__)

# Rough character-per-token ratio for input estimation: ~4 chars / English
# token, ~1.5 chars / CJK token. We deliberately underestimate (use 3) so
# chunks come out smaller than the nominal budget — safer than overflowing.
_CHARS_PER_TOKEN = 3


@dataclass
class BaselineResult:
    full_text: str
    paragraphs: list[str]
    meta: BaselineMeta


def _source_text(book: Book) -> str:
    """Concatenate every prose paragraph in book order with blank-line separators.

    Headings are NOT included — the baseline produces flat prose; alignment
    is done at the paragraph level downstream.
    """
    parts: list[str] = []
    for p in book.iter_paragraphs():
        text = p.source_text.strip()
        if not text:
            continue
        parts.append(text)
    return "\n\n".join(parts)


def _split_into_chunks(text: str, char_budget: int) -> list[str]:
    """Split ``text`` on paragraph boundaries respecting ``char_budget`` per chunk.

    Never splits a paragraph mid-text; a single paragraph larger than the
    budget gets its own oversize chunk (the model will handle it or fail
    loudly).
    """
    paragraphs = [p for p in re.split(r"\n\n+", text) if p.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for p in paragraphs:
        plen = len(p) + 2  # +2 for the joining "\n\n"
        if buf and buf_len + plen > char_budget:
            chunks.append("\n\n".join(buf))
            buf = [p]
            buf_len = plen
        else:
            buf.append(p)
            buf_len += plen
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks


async def generate_baseline(
    *,
    book: Book,
    router: LLMRouter,
    events: EventLogger,
    source_language: str,
    target_language: str,
    chunk_token_budget: int = 50_000,
) -> BaselineResult:
    """Translate the entire book in one (or a few) large LLM calls."""
    source_text = _source_text(book)
    char_budget = chunk_token_budget * _CHARS_PER_TOKEN
    chunks = _split_into_chunks(source_text, char_budget)

    events.event(
        "eval.baseline.start",
        chunks=len(chunks),
        chars=len(source_text),
        chunk_char_budget=char_budget,
        chunk_token_budget=chunk_token_budget,
    )

    registry = get_registry()
    t0 = time.perf_counter()
    pieces: list[str] = []
    tokens_in = 0
    tokens_out = 0
    cost = 0.0
    for i, chunk in enumerate(chunks):
        prompt = registry.render(
            "baseline_translator",
            source_language=source_language,
            target_language=target_language,
            source_text=chunk,
        )
        messages = [
            system_message(
                "You output strict JSON only. No prose outside the JSON envelope."
            ),
            user_message(prompt),
        ]
        try:
            parsed, resp = await router.invoke_structured(
                BaselineChunkOutput,
                messages,
                agent_name="baseline_translator",
                prompt_version=registry.version_for("baseline_translator"),
                metadata={"chunk_index": i, "chunk_count": len(chunks)},
            )
            pieces.append(parsed.translated_text.strip())
            tokens_in += resp.tokens_in
            tokens_out += resp.tokens_out
            cost += resp.cost_usd
        except Exception as exc:  # pragma: no cover — network failure path
            _log.error("baseline chunk %d failed: %s", i, exc)
            events.event(
                "eval.baseline.chunk_failed",
                chunk_index=i,
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            pieces.append("")

    full = "\n\n".join(p for p in pieces if p)
    paragraphs = [p.strip() for p in re.split(r"\n\n+", full) if p.strip()]
    latency_ms = int((time.perf_counter() - t0) * 1000)

    events.event(
        "eval.baseline.done",
        chunks=len(chunks),
        paragraphs_out=len(paragraphs),
        chars_out=len(full),
        latency_ms=latency_ms,
        cost_usd=round(cost, 6),
    )

    meta = BaselineMeta(
        model=router.model,
        base_url=router.base_url,
        chunks=len(chunks),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        latency_ms=latency_ms,
        chunk_token_budget=chunk_token_budget,
        output_chars=len(full),
    )
    return BaselineResult(full_text=full, paragraphs=paragraphs, meta=meta)


def save_baseline(result: BaselineResult, out_dir: Path) -> Path:
    """Persist the baseline translation as a flat markdown file + meta.json."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "translated.md"
    md_path.write_text(result.full_text + "\n", encoding="utf-8")
    meta_path = out_dir / "meta.json"
    meta_path.write_text(result.meta.model_dump_json(indent=2), encoding="utf-8")
    return md_path


def load_baseline(out_dir: Path) -> BaselineResult | None:
    """Return a previously saved baseline if both files are present."""
    md_path = out_dir / "translated.md"
    meta_path = out_dir / "meta.json"
    if not md_path.exists() or not meta_path.exists():
        return None
    full = md_path.read_text(encoding="utf-8").strip()
    paragraphs = [p.strip() for p in re.split(r"\n\n+", full) if p.strip()]
    meta = BaselineMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
    return BaselineResult(full_text=full, paragraphs=paragraphs, meta=meta)
