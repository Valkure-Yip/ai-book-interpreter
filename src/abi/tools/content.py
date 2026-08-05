"""Source ingest + chapter split + state tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path

from abi.ir import ingest
from abi.ir.split import split_book_to_chapters, write_toc_json
from abi.ir.toc_refiner import needs_refinement, refine_toc_with_llm
from abi.project.state import Status
from abi.tools.context import ToolContext
from abi.types._base import FrozenModel
from abi.types.book import Book
from abi.types.orchestration import RunSnapshot
from abi.types.tools import ToolBinding

_log = logging.getLogger(__name__)


class EmptyInput(FrozenModel):
    """No arguments are accepted by this tool."""


def make_content_tools(
    ctx: ToolContext,
    *,
    get_run_snapshot: Callable[[], RunSnapshot] | None = None,
) -> list[ToolBinding]:
    project = ctx.project

    def ingest_source() -> str:
        """Parse the raw source file into clean text + source_manifest.json.

        Reads ``source/source_text_raw.txt`` (or an ingested EPUB placed in
        ``source/``) and writes ``source/source_text.txt`` plus
        ``source/source_manifest.json`` (hash, format, paragraph/section counts).
        """
        raw = project.source_raw
        epubs = sorted(project.root.glob("source/*.epub"))
        src_path = raw if raw.exists() else (epubs[0] if epubs else None)
        if src_path is None:
            return (
                "ERROR: no source found. Place the source text at "
                "source/source_text_raw.txt or an .epub under source/."
            )
        book, warnings = ingest(src_path)
        paras = book.iter_paragraphs()
        clean = "\n\n".join(p.source_text for p in paras if p.source_text.strip())
        project.source_clean.write_text(clean, encoding="utf-8")
        data = src_path.read_bytes()
        manifest = {
            "source_file": project.rel(src_path),
            "format": book.meta.source_format,
            "sha256": hashlib.sha256(data).hexdigest(),
            "title": book.meta.title,
            "authors": book.meta.authors,
            "source_language": book.meta.source_language,
            "sections": len(book.toc),
            "paragraphs": len(paras),
            "warnings": warnings[:50],
        }
        project.source_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        st = ctx.state()
        st.advance(
            Status.SOURCE_INGESTED,
            step="01_ingest_clean",
            note=f"{len(paras)} paragraphs, {len(book.toc)} sections",
        )
        st.record_artifact("source_clean", project.rel(project.source_clean))
        ctx.save_state(st)
        return (
            f"ingested {book.meta.source_format}: {len(book.toc)} sections, "
            f"{len(paras)} paragraphs. Wrote source_text.txt + source_manifest.json."
        )

    def _maybe_refine_toc(book: Book, warnings: list[str], src_path: Path) -> Book:
        """Run Pass 0.5 LLM TOC refinement if the heuristic result is suspect."""
        if ctx.config is not None and not ctx.config.refine_toc:
            return book
        if not needs_refinement(book, warnings):
            return book
        _log.info(
            "toc_refiner: Pass 0 produced suspect structure (%d sections, "
            "%d paragraphs), running Pass 0.5 LLM refinement",
            len(book.toc),
            len(book.iter_paragraphs()),
        )
        raw_text = src_path.read_text(encoding="utf-8", errors="replace")
        try:
            refined = asyncio.get_event_loop().run_until_complete(
                refine_toc_with_llm(book, raw_text, router=ctx.services.router)
            )
        except RuntimeError:
            # No running event loop; create one.
            refined = asyncio.run(refine_toc_with_llm(book, raw_text, router=ctx.services.router))
        if len(refined.toc) > len(book.toc):
            _log.info("toc_refiner: refined %d -> %d sections", len(book.toc), len(refined.toc))
            ctx.services.events.event(
                "toc.refinement.applied",
                before=len(book.toc),
                after=len(refined.toc),
            )
            return refined
        _log.info("toc_refiner: refinement did not improve (kept %d sections)", len(book.toc))
        ctx.services.events.event("toc.refinement.skipped", reason="no improvement")
        return book

    def split_chapters() -> str:
        """Split the ingested source into chapters/src/{NNN_slug}.md + source/toc.json."""
        raw = project.source_raw
        epubs = sorted(project.root.glob("source/*.epub"))
        src_path = raw if raw.exists() else (epubs[0] if epubs else None)
        if src_path is None:
            return "ERROR: ingest the source first (no source file found)."
        book, warnings = ingest(src_path)
        book = _maybe_refine_toc(book, warnings, src_path)
        entries = split_book_to_chapters(book, project.chapters_src)
        write_toc_json(entries, project.toc_json)
        st = ctx.state()
        st.advance(Status.SOURCE_SPLIT, step="02_split", note=f"{len(entries)} chapters")
        st.record_artifact("toc", project.rel(project.toc_json))
        ctx.save_state(st)
        listing = "\n".join(f"  {e.slug} ({e.paragraph_count} paras)" for e in entries[:60])
        return f"split into {len(entries)} chapters:\n{listing}"

    def read_run_snapshot() -> str:
        """Return the Action-scoped, read-only RunLedger snapshot as JSON."""
        if get_run_snapshot is None:
            return (
                "ERROR: this Action has no run snapshot callback. "
                "Bind get_run_snapshot when constructing its tool belt."
            )
        return json.dumps(get_run_snapshot().model_dump(mode="json"), ensure_ascii=False, indent=2)

    return [
        ToolBinding(
            "ingest_source",
            ingest_source.__doc__ or "Ingest the source.",
            EmptyInput,
            ingest_source,
        ),
        ToolBinding(
            "split_chapters",
            split_chapters.__doc__ or "Split chapters.",
            EmptyInput,
            split_chapters,
        ),
        ToolBinding(
            "get_run_snapshot",
            read_run_snapshot.__doc__ or "Read the current run snapshot.",
            EmptyInput,
            read_run_snapshot,
        ),
    ]
