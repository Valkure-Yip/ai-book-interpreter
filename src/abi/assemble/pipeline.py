"""Pass 3: compose final Markdown outputs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from abi.assemble.components import render_bilingual, render_translated
from abi.providers.observability.events import EventLogger, MetricsAggregator
from abi.types.book import Book, Section
from abi.types.glossary import Glossary
from abi.types.run import OutputMode, RunConfig
from abi.types.survey import BookOverview
from abi.types.translation import TranslationUnit


@dataclass
class AssembleResult:
    output_paths: dict[OutputMode, Path]
    report_path: Path
    summary: dict[str, int | float | str]


def _walk_sections(section: Section, depth: int, render_fn: Callable[[Section, int], list[str]],
                   ) -> list[str]:
    lines = render_fn(section, depth)
    for child in section.children:
        lines.extend(_walk_sections(child, depth + 1, render_fn))
    return lines


def _heading_md(
    section: Section,
    base_level: int,
    *,
    headings_map: dict[str, str] | None = None,
    bilingual: bool = False,
) -> str:
    level = max(1, min(6, base_level + section.level - 1))
    source = section.heading
    translated = (headings_map or {}).get(section.section_id, "").strip()
    if translated and translated != source:
        text = f"{translated} ({source})" if bilingual else translated
    else:
        text = source
    return f"{'#' * level} {text}"


def _render_translated(
    book: Book,
    units: dict[str, TranslationUnit],
    headings_map: dict[str, str] | None = None,
) -> str:
    parts: list[str] = [f"# {book.meta.title}", ""]
    if book.meta.authors:
        parts.append(f"> {', '.join(book.meta.authors)}")
        parts.append("")

    def render_section(s: Section, depth: int) -> list[str]:
        out = [_heading_md(s, base_level=2, headings_map=headings_map), ""]
        for p in s.paragraphs:
            unit = units.get(p.paragraph_id)
            out.append(render_translated(p, unit))
            out.append("")
        return out

    for top in book.toc:
        parts.extend(_walk_sections(top, 1, render_section))
    return "\n".join(parts).rstrip() + "\n"


def _render_bilingual(
    book: Book,
    units: dict[str, TranslationUnit],
    headings_map: dict[str, str] | None = None,
) -> str:
    parts: list[str] = [f"# {book.meta.title} (双语对照)", ""]

    def render_section(s: Section, depth: int) -> list[str]:
        out = [
            _heading_md(s, base_level=2, headings_map=headings_map, bilingual=True),
            "",
        ]
        for p in s.paragraphs:
            unit = units.get(p.paragraph_id)
            out.append(render_bilingual(p, unit))
            out.append("")
        return out

    for top in book.toc:
        parts.extend(_walk_sections(top, 1, render_section))
    return "\n".join(parts).rstrip() + "\n"


def _render_annotated(
    book: Book,
    units: dict[str, TranslationUnit],
    overview: BookOverview,
    glossary: Glossary,
    headings_map: dict[str, str] | None = None,
) -> str:
    summary_by_section = {c.section_id: c for c in overview.chapter_summaries}
    parts: list[str] = [f"# {book.meta.title} (带 AI 摘要)", ""]
    if overview.thesis:
        parts.extend([f"> **核心论点**：{overview.thesis}", ""])

    def render_section(s: Section, depth: int) -> list[str]:
        out = [_heading_md(s, base_level=2, headings_map=headings_map), ""]
        summ = summary_by_section.get(s.section_id)
        if summ and summ.key_points:
            out.append("> **本章要点**：")
            for p in summ.key_points:
                out.append(f"> - {p}")
            out.append("")
        for p in s.paragraphs:
            unit = units.get(p.paragraph_id)
            out.append(render_translated(p, unit))
            out.append("")
        return out

    for top in book.toc:
        parts.extend(_walk_sections(top, 1, render_section))

    if overview.mindmap_mermaid:
        parts.extend(["", "## 思维导图", "", "```mermaid", overview.mindmap_mermaid, "```"])
    parts.extend(["", "## 术语表", "", "| 原文 | 译文 | 释义 |", "| --- | --- | --- |"])
    for e in glossary.entries:
        defn = e.definition.replace("|", "\\|") if e.definition else ""
        parts.append(f"| {e.term} | {e.target} | {defn} |")

    return "\n".join(parts).rstrip() + "\n"


def _render_report(
    book: Book,
    units: dict[str, TranslationUnit],
    overview: BookOverview | None,
    glossary: Glossary,
    config: RunConfig,
    metrics: MetricsAggregator,
) -> str:
    snap = metrics.snapshot()
    confidences = [u.confidence for u in units.values() if u.confidence > 0]
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
    high = sum(1 for c in confidences if c >= 0.9)
    mid = sum(1 for c in confidences if 0.7 <= c < 0.9)
    low = sum(1 for c in confidences if c < 0.7)
    flagged = [u for u in units.values()
               if any(f.code != "passthrough" for f in u.flags)]
    lines = [
        "# 翻译报告",
        "",
        "## 基本信息",
        f"- 书名：{book.meta.title}",
        f"- 作者：{', '.join(book.meta.authors) or '(unknown)'}",
        f"- 源语言 → 目标语言：{book.meta.source_language} → {config.target_language}",
        f"- 输入格式：{book.meta.source_format}",
        f"- 模型 / 端点：{config.llm.model} @ {config.llm.base_url}",
        f"- Book ID：`{book.meta.book_id}`",
        f"- Run ID：`{snap.get('run_id')}`",
        "",
        "## 段落统计",
        f"- 总段落数：{snap['paragraphs']['total']}",
        f"- 已翻译：{snap['paragraphs']['done']}",
        f"- 标记（flagged）：{snap['paragraphs']['flagged']}",
        f"- 失败：{snap['paragraphs']['failed']}",
        "",
        "## Confidence 分布",
        f"- ≥ 0.9: {high}",
        f"- 0.7-0.9: {mid}",
        f"- < 0.7: {low}",
        f"- 平均：{avg_conf:.3f}",
        "",
        "## Flag 计数",
    ]
    for code, count in snap.get("flag_counts", {}).items():
        lines.append(f"- `{code}`: {count}")
    lines.extend(["", "## Token & 成本"])
    lines.append(f"- LLM 调用次数：{snap['llm_calls']}")
    lines.append(f"- 输入 tokens：{snap['tokens']['input']:,}")
    lines.append(f"- 输出 tokens：{snap['tokens']['output']:,}")
    lines.append(f"- 估算成本：$ {snap['cost_usd']:.4f}")
    lines.append(f"- 用时：{snap['duration_s']} 秒")
    lines.extend(["", "## Flagged 段落 (Top 20)"])
    for u in flagged[:20]:
        flag_codes = ", ".join(f.code for f in u.flags)
        excerpt = (u.source_text[:80] + "…") if len(u.source_text) > 80 else u.source_text
        lines.append(f"- `{u.paragraph_id}` [{flag_codes}]: {excerpt}")
    lines.extend(["", "## 术语表大小", f"- 共 {len(glossary.entries)} 条"])
    if overview and overview.thesis:
        lines.extend(["", "## 全书论点", overview.thesis])
    return "\n".join(lines) + "\n"


def assemble(
    *,
    book: Book,
    units: dict[str, TranslationUnit],
    overview: BookOverview | None,
    glossary: Glossary,
    config: RunConfig,
    events: EventLogger,
    metrics: MetricsAggregator,
    out_dir: Path,
    headings_map: dict[str, str] | None = None,
) -> AssembleResult:
    events.event("pass.start", pass_name="assemble", book_id=book.meta.book_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[OutputMode, Path] = {}
    for mode in config.modes:
        if mode == "translated":
            content = _render_translated(book, units, headings_map=headings_map)
            p = out_dir / "translated.md"
        elif mode == "bilingual":
            content = _render_bilingual(book, units, headings_map=headings_map)
            p = out_dir / "bilingual.md"
        elif mode == "annotated":
            if overview is None:
                events.event("assemble.skipped", mode=mode, reason="no overview")
                continue
            content = _render_annotated(
                book, units, overview, glossary, headings_map=headings_map
            )
            p = out_dir / "annotated.md"
        elif mode == "survey-only":
            # Survey artifacts are written by survey pipeline; nothing to do here.
            continue
        else:  # pragma: no cover
            continue
        p.write_text(content, encoding="utf-8")
        paths[mode] = p
        events.event("assemble.wrote", mode=mode, path=str(p), bytes=len(content))

    report = _render_report(book, units, overview, glossary, config, metrics)
    report_path = out_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")

    snap = metrics.snapshot()
    summary: dict[str, int | float | str] = {
        "paragraphs_done": int(snap["paragraphs"]["done"]),
        "paragraphs_flagged": int(snap["paragraphs"]["flagged"]),
        "cost_usd": float(snap["cost_usd"]),
        "model": config.llm.model,
    }
    events.event("pass.end", pass_name="assemble")
    return AssembleResult(output_paths=paths, report_path=report_path, summary=summary)
