"""Source ingest + chapter split + state tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path

from abi.ir import ingest_bytes
from abi.ir.split import split_book_to_chapters, write_toc_json
from abi.ir.toc_refiner import needs_refinement, refine_toc_with_llm
from abi.project.artifact_paths import canonical_artifact_key
from abi.tools.context import ToolContext
from abi.tools.permissions import ActionPathPermissions
from abi.types._base import FrozenModel
from abi.types.book import Book
from abi.types.orchestration import RunSnapshot
from abi.types.tools import ToolBinding

_log = logging.getLogger(__name__)


class EmptyInput(FrozenModel):
    """No arguments are accepted by this tool."""


class IngestSourceToolInput(FrozenModel):
    source_relpath: str = "source/source_text_raw.txt"


class SplitSourceToolInput(FrozenModel):
    source_relpath: str = "source/source_text_raw.txt"
    refine_toc: bool = True


def make_content_tools(
    ctx: ToolContext,
    *,
    get_run_snapshot: Callable[[], RunSnapshot] | None = None,
    permissions: ActionPathPermissions | None = None,
) -> list[ToolBinding]:
    project = ctx.project
    snapshot_callback = get_run_snapshot or ctx.get_run_snapshot

    def require_read(path: str) -> None:
        canonical_artifact_key(path)
        if permissions is not None and not permissions.can_read(path):
            raise PermissionError(
                f"this Action is not allowed to read {path!r}; declare it in read_set"
            )

    def require_write(path: str) -> None:
        canonical_artifact_key(path)
        if permissions is not None and not permissions.can_write(path):
            raise PermissionError(
                f"this Action is not allowed to write {path!r}; declare it in write_set"
            )

    def authorized_read(path: str) -> Path:
        require_read(path)
        return ctx.authorize_read_path(path, permissions)

    def source_epubs() -> list[str]:
        epubs: list[str] = []
        for candidate in sorted(project.root.glob("source/*.epub")):
            relpath = candidate.relative_to(project.root).as_posix()
            if permissions is not None and not permissions.can_read(relpath):
                continue
            ctx.authorize_read_path(relpath, permissions)
            epubs.append(relpath)
        return epubs

    def select_source(source_relpath: str) -> tuple[str, bytes] | None:
        authorized_read(source_relpath)
        try:
            return source_relpath, ctx.read_authorized_bytes(source_relpath, permissions)
        except FileNotFoundError:
            if source_relpath != "source/source_text_raw.txt":
                return None
        for epub_relpath in source_epubs():
            try:
                return epub_relpath, ctx.read_authorized_bytes(epub_relpath, permissions)
            except FileNotFoundError:
                continue
        return None

    def ingest_source(source_relpath: str = "source/source_text_raw.txt") -> str:
        """Parse the raw source file into clean text + source_manifest.json.

        Reads ``source/source_text_raw.txt`` (or an ingested EPUB placed in
        ``source/``) and writes ``source/source_text.txt`` plus
        ``source/source_manifest.json`` (hash, format, paragraph/section counts).
        """
        selected = select_source(source_relpath)
        if selected is None:
            return (
                "ERROR: no source found. Place the source text at "
                "source/source_text_raw.txt or an .epub under source/."
            )
        src_relpath, data = selected
        require_write(project.rel(project.source_clean))
        require_write(project.rel(project.source_manifest))
        book, warnings = ingest_bytes(data, source_name=src_relpath)
        paras = book.iter_paragraphs()
        clean = "\n\n".join(p.source_text for p in paras if p.source_text.strip())
        project.source_clean.write_text(clean, encoding="utf-8")
        manifest = {
            "source_file": src_relpath,
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
        return (
            f"ingested {book.meta.source_format}: {len(book.toc)} sections, "
            f"{len(paras)} paragraphs. Wrote source_text.txt + source_manifest.json."
        )

    def _maybe_refine_toc(
        book: Book,
        warnings: list[str],
        source_data: bytes,
        *,
        refine_toc: bool,
    ) -> Book:
        """Run Pass 0.5 LLM TOC refinement if the heuristic result is suspect."""
        if not refine_toc:
            return book
        if not needs_refinement(book, warnings):
            return book
        _log.info(
            "toc_refiner: Pass 0 produced suspect structure (%d sections, "
            "%d paragraphs), running Pass 0.5 LLM refinement",
            len(book.toc),
            len(book.iter_paragraphs()),
        )
        raw_text = source_data.decode("utf-8", errors="replace")
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

    def split_chapters(
        source_relpath: str = "source/source_text_raw.txt",
        refine_toc: bool = True,
    ) -> str:
        """Split the ingested source into chapters/src/{NNN_slug}.md + source/toc.json."""
        selected = select_source(source_relpath)
        if selected is None:
            return "ERROR: ingest the source first (no source file found)."
        src_relpath, data = selected
        book, warnings = ingest_bytes(data, source_name=src_relpath)
        book = _maybe_refine_toc(
            book,
            warnings,
            data,
            refine_toc=refine_toc,
        )
        require_write(project.rel(project.toc_json))
        require_write(project.rel(project.chapters_src))
        entries = split_book_to_chapters(book, project.chapters_src)
        write_toc_json(entries, project.toc_json)
        listing = "\n".join(f"  {e.slug} ({e.paragraph_count} paras)" for e in entries[:60])
        return f"split into {len(entries)} chapters:\n{listing}"

    def read_run_snapshot() -> str:
        """Return the Action-scoped, read-only RunLedger snapshot as JSON."""
        return json.dumps(
            snapshot_callback().model_dump(mode="json"), ensure_ascii=False, indent=2
        )

    return [
        ToolBinding(
            "ingest_source",
            ingest_source.__doc__ or "Ingest the source.",
            IngestSourceToolInput,
            ingest_source,
        ),
        ToolBinding(
            "split_chapters",
            split_chapters.__doc__ or "Split chapters.",
            SplitSourceToolInput,
            split_chapters,
        ),
        ToolBinding(
            "get_run_snapshot",
            read_run_snapshot.__doc__ or "Read the current run snapshot.",
            EmptyInput,
            read_run_snapshot,
        ),
    ]
