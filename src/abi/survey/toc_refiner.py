"""LLM-assisted refinement of a book's table of contents.

Runs once between Pass 0 (heuristic ingest) and Pass 1 (survey). The heuristic
parsers (TXT regex, EPUB ``hN`` tags) can miss real chapters (e.g. Roman-
numeral titles split across two lines) or falsely promote noise (running heads,
front-matter scraps). This module asks an LLM to confirm/correct the structure
**once** and then rewrites ``Book.toc`` accordingly.

## Pipeline
1. Flatten the book into an ordered ``_Node`` stream — both existing headings
   and prose paragraphs are nodes, each with a synthetic ``anchor_id``.
2. Filter to a candidate set: every existing heading + every prose paragraph
   that *looks like* a heading (short, no terminal punctuation, optionally
   matches a strong pattern). Capped at ``_MAX_CANDIDATES`` to keep one LLM
   call self-contained.
3. LLM picks the real chapters from the candidate list and returns cleaned
   titles + nesting levels.
4. Rebuild a fresh ``Book.toc`` from the LLM's selections, partitioning the
   original ordered paragraphs into the new sections.

The new ``section_id``s are derived from the new ``heading_trail`` (pure
function), so they're stable across re-runs as long as the LLM output is
stable. Paragraph IDs (content-hashed) are preserved.

If the LLM call fails, returns an empty result, or yields zero valid anchors,
this module returns the input book unchanged with ``method="heuristic_fallback"``
— refinement is opt-in robust, never a hard requirement.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass

from abi.ir.builder import is_non_content_heading
from abi.prompts import get_registry
from abi.providers.llm.factory import LLMRouter, system_message, user_message
from abi.providers.observability.events import EventLogger
from abi.survey._schemas import TocDetectorOutput
from abi.types.book import Book, Paragraph, Section
from abi.types.ids import section_id

_MAX_CANDIDATES = 250
_NEXT_PREVIEW_CHARS = 120
_MIN_DETECTED_CHAPTERS = 1

# Heading used for the synthetic wrapper holding any paragraphs that appear
# before the first LLM-detected chapter. Downstream selection (``--chapters``)
# treats sections with this exact heading as out-of-band: they're preserved
# alongside translation output but don't consume a numeric index.
SYNTHETIC_FRONT_MATTER_HEADING = "Front Matter"


# Strong patterns: matches almost always indicate a real heading. We always
# include nodes matching these as candidates regardless of length.
_STRONG_HEADING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^[IVXLC]+\.?\s*$"),                                # "I.", "III"
    re.compile(r"^Chapter\s+\d+\b", re.IGNORECASE),                 # "Chapter 3 ..."
    re.compile(r"^第[一二三四五六七八九十百0-9]+章"),
    re.compile(r"^PART\s+[IVXLC0-9]+\b", re.IGNORECASE),
    re.compile(r"^第[一二三四五六七八九十百0-9]+部分?\b"),
    re.compile(r"^[0-9]+(\.[0-9]+){0,3}\s+\S+"),                    # "1 Grow", "1.2 X"
    re.compile(r"^[IVXLC]+\.\s+[A-Z]"),                             # "I. BOURGEOIS"
]


@dataclass(frozen=True)
class _Node:
    """One element in the flattened book stream (heading or paragraph)."""

    kind: str  # "heading" | "paragraph"
    anchor_id: str
    text: str
    next_preview: str = ""
    paragraph_id: str | None = None   # set when kind == "paragraph"
    heading_level: int | None = None  # set when kind == "heading"


@dataclass(frozen=True)
class TocRefinement:
    """Metadata describing what TOC refinement did this run."""

    method: str            # "llm" | "heuristic_fallback" | "disabled"
    candidates: int = 0
    detected: int = 0
    top_level_before: int = 0
    top_level_after: int = 0
    reason: str = ""


# --------------------------------------------------------------------------- #
# Candidate extraction
# --------------------------------------------------------------------------- #

def _is_strong_heading(text: str) -> bool:
    text = text.strip()
    return any(p.match(text) for p in _STRONG_HEADING_PATTERNS)


def _looks_like_heading(text: str) -> bool:
    """Permissive filter: short + not sentence-shaped."""
    text = text.strip()
    if not text:
        return False
    if _is_strong_heading(text):
        return True
    # Reject obvious sentences.
    if len(text) > 120:
        return False
    # Pure punctuation / digits with no letters
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", text):
        return False
    last = text[-1]
    if last in ".!?" and len(text) > 40:
        return False
    if last in ",;:":
        return False
    if text.isupper() and 2 <= len(text) <= 100:
        return True
    return len(text) <= 80 and last not in ".!?,;:"


def _flatten_to_nodes(book: Book) -> list[_Node]:
    """Walk ``book.toc`` in document order, emitting one node per heading or
    paragraph. ``anchor_id``s are assigned sequentially (``C001``, ``C002`` …)
    so they're stable for a given book + heuristic-ingest output.
    """
    nodes: list[_Node] = []
    counter = 0

    def walk(s: Section) -> None:
        nonlocal counter
        if s.heading:
            counter += 1
            nodes.append(
                _Node(
                    kind="heading",
                    anchor_id=f"C{counter:04d}",
                    text=s.heading,
                    heading_level=s.level,
                )
            )
        for p in s.paragraphs:
            counter += 1
            nodes.append(
                _Node(
                    kind="paragraph",
                    anchor_id=f"C{counter:04d}",
                    text=p.source_text,
                    paragraph_id=p.paragraph_id,
                )
            )
        for c in s.children:
            walk(c)

    for top in book.toc:
        walk(top)

    # Backfill next_preview using the immediately following node's text.
    enriched: list[_Node] = []
    for i, n in enumerate(nodes):
        if i + 1 < len(nodes):
            nxt = nodes[i + 1].text
            preview = nxt[:_NEXT_PREVIEW_CHARS] + ("..." if len(nxt) > _NEXT_PREVIEW_CHARS else "")
        else:
            preview = ""
        enriched.append(dataclasses.replace(n, next_preview=preview))
    return enriched


def _select_candidates(nodes: list[_Node], *, max_count: int = _MAX_CANDIDATES) -> list[_Node]:
    """Pick heading-like nodes for the LLM to judge.

    Always include heuristic-detected headings (high prior). For prose-kind
    nodes, apply ``_looks_like_heading`` as a cheap pre-filter.
    """
    out: list[_Node] = []
    for n in nodes:
        keep = n.kind == "heading" or (
            n.kind == "paragraph" and _looks_like_heading(n.text)
        )
        if keep:
            out.append(n)
        if len(out) >= max_count:
            break
    return out


# --------------------------------------------------------------------------- #
# Rebuild
# --------------------------------------------------------------------------- #

def _collect_paragraph_index(book: Book) -> dict[str, Paragraph]:
    """Build a ``paragraph_id → Paragraph`` map by walking the entire toc."""
    out: dict[str, Paragraph] = {}

    def walk(s: Section) -> None:
        for p in s.paragraphs:
            out[p.paragraph_id] = p
        for c in s.children:
            walk(c)

    for top in book.toc:
        walk(top)
    return out


def _rebuild_book(
    book: Book,
    nodes: list[_Node],
    detections: list[tuple[str, str, int]],  # (anchor_id, title, level)
) -> Book:
    """Construct a new ``Book`` whose ``toc`` is partitioned by ``detections``.

    Algorithm:
      1. Map ``anchor_id`` → index in ``nodes``.
      2. Walk detections in document order.
      3. Paragraphs before the first detection become a synthetic "Front Matter"
         top-level section (omitted if there are none).
      4. Each detection range = (detection.anchor + 1, next.anchor) in node
         indices. Only **paragraph** nodes inside the range become section
         paragraphs; orphaned heading nodes are dropped (their text was
         already noise by definition since the LLM didn't anchor them).
      5. Stack-based nesting using ``detection.level`` builds the final tree.
    """
    anchor_to_idx = {n.anchor_id: i for i, n in enumerate(nodes)}
    ordered: list[tuple[int, str, int]] = sorted(
        (anchor_to_idx[a], t, lvl)
        for a, t, lvl in detections
        if a in anchor_to_idx
    )
    if not ordered:
        return book

    pid_to_para = _collect_paragraph_index(book)

    def paras_in_range(start: int, end: int) -> list[Paragraph]:
        return [
            pid_to_para[n.paragraph_id]
            for n in nodes[start:end]
            if n.kind == "paragraph"
            and n.paragraph_id is not None
            and n.paragraph_id in pid_to_para
        ]

    # 1. Optional Front Matter from paragraphs before the first detection.
    first_idx = ordered[0][0]
    fm_paras = paras_in_range(0, first_idx) if first_idx > 0 else []

    # 2. Build flat (level, title, paragraphs) list.
    flat_sections: list[tuple[int, str, list[Paragraph]]] = []
    if fm_paras:
        # Front matter must be a SIBLING of the detected chapters, not their
        # parent. Use the shallowest (min) level among detections so the
        # synthetic Front Matter sits at the same depth as the first real
        # chapter and doesn't accidentally swallow the entire book.
        fm_level = min(level for _, _, level in ordered)
        flat_sections.append((fm_level, SYNTHETIC_FRONT_MATTER_HEADING, fm_paras))
    for i, (idx, title, level) in enumerate(ordered):
        start = idx + 1  # the anchor node itself is consumed as the title
        end = ordered[i + 1][0] if i + 1 < len(ordered) else len(nodes)
        flat_sections.append((level, title, paras_in_range(start, end)))

    # 3. Stack-based nesting.
    def new_section_dict(
        level: int, heading: str, trail: list[str], paras: list[Paragraph]
    ) -> dict[str, object]:
        return {
            "level": level,
            "heading": heading,
            "trail": trail,
            "paragraphs": list(paras),
            "children": [],
        }

    root: dict[str, object] = new_section_dict(0, "", [], [])
    stack: list[dict[str, object]] = [root]
    for level, title, paras in flat_sections:
        while len(stack) > 1 and int(stack[-1]["level"]) >= level:  # type: ignore[arg-type]
            stack.pop()
        parent = stack[-1]
        parent_trail = list(parent["trail"])  # type: ignore[arg-type]
        trail = [*parent_trail, title]
        sec = new_section_dict(level, title, trail, paras)
        children = parent["children"]
        assert isinstance(children, list)
        children.append(sec)
        stack.append(sec)

    # 4. Materialize into frozen Section objects, regenerating section_ids
    # and rebinding each paragraph's section_id to its new home.
    def materialize(d: dict[str, object]) -> Section:
        trail = list(d["trail"])  # type: ignore[arg-type]
        sid = section_id(trail)
        paras = [
            Paragraph(
                paragraph_id=p.paragraph_id,
                kind=p.kind,
                source_text=p.source_text,
                position=p.position,
                section_id=sid,
                anchors=list(p.anchors),
                attrs=dict(p.attrs),
            )
            for p in d["paragraphs"]  # type: ignore[union-attr]
        ]
        children = [materialize(c) for c in d["children"]]  # type: ignore[union-attr]
        return Section(
            section_id=sid,
            level=int(d["level"]),  # type: ignore[arg-type]
            heading=str(d["heading"]),
            heading_trail=trail,
            paragraphs=paras,
            children=children,
        )

    new_toc = [materialize(c) for c in root["children"]]  # type: ignore[union-attr]
    return book.model_copy(update={"toc": new_toc})


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

async def refine_book_toc(
    *,
    book: Book,
    router: LLMRouter,
    events: EventLogger,
) -> tuple[Book, TocRefinement]:
    """LLM-driven TOC refinement. Returns ``(possibly_new_book, metadata)``.

    Failure modes (LLM error, empty response, all-invalid anchors) all return
    the **original** book with ``method="heuristic_fallback"``. The caller
    should always use the returned book; never the input book.
    """
    top_before = len(book.toc)
    nodes = _flatten_to_nodes(book)
    candidates = _select_candidates(nodes)

    if not candidates:
        events.event("toc.refinement.skipped", reason="no_candidates")
        return book, TocRefinement(
            method="heuristic_fallback",
            candidates=0,
            detected=0,
            top_level_before=top_before,
            top_level_after=top_before,
            reason="no_candidates",
        )

    registry = get_registry()
    prompt = registry.render(
        "toc_detector",
        book_title=book.meta.title,
        source_language=book.meta.source_language,
        heuristic_chapter_count=top_before,
        candidates=[
            {
                "anchor_id": c.anchor_id,
                "kind": c.kind,
                "text": c.text,
                "next_preview": c.next_preview,
            }
            for c in candidates
        ],
    )
    messages = [
        system_message(
            "You output strict JSON only. No prose, no markdown fences."
        ),
        user_message(prompt),
    ]

    events.event("toc.refinement.start", candidates=len(candidates))
    try:
        parsed, _resp = await router.invoke_structured(
            TocDetectorOutput,
            messages,
            agent_name="toc_detector",
            prompt_version=registry.version_for("toc_detector"),
            metadata={
                "candidates": len(candidates),
                "heuristic_top_level": top_before,
            },
        )
    except Exception as exc:
        events.event(
            "toc.refinement.failed",
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return book, TocRefinement(
            method="heuristic_fallback",
            candidates=len(candidates),
            detected=0,
            top_level_before=top_before,
            top_level_after=top_before,
            reason=f"llm_failed:{type(exc).__name__}",
        )

    anchor_set = {c.anchor_id for c in candidates}
    valid: list[tuple[str, str, int]] = []
    dropped_non_content: list[str] = []
    for item in parsed.chapters:
        if item.anchor_id not in anchor_set:
            continue
        title = item.title.strip()
        if not title:
            continue
        # Defense in depth: even if the LLM picked "Contents" / "Index" /
        # "Copyright" etc., drop them here. Same blocklist used by Pass 0.
        if is_non_content_heading(title):
            dropped_non_content.append(title)
            continue
        level = max(1, min(item.level, 6))
        valid.append((item.anchor_id, title, level))
    if dropped_non_content:
        events.event(
            "toc.refinement.dropped_non_content",
            count=len(dropped_non_content),
            headings=dropped_non_content[:10],
        )

    if len(valid) < _MIN_DETECTED_CHAPTERS:
        events.event(
            "toc.refinement.empty",
            returned=len(parsed.chapters),
            valid=len(valid),
        )
        return book, TocRefinement(
            method="heuristic_fallback",
            candidates=len(candidates),
            detected=len(valid),
            top_level_before=top_before,
            top_level_after=top_before,
            reason="empty_detection",
        )

    refined = _rebuild_book(book, nodes, valid)
    top_after = len(refined.toc)
    events.event(
        "toc.refinement.applied",
        candidates=len(candidates),
        detected=len(valid),
        top_level_before=top_before,
        top_level_after=top_after,
    )
    return refined, TocRefinement(
        method="llm",
        candidates=len(candidates),
        detected=len(valid),
        top_level_before=top_before,
        top_level_after=top_after,
    )
