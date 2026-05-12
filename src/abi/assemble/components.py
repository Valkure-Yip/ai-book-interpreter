"""Render individual blocks (Paragraph + TranslationUnit) into Markdown chunks."""

from __future__ import annotations

from abi.types.book import Paragraph
from abi.types.translation import TranslationUnit


def render_translated(p: Paragraph, unit: TranslationUnit | None) -> str:
    text = unit.translated_text if unit else p.source_text
    if p.kind == "code":
        lang = p.attrs.get("code_lang", "")
        return f"```{lang}\n{p.source_text}\n```"
    if p.kind == "equation":
        return f"$$\n{p.source_text}\n$$"
    if p.kind == "quote":
        return "\n".join("> " + line for line in text.splitlines())
    if p.kind == "list_item":
        return f"- {text}"
    if p.kind == "figure_caption":
        return f"> *Figure: {text}*"
    if p.kind == "footnote":
        return f"[^{p.paragraph_id}]: {text}"
    return text


def render_bilingual(p: Paragraph, unit: TranslationUnit | None) -> str:
    if p.kind in ("code", "equation"):
        return render_translated(p, unit)
    src = p.source_text
    tgt = unit.translated_text if unit else "(untranslated)"
    if p.kind == "quote":
        src_block = "\n".join("> " + line for line in src.splitlines())
        tgt_block = "\n".join("> " + line for line in tgt.splitlines())
        return f"{src_block}\n>\n{tgt_block}"
    if p.kind == "list_item":
        return f"- **EN:** {src}\n- **ZH:** {tgt}"
    return f"> **EN:** {src}\n>\n> **ZH:** {tgt}"
