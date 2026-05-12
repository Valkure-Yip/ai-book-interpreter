"""TXT parser → list of RawBlock.

Heuristics for chapter detection are documented in design-docs/ingest-design.md.
"""

from __future__ import annotations

import re
from pathlib import Path

import chardet

from abi.ir.blocks import RawBlock

CHAPTER_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"^\s*PART\s+[IVXLC0-9]+\b.*", re.IGNORECASE), 1),
    (re.compile(r"^\s*第[一二三四五六七八九十百0-9]+部分?\b.*"), 1),
    (re.compile(r"^\s*Chapter\s+[0-9]+\b.*", re.IGNORECASE), 2),
    (re.compile(r"^\s*第[一二三四五六七八九十百0-9]+章\b.*"), 2),
    (re.compile(r"^\s*([0-9]+\.){1,3}\s+\S.*"), 3),  # 1. , 1.1 , 1.1.1
    (re.compile(r"^\s*[0-9]+\s+[A-Z][^\.\n]{2,60}$"), 2),  # "5 The X"
]

ALL_CAPS_SHORT = re.compile(r"^[A-Z][A-Z0-9 \-:,'`]{2,60}$")


def _detect_encoding(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    result = chardet.detect(data[:4096])
    encoding = result.get("encoding") or "utf-8"
    return encoding


def _is_heading_line(line: str, *, prev_blank: bool, next_blank: bool) -> int | None:
    """Return heading level (1-based) if line is a heading, else None."""
    stripped = line.strip()
    if not stripped:
        return None

    for pat, level in CHAPTER_PATTERNS:
        if pat.match(stripped):
            return level

    # All-caps short line surrounded by blanks
    if prev_blank and next_blank and ALL_CAPS_SHORT.match(stripped):
        return 2

    return None


def parse_txt(path: Path) -> list[RawBlock]:
    """Parse a TXT file into a flat list of RawBlocks (heading + prose + code/quote)."""
    data = path.read_bytes()
    encoding = _detect_encoding(data)
    text = data.decode(encoding, errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    lines = text.split("\n")
    blocks: list[RawBlock] = []
    buf: list[str] = []
    buf_is_code = False

    def flush() -> None:
        nonlocal buf, buf_is_code
        if not buf:
            return
        joined = "\n".join(buf) if buf_is_code else " ".join(b.strip() for b in buf).strip()
        if joined:
            blocks.append(RawBlock(kind="code" if buf_is_code else "prose", text=joined))
        buf = []
        buf_is_code = False

    for i, line in enumerate(lines):
        prev_blank = i == 0 or not lines[i - 1].strip()
        next_blank = i + 1 >= len(lines) or not lines[i + 1].strip()
        stripped = line.strip()

        if not stripped:
            flush()
            continue

        h_level = _is_heading_line(line, prev_blank=prev_blank, next_blank=next_blank)
        if h_level is not None and not buf:
            blocks.append(RawBlock(kind="heading", text=stripped, level=h_level))
            continue

        # Heuristic code: line starts with 4+ spaces and has many non-letters
        is_code_line = line.startswith("    ") and bool(stripped)
        if is_code_line:
            if buf and not buf_is_code:
                flush()
            buf_is_code = True
            buf.append(line)
        else:
            if buf_is_code:
                flush()
            buf.append(line)

    flush()
    return blocks
