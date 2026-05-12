"""EPUB parser → list of RawBlock per spine doc."""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag
from ebooklib import ITEM_DOCUMENT, epub

from abi.ir.blocks import RawBlock


def _text_of(el: Tag) -> str:
    return " ".join(el.get_text(" ", strip=True).split())


def _process_node(node: Tag, out: list[RawBlock]) -> None:
    """Recursively turn an HTML subtree into RawBlocks (block-level only)."""
    name = (node.name or "").lower()

    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(name[1])
        text = _text_of(node)
        if text:
            out.append(RawBlock(kind="heading", text=text, level=level))
        return

    if name == "p":
        text = _text_of(node)
        if text:
            out.append(RawBlock(kind="prose", text=text))
        return

    if name == "blockquote":
        text = _text_of(node)
        if text:
            out.append(RawBlock(kind="quote", text=text))
        return

    if name in {"pre", "code"} and (name == "pre" or not node.parent or node.parent.name == "body"):
        text = node.get_text("\n", strip=False)
        if text.strip():
            out.append(RawBlock(kind="code", text=text))
        return

    if name == "li":
        text = _text_of(node)
        if text:
            out.append(RawBlock(kind="list_item", text=text))
        return

    if name in {"figcaption"}:
        text = _text_of(node)
        if text:
            out.append(RawBlock(kind="figure_caption", text=text))
        return

    # Recurse into children for container nodes (div, section, article, body, ul, ol, etc.)
    for child in node.children:
        if isinstance(child, NavigableString):
            continue
        if isinstance(child, Tag):
            _process_node(child, out)


def parse_epub(path: Path) -> tuple[list[RawBlock], dict[str, str]]:
    """Return (blocks, metadata)."""
    book = epub.read_epub(str(path))

    metadata: dict[str, str] = {}
    titles = book.get_metadata("DC", "title")
    if titles:
        metadata["title"] = titles[0][0]
    creators = book.get_metadata("DC", "creator")
    if creators:
        metadata["authors"] = ", ".join(c[0] for c in creators)
    langs = book.get_metadata("DC", "language")
    if langs:
        metadata["language"] = langs[0][0]

    all_blocks: list[RawBlock] = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        content = item.get_content()
        soup = BeautifulSoup(content, "lxml")
        body = soup.find("body") or soup
        if isinstance(body, Tag):
            _process_node(body, all_blocks)

    return all_blocks, metadata
