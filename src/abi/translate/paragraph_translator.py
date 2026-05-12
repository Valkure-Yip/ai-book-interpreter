"""Translate a single paragraph using the sliding-window context."""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import datetime

from abi.prompts import get_registry
from abi.providers.llm.factory import LLMRouter, system_message, user_message
from abi.translate.context_builder import (
    TranslationContext,
    WindowParagraph,
    extract_anchors,
)
from abi.translate.validator import validate_translation
from abi.types.book import Paragraph
from abi.types.glossary import Glossary
from abi.types.translation import (
    BatchTranslationOutput,
    ContextWindowMeta,
    ParagraphTranslationOutput,
    QualityFlag,
    TokenUsage,
    TranslationUnit,
)


def _passthrough_unit(paragraph: Paragraph, target_language: str, model: str) -> TranslationUnit:
    return TranslationUnit(
        paragraph_id=paragraph.paragraph_id,
        kind=paragraph.kind,
        source_text=paragraph.source_text,
        translated_text=paragraph.source_text,
        target_language=target_language,
        confidence=1.0,
        flags=[QualityFlag(code="passthrough", detail=paragraph.kind)],
        notes="passthrough: source preserved verbatim",
        model=model,
        provider="openai-compatible",
        prompt_version="passthrough",
    )


def _render_prompt(context: TranslationContext) -> str:
    registry = get_registry()
    return registry.render(
        "paragraph_translator",
        target_language=context.target_language,
        source_language=context.source_language,
        register=context.register,
        quote_style=context.quote_style,
        directives=context.directives,
        glossary=[
            {"term": e.term, "target": e.target, "definition": e.definition or ""}
            for e in context.glossary_slice
        ],
        book_thesis=context.book_thesis,
        book_audience=context.book_audience,
        heading_trail=context.heading_trail,
        position_in_chapter=context.position_in_chapter,
        chapter_length=context.chapter_length,
        chapter_abstract=context.chapter_abstract,
        crossed_chapter_boundary=context.crossed_chapter_boundary,
        prev_window=[
            {
                "offset": p.offset,
                "id": p.id,
                "source": p.source,
                "translated": p.translated,
            }
            for p in context.prev_window
        ],
        next_window=[
            {"offset": p.offset, "source": p.source} for p in context.next_window
        ],
        anchors=context.anchors,
        target={
            "id": context.target.id,
            "source": context.target.source,
            "kind": context.target_kind,
        },
    )


def _render_revision_prompt(
    context: TranslationContext, previous: str, problems: list[str]
) -> str:
    registry = get_registry()
    return registry.render(
        "revision_translator",
        previous_translation=previous,
        problems=problems,
        target={"id": context.target.id, "source": context.target.source},
        glossary=[
            {"term": e.term, "target": e.target} for e in context.glossary_slice
        ],
        anchors=context.anchors,
    )


async def translate_paragraph(
    *,
    router: LLMRouter,
    paragraph: Paragraph,
    context: TranslationContext,
    glossary: Glossary,
    max_revision_rounds: int = 2,
) -> TranslationUnit:
    # Special kinds bypass LLM.
    if paragraph.kind in ("code", "equation"):
        return _passthrough_unit(paragraph, context.target_language, router.model)
    if not paragraph.source_text.strip():
        return _passthrough_unit(paragraph, context.target_language, router.model)

    registry = get_registry()
    prompt_version = registry.version_for("paragraph_translator")

    prompt = _render_prompt(context)
    prompt_hash = hashlib.sha1(prompt.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]

    messages = [
        system_message(
            "You are a professional academic translator. Output strict JSON only. "
            "Do not include markdown fences."
        ),
        user_message(prompt),
    ]

    parsed, resp = await router.invoke_structured(
        ParagraphTranslationOutput,
        messages,
        agent_name="paragraph_translator",
        prompt_version=prompt_version,
        metadata={
            "paragraph_id": paragraph.paragraph_id,
            "section_id": paragraph.section_id,
            "kind": paragraph.kind,
        },
    )

    score, flags, terms_used, problems = validate_translation(
        output=parsed, context=context, glossary=glossary, kind=paragraph.kind
    )
    retries = 0

    while problems and retries < max_revision_rounds:
        retries += 1
        rev_prompt = _render_revision_prompt(context, parsed.translated_text, problems)
        rev_messages = [
            system_message("You output strict JSON only."),
            user_message(rev_prompt),
        ]
        try:
            parsed, resp2 = await router.invoke_structured(
                ParagraphTranslationOutput,
                rev_messages,
                agent_name="revision_translator",
                prompt_version=registry.version_for("revision_translator"),
                metadata={
                    "paragraph_id": paragraph.paragraph_id,
                    "round": retries,
                },
            )
            resp = resp2  # update resource accounting
        except Exception:
            break
        score, flags, terms_used, problems = validate_translation(
            output=parsed, context=context, glossary=glossary, kind=paragraph.kind
        )

    unit = TranslationUnit(
        paragraph_id=paragraph.paragraph_id,
        kind=paragraph.kind,
        source_text=paragraph.source_text,
        translated_text=parsed.translated_text,
        target_language=context.target_language,
        terms_used=terms_used,
        confidence=score,
        flags=flags,
        notes=parsed.notes,
        prompt_version=prompt_version,
        model=router.model,
        provider="openai-compatible",
        token_usage=TokenUsage(input=resp.tokens_in, output=resp.tokens_out),
        cost_usd=resp.cost_usd,
        latency_ms=resp.latency_ms,
        retries=retries,
        context_window=ContextWindowMeta(
            k_before=len(context.prev_window),
            j_after=len(context.next_window),
            glossary_size=len(context.glossary_slice),
            crossed_chapter_boundary=context.crossed_chapter_boundary,
            trimmed_reasons=context.trimmed_reasons,
            prompt_hash=prompt_hash,
            token_budget=0,
            glossary_version=glossary.version,
        ),
        created_at=datetime.utcnow(),
    )
    return unit


def _render_batch_prompt(
    context: TranslationContext, paragraphs: list[Paragraph]
) -> str:
    registry = get_registry()
    return registry.render(
        "paragraph_batch_translator",
        target_language=context.target_language,
        source_language=context.source_language,
        register=context.register,
        quote_style=context.quote_style,
        directives=context.directives,
        glossary=[
            {"term": e.term, "target": e.target, "definition": e.definition or ""}
            for e in context.glossary_slice
        ],
        book_thesis=context.book_thesis,
        book_audience=context.book_audience,
        heading_trail=context.heading_trail,
        chapter_abstract=context.chapter_abstract,
        crossed_chapter_boundary=context.crossed_chapter_boundary,
        prev_window=[
            {
                "offset": p.offset,
                "id": p.id,
                "source": p.source,
                "translated": p.translated,
            }
            for p in context.prev_window
        ],
        next_window=[
            {"offset": p.offset, "source": p.source} for p in context.next_window
        ],
        anchors=context.anchors,
        targets=[
            {"id": p.paragraph_id, "kind": p.kind, "source": p.source_text}
            for p in paragraphs
        ],
    )


async def translate_paragraph_batch(
    *,
    router: LLMRouter,
    paragraphs: list[Paragraph],
    context: TranslationContext,
    glossary: Glossary,
) -> dict[str, TranslationUnit] | None:
    """Translate K paragraphs in one LLM call. Returns ``None`` on failure so
    the caller can fall back to per-paragraph translation.

    Notes:
    - All paragraphs share the same surrounding context (sliding window
      paragraphs from BEFORE the batch). Within the batch, paragraphs do NOT
      see each other's translations — they are produced in one inference pass.
    - Validation is applied per item. Failed items get flagged but are NOT
      re-translated via ``revision_translator`` here; caller can re-process if
      needed. This keeps the batch fast-path simple; revision falls back to
      single-paragraph mode automatically (caller decides).
    - Passthrough paragraphs (code/equation/empty) are filtered out BEFORE
      calling this function; the caller is expected to handle those.
    """
    if not paragraphs:
        return {}

    registry = get_registry()
    prompt_version = registry.version_for("paragraph_batch_translator")
    prompt = _render_batch_prompt(context, paragraphs)
    prompt_hash = hashlib.sha1(prompt.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    messages = [
        system_message(
            "You are a professional academic translator. Output strict JSON only. "
            "Do not include markdown fences."
        ),
        user_message(prompt),
    ]

    try:
        parsed, resp = await router.invoke_structured(
            BatchTranslationOutput,
            messages,
            agent_name="paragraph_batch_translator",
            prompt_version=prompt_version,
            metadata={
                "batch_size": len(paragraphs),
                "first_paragraph_id": paragraphs[0].paragraph_id,
                "section_id": paragraphs[0].section_id,
            },
        )
    except Exception:
        return None

    # Match items back by paragraph_id. If the model dropped any or invented
    # extras, treat the batch as failed so caller falls back.
    by_id = {item.paragraph_id: item for item in parsed.items}
    if not all(p.paragraph_id in by_id for p in paragraphs):
        return None

    # Even cost split: we can't get per-item cost back from one LLM call.
    n = len(paragraphs)
    per_item_cost = resp.cost_usd / n if n else 0.0
    per_item_tokens_in = resp.tokens_in // n if n else 0
    per_item_tokens_out = resp.tokens_out // n if n else 0
    per_item_latency = resp.latency_ms // n if n else 0

    units: dict[str, TranslationUnit] = {}
    for p in paragraphs:
        item = by_id[p.paragraph_id]
        view = ParagraphTranslationOutput(
            translated_text=item.translated_text,
            terms_used=item.terms_used,
            confidence=item.confidence,
            notes=item.notes,
            untranslated_passthrough=item.untranslated_passthrough,
        )
        # Per-paragraph view of the shared context. The validator reads
        # ``context.target.source`` and ``context.anchors`` — both must reflect
        # THIS paragraph, not the batch leader.
        per_item_ctx = dataclasses.replace(
            context,
            target=WindowParagraph(
                offset=0,
                id=p.paragraph_id,
                source=p.source_text,
                translated=None,
            ),
            target_kind=p.kind,
            anchors=extract_anchors(p.source_text),
        )
        score, flags, terms_used, _problems = validate_translation(
            output=view,
            context=per_item_ctx,
            glossary=glossary,
            kind=p.kind,
        )
        units[p.paragraph_id] = TranslationUnit(
            paragraph_id=p.paragraph_id,
            kind=p.kind,
            source_text=p.source_text,
            translated_text=view.translated_text,
            target_language=context.target_language,
            terms_used=terms_used,
            confidence=score,
            flags=flags,
            notes=view.notes,
            prompt_version=prompt_version,
            model=router.model,
            provider="openai-compatible",
            token_usage=TokenUsage(input=per_item_tokens_in, output=per_item_tokens_out),
            cost_usd=per_item_cost,
            latency_ms=per_item_latency,
            retries=0,
            context_window=ContextWindowMeta(
                k_before=len(context.prev_window),
                j_after=len(context.next_window),
                glossary_size=len(context.glossary_slice),
                crossed_chapter_boundary=context.crossed_chapter_boundary,
                trimmed_reasons=[*context.trimmed_reasons, f"batch:{n}"],
                prompt_hash=prompt_hash,
                token_budget=0,
                glossary_version=glossary.version,
            ),
            created_at=datetime.utcnow(),
        )
    return units
