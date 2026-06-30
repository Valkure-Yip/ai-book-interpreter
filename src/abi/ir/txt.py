"""TXT parser → list of RawBlock.

Heuristics for chapter detection are documented in design-docs/ingest-design.md.

Pass 0 heading recognition — from highest priority to lowest:

1. **Explicit patterns** (``CHAPTER_PATTERNS``): ``PART IV``, ``Chapter 3``,
   ``第二章``, numbered ``1.2 Foo``, roman-numeral standalone ``III.``.
2. **Two-line headings**: a short *marker line* (roman numeral, ``Chapter N``,
   or digit) immediately followed by an all-caps or title-case line that is
   separated from the surrounding prose by blank lines — fused into a single
   heading.  Handles classic Gutenberg patterns like ``I.\nBOURGEOIS AND
   PROLETARIANS``.
3. **All-caps standalone line** (``ALL_CAPS_LINE``): ≤120 characters, surrounded
   by blank lines.
4. **Common heading words** (``HEADING_WORDS``): ``Preamble``, ``Introduction``,
   ``Epilogue``, etc., standalone between blank lines.
"""

from __future__ import annotations

import re
from pathlib import Path

import chardet

from abi.ir.blocks import RawBlock

CHAPTER_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"^\s*PART\s+[IVXLC0-9]+\b.*", re.IGNORECASE), 1),
    (re.compile(r"^\s*第[一二三四五六七八九十百0-9]+部分?\b.*"), 1),
    (re.compile(r"^\s*Chapter\s+[0-9IVXLC]+\b.*", re.IGNORECASE), 2),
    (re.compile(r"^\s*第[一二三四五六七八九十百0-9]+章\b.*"), 2),
    # Roman numeral alone on a line: I. / II. / XIV. (with optional trailing text)
    (re.compile(r"^\s*[IVXLC]+\.\s*$"), 1),
    (re.compile(r"^\s*([0-9]+\.){1,3}\s+\S.*"), 3),  # 1. , 1.1 , 1.1.1
    (re.compile(r"^\s*[0-9]+\s+[A-Z][^\.\n]{2,60}$"), 2),  # "5 The X"
]

# All-caps line up to 120 characters (relaxed from 60 for long chapter titles).
ALL_CAPS_LINE = re.compile(r"^[A-Z][A-Z0-9 \-:,;'`\u2014\u2013/()&]{1,119}$")

# Standalone words commonly used as section headings (case-insensitive).
HEADING_WORDS: frozenset[str] = frozenset({
    "preamble", "preface", "foreword", "introduction", "prologue",
    "epilogue", "conclusion", "afterword", "postscript",
    "appendix", "glossary", "bibliography", "acknowledgements",
    "acknowledgments", "dedication", "author's note", "authors' note",
    "contents", "notes",
})

# Pattern for the short *marker line* that precedes a title in a two-line heading.
# Matches roman numerals (I. / XIV.), arabic digits (1 / 12.), or "Chapter N".
_MARKER_RE = re.compile(
    r"^\s*(?:[IVXLC]+\.?|[0-9]+\.?|Chapter\s+[0-9IVXLC]+\.?)\s*$",
    re.IGNORECASE,
)


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

    # All-caps standalone line between blank lines.
    if prev_blank and next_blank and ALL_CAPS_LINE.match(stripped):
        return 2

    # Common heading words (case-insensitive) standalone between blanks.
    if prev_blank and next_blank and stripped.lower() in HEADING_WORDS:
        return 2

    return None


def _try_two_line_heading(
    lines: list[str], i: int
) -> tuple[int, str] | None:
    """Detect a two-line heading starting at ``lines[i]``.

    Returns ``(level, fused_text)`` or ``None``.  Requires::

        <blank line(s)>
        MARKER LINE      (roman num / arabic num / "Chapter N")
        TITLE LINE        (all-caps or title-case, non-empty, ≤120 chars)
        <blank line(s)>

    The marker must be preceded by a blank and the title must be followed by a
    blank (or EOF).  The title may itself span 1-2 lines (for long titles that
    wrap, like ``POSITION OF THE COMMUNISTS IN RELATION TO THE VARIOUS\n
    EXISTING OPPOSITION PARTIES``).
    """
    if i + 1 >= len(lines):
        return None
    marker = lines[i].strip()
    if not _MARKER_RE.match(marker):
        return None
    prev_blank = i == 0 or not lines[i - 1].strip()
    if not prev_blank:
        return None

    # Collect up to 2 continuation title lines.
    title_parts: list[str] = []
    j = i + 1
    while j < len(lines) and len(title_parts) < 2:
        t = lines[j].strip()
        if not t:
            break
        # Each title-continuation line must be "heading-like": all-caps, or
        # title-case with no sentence-ending punctuation.
        is_caps = bool(ALL_CAPS_LINE.match(t))
        is_titleish = t[0].isupper() and not t.endswith((".", "?", "!", ",", ";"))
        if not (is_caps or is_titleish):
            break
        title_parts.append(t)
        j += 1

    if not title_parts:
        return None

    # The line after the last title part must be blank (or EOF).
    if j < len(lines) and lines[j].strip():
        return None

    fused = marker.rstrip(".") + ". " + " ".join(title_parts)
    level = 1 if _MARKER_RE.match(marker) else 2
    return level, fused.strip()


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
    skip_until = -1  # skip lines consumed by two-line heading

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
        if i < skip_until:
            continue

        prev_blank = i == 0 or not lines[i - 1].strip()
        next_blank = i + 1 >= len(lines) or not lines[i + 1].strip()
        stripped = line.strip()

        if not stripped:
            flush()
            continue

        # --- two-line heading detection (before single-line) ---
        if not buf:
            two = _try_two_line_heading(lines, i)
            if two is not None:
                flush()
                level, fused = two
                blocks.append(RawBlock(kind="heading", text=fused, level=level))
                # skip the consumed lines (marker + title parts)
                j = i + 1
                while j < len(lines) and lines[j].strip():
                    j += 1
                skip_until = j
                continue

        # --- single-line heading detection ---
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
