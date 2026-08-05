"""Python-native EPUB 3 builder: Markdown chapters -> XHTML + OPF + nav + zip.

No external EPUB SDK: we render Markdown with markdown-it-py, normalise to
well-formed XHTML via lxml, then assemble a valid EPUB 3 container by hand. This
replaces PDBT's Node ``build:epub`` script.
"""

from __future__ import annotations

import contextlib
import io
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from lxml import etree, html
from markdown_it import MarkdownIt

from abi.epub.result import GateResult
from abi.project.layout import BookProject

_CSS = """\
body { font-family: serif; line-height: 1.6; margin: 1em; }
h1, h2, h3 { font-family: sans-serif; line-height: 1.3; }
h1 { font-size: 1.6em; margin: 1.2em 0 0.6em; }
h2 { font-size: 1.3em; margin: 1em 0 0.5em; }
p { margin: 0.6em 0; text-indent: 0; }
blockquote { margin: 0.8em 1.5em; color: #333; }
figure { margin: 1em 0; text-align: center; }
figcaption { font-size: 0.9em; color: #555; }
table { border-collapse: collapse; margin: 1em auto; }
th, td { border: 1px solid #999; padding: 0.3em 0.6em; }
"""

_XHTML_TMPL = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE html>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml" '
    'xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">\n'
    '<head>\n<meta charset="utf-8"/>\n<title>{title}</title>\n'
    '<link rel="stylesheet" type="text/css" href="{css_href}"/>\n</head>\n'
    '<body>\n{body}\n</body>\n</html>\n'
)


@dataclass
class _Chapter:
    idx: int
    slug: str
    title: str
    xhtml_name: str
    body_xhtml: str

    def slug_id(self) -> str:
        return "ch_" + self.slug.replace("-", "_").replace(".", "_")


def _md() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False})
    with contextlib.suppress(Exception):
        md.enable("table")
    return md


def _md_to_xhtml_body(md_text: str) -> str:
    """Render Markdown to a well-formed XHTML fragment (lxml-normalised)."""
    raw = _md().render(md_text)
    if not raw.strip():
        raw = "<p></p>"
    container = html.fragment_fromstring(raw, create_parent="div")
    xml = etree.tostring(container, method="xml", encoding="unicode")
    # Strip the wrapper <div> tags, keep inner content.
    inner = xml[xml.find(">") + 1 : xml.rfind("</div>")]
    return cast(str, inner.strip())


def _first_heading_title(md_text: str, default: str) -> str:
    for line in md_text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip() or default
    return default


def _load_meta(project: BookProject) -> dict[str, Any]:
    if project.book_yaml.exists():
        try:
            data = yaml.safe_load(project.book_yaml.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                return cast(dict[str, Any], data)
        except Exception:
            pass
    return {}


def _slug_title(slug: str) -> str:
    # NNN_slug -> readable-ish fallback
    parts = slug.split("_", 1)
    return parts[1].replace("_", " ") if len(parts) == 2 else slug


def _collect_chapters(md_files: list[Path], lang: str) -> list[_Chapter]:
    chapters: list[_Chapter] = []
    for i, f in enumerate(sorted(md_files), start=1):
        text = f.read_text(encoding="utf-8")
        title = _first_heading_title(text, _slug_title(f.stem))
        chapters.append(
            _Chapter(
                idx=i,
                slug=f.stem,
                title=title,
                xhtml_name=f"chap_{i:03d}_{f.stem}.xhtml",
                body_xhtml=_md_to_xhtml_body(text),
            )
        )
    return chapters


def _nav_xhtml(chapters: list[_Chapter], lang: str, title: str) -> str:
    items = "\n".join(
        f'      <li><a href="{c.xhtml_name}">{_xml_escape(c.title)}</a></li>'
        for c in chapters
    )
    body = (
        '<nav epub:type="toc" id="toc">\n'
        f"  <h1>{_xml_escape(title)}</h1>\n  <ol>\n{items}\n  </ol>\n</nav>"
    )
    return _XHTML_TMPL.format(lang=lang, title="Navigation", css_href="styles/base.css", body=body)


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _opf(
    chapters: list[_Chapter],
    meta: dict[str, Any],
    *,
    lang: str,
    identifier: str,
    asset_items: list[tuple[str, str, str]],
) -> str:
    title = meta.get("title", "Untitled")
    authors = meta.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    creator_xml = "\n".join(
        f'    <dc:creator id="creator{i}">{_xml_escape(str(a))}</dc:creator>'
        for i, a in enumerate(authors)
    )
    manifest_items = [
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="css" href="styles/base.css" media-type="text/css"/>',
    ]
    for c in chapters:
        manifest_items.append(
            f'    <item id="{c.slug_id()}" href="{c.xhtml_name}" '
            'media-type="application/xhtml+xml"/>'
        )
    for item_id, href, mtype in asset_items:
        manifest_items.append(f'    <item id="{item_id}" href="{href}" media-type="{mtype}"/>')

    spine_items = "\n".join(f'    <itemref idref="{c.slug_id()}"/>' for c in chapters)

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="pub-id">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="pub-id">{identifier}</dc:identifier>\n'
        f'    <dc:title>{_xml_escape(str(title))}</dc:title>\n'
        f'    <dc:language>{lang}</dc:language>\n'
        f"{creator_xml}\n"
        f'    <meta property="dcterms:modified">{modified}</meta>\n'
        "  </metadata>\n"
        "  <manifest>\n" + "\n".join(manifest_items) + "\n  </manifest>\n"
        '  <spine>\n' + spine_items + "\n  </spine>\n"
        "</package>\n"
    )


_CONTAINER_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    '  <rootfiles>\n'
    '    <rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/>\n'
    '  </rootfiles>\n</container>\n'
)

_ASSET_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".css": "text/css",
}


def _gather_assets(project: BookProject) -> list[tuple[Path, str, str, str]]:
    """Return (src_path, opf_href, item_id, media_type) for usable image assets."""
    out: list[tuple[Path, str, str, str]] = []
    for sub in ("assets/figures", "assets/images"):
        d = project.root / sub
        if not d.exists():
            continue
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.suffix.lower() in _ASSET_MEDIA and f.suffix.lower() != ".css":
                rel = f.relative_to(project.root / "assets")
                href = f"assets/{rel.as_posix()}"
                item_id = "asset_" + rel.as_posix().replace("/", "_").replace(".", "_")
                out.append((f, href, item_id, _ASSET_MEDIA[f.suffix.lower()]))
    return out


def _render_epub(project: BookProject, md_files: list[Path]) -> tuple[bytes, int, int]:
    """Render an EPUB archive fully in memory for attempt-scoped callers."""
    meta = _load_meta(project)
    lang = str(meta.get("language") or "zh-Hans")
    identifier = str(meta.get("identifier") or "").strip() or f"urn:uuid:{uuid.uuid4()}"
    chapters = _collect_chapters(md_files, lang)
    assets = _gather_assets(project)
    asset_items = [(item_id, href, mtype) for _, href, item_id, mtype in assets]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", _CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/styles/base.css", _CSS, compress_type=zipfile.ZIP_DEFLATED)
        for chapter in chapters:
            xhtml = _XHTML_TMPL.format(
                lang=lang,
                title=_xml_escape(chapter.title),
                css_href="styles/base.css",
                body=chapter.body_xhtml,
            )
            zf.writestr(
                f"OEBPS/{chapter.xhtml_name}", xhtml, compress_type=zipfile.ZIP_DEFLATED
            )
        zf.writestr(
            "OEBPS/nav.xhtml", _nav_xhtml(chapters, lang, str(meta.get("title", "Book"))),
            compress_type=zipfile.ZIP_DEFLATED,
        )
        zf.writestr(
            "OEBPS/content.opf",
            _opf(chapters, meta, lang=lang, identifier=identifier, asset_items=asset_items),
            compress_type=zipfile.ZIP_DEFLATED,
        )
        for source, href, _item_id, _media_type in assets:
            zf.writestr(
                f"OEBPS/{href}", source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED
            )
    return output.getvalue(), len(chapters), len(assets)


def build_epub_bytes(project: BookProject) -> tuple[GateResult, bytes]:
    """Build the full EPUB without writing project output paths."""
    md_files = sorted(project.chapters_final.glob("*.md"))
    if not md_files:
        return GateResult(False, "chapters/final/ is empty — run the chapter gate first"), b""
    payload, chapter_count, asset_count = _render_epub(project, md_files)
    return (
        GateResult(
            True,
            f"built book.epub: {chapter_count} chapters, {asset_count} assets",
            details={"chapters": chapter_count, "assets": asset_count, "path": "output/book.epub"},
        ),
        payload,
    )


def _build(project: BookProject, md_files: list[Path], out_path: Path) -> GateResult:
    if not md_files:
        return GateResult(False, "no chapter Markdown files to build")
    payload, chapter_count, asset_count = _render_epub(project, md_files)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(payload)

    return GateResult(
        True,
        f"built {out_path.name}: {chapter_count} chapters, {asset_count} assets",
        details={"chapters": chapter_count, "assets": asset_count,
                 "path": project.rel(out_path)},
    )


def build_epub(project: BookProject) -> GateResult:
    """Build the full EPUB from chapters/final/ into output/book.epub."""
    md_files = sorted(project.chapters_final.glob("*.md"))
    if not md_files:
        return GateResult(False, "chapters/final/ is empty — run the chapter gate first")
    res = _build(project, md_files, project.book_epub)
    res.write_json(project.root / "output/epub_build.json")
    return res


def build_sample_epub(
    project: BookProject, *, chapter_slugs: list[str] | None = None
) -> GateResult:
    """Build a sample EPUB from selected chapters into preproduction/stage2_sample/."""
    source_dir = project.chapters_final if any(project.chapters_final.glob("*.md")) \
        else project.chapters_translated
    all_md = sorted(source_dir.glob("*.md"))
    if not all_md:
        return GateResult(False, "no translated/final chapters available for a sample")
    if chapter_slugs:
        wanted = set(chapter_slugs)
        md_files = [f for f in all_md if f.stem in wanted]
        if not md_files:
            return GateResult(False, f"none of {chapter_slugs} found in {source_dir.name}")
    else:
        md_files = all_md[:1]
    return _build(project, md_files, project.sample_epub)


def build_sample_epub_bytes(
    project: BookProject, *, chapter_slugs: list[str] | None = None
) -> tuple[GateResult, bytes]:
    """Build the selected sample EPUB fully in memory."""
    source_dir = (
        project.chapters_final
        if any(project.chapters_final.glob("*.md"))
        else project.chapters_translated
    )
    all_markdown = sorted(source_dir.glob("*.md"))
    if not all_markdown:
        return GateResult(False, "no translated/final chapters available for a sample"), b""
    if chapter_slugs:
        wanted = set(chapter_slugs)
        markdown = [path for path in all_markdown if path.stem in wanted]
        if not markdown:
            return GateResult(False, f"none of {chapter_slugs} found in {source_dir.name}"), b""
    else:
        markdown = all_markdown[:1]
    payload, chapter_count, asset_count = _render_epub(project, markdown)
    return (
        GateResult(
            True,
            f"built sample_book.epub: {chapter_count} chapters, {asset_count} assets",
            details={
                "chapters": chapter_count,
                "assets": asset_count,
                "path": "preproduction/stage2_sample/sample_book.epub",
            },
        ),
        payload,
    )
