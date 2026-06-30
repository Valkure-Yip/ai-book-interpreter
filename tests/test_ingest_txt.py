"""End-to-end test for TXT ingest."""

from __future__ import annotations

from pathlib import Path

from abi.ir import ingest
from abi.ir.txt import _is_heading_line, _try_two_line_heading

FIXTURE = Path(__file__).parent / "fixtures" / "short_book.txt"
MANIFESTO = Path(__file__).parent / "fixtures" / "the_communist_manifesto.txt"


def test_ingest_txt_produces_book() -> None:
    book, warnings = ingest(FIXTURE)
    assert book.meta.source_format == "txt"
    assert book.meta.title  # non-empty (stem or detected)
    assert len(book.toc) >= 1
    assert warnings == []  # this fixture has explicit chapter headings

    paragraphs = book.iter_paragraphs()
    assert len(paragraphs) >= 5

    # Positions are strictly increasing and unique
    positions = [p.position for p in paragraphs]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)

    # paragraph IDs are unique
    ids = [p.paragraph_id for p in paragraphs]
    assert len(set(ids)) == len(ids)

    # First section should be Chapter 1 (or a parent containing it)
    titles = [s.heading for s in book.iter_sections()]
    assert any("Chapter 1" in t for t in titles)


def test_ingest_txt_no_chapters_warns(tmp_path: Path) -> None:
    bare = tmp_path / "bare.txt"
    bare.write_text(
        "Just one paragraph of text.\n\nAnd another paragraph.\n",
        encoding="utf-8",
    )
    book, warnings = ingest(bare)
    assert "no_explicit_chapter_detected" in warnings
    assert book.iter_paragraphs()


# --- Pass 0 heading enhancements ---

def test_roman_numeral_standalone_is_heading() -> None:
    assert _is_heading_line("I.", prev_blank=True, next_blank=True) == 1
    assert _is_heading_line("XIV.", prev_blank=True, next_blank=True) == 1
    assert _is_heading_line("  III. ", prev_blank=True, next_blank=True) == 1
    # Must not match roman letters in prose context
    assert _is_heading_line("I.", prev_blank=False, next_blank=True) == 1  # pattern match, not caps


def test_heading_words_detected() -> None:
    assert _is_heading_line("Preamble", prev_blank=True, next_blank=True) == 2
    assert _is_heading_line("Introduction", prev_blank=True, next_blank=True) == 2
    assert _is_heading_line("Epilogue", prev_blank=True, next_blank=True) == 2
    assert _is_heading_line("Contents", prev_blank=True, next_blank=True) == 2
    # Not a heading word in prose
    assert _is_heading_line("Preamble", prev_blank=False, next_blank=True) is None


def test_all_caps_line_120_chars() -> None:
    long_title = "POSITION OF THE COMMUNISTS IN RELATION TO THE VARIOUS EXISTING OPPOSITION PARTIES"
    assert len(long_title) > 60
    assert _is_heading_line(long_title, prev_blank=True, next_blank=True) == 2


def test_two_line_heading_roman_plus_caps() -> None:
    lines = ["", "I.", "BOURGEOIS AND PROLETARIANS", "", "The history of all..."]
    result = _try_two_line_heading(lines, 1)
    assert result is not None
    level, text = result
    assert level == 1
    assert "I." in text
    assert "BOURGEOIS AND PROLETARIANS" in text


def test_two_line_heading_multiline_title() -> None:
    lines = [
        "",
        "IV.",
        "POSITION OF THE COMMUNISTS IN RELATION TO THE VARIOUS EXISTING",
        "OPPOSITION PARTIES",
        "",
        "Section II has made clear...",
    ]
    result = _try_two_line_heading(lines, 1)
    assert result is not None
    level, text = result
    assert level == 1
    assert "OPPOSITION PARTIES" in text


def test_two_line_heading_not_triggered_without_blank() -> None:
    lines = ["Some prose before.", "IV.", "TITLE HERE", "", "Next paragraph."]
    result = _try_two_line_heading(lines, 1)
    assert result is None  # no blank before marker


def test_communist_manifesto_splits_into_chapters() -> None:
    """The Communist Manifesto must produce ≥4 top-level sections (I-IV)."""
    book, warnings = ingest(MANIFESTO)
    assert "no_explicit_chapter_detected" not in warnings
    titles = [s.heading for s in book.toc]
    assert len(book.toc) >= 4, f"expected ≥4 sections, got {len(book.toc)}: {titles}"
    # The four main chapters must appear
    joined = " | ".join(titles)
    assert "BOURGEOIS AND PROLETARIANS" in joined
    assert "PROLETARIANS AND COMMUNISTS" in joined
    assert "SOCIALIST AND COMMUNIST LITERATURE" in joined
    assert "POSITION OF THE COMMUNISTS" in joined


def test_chapter_with_roman_numeral_in_pattern() -> None:
    assert _is_heading_line("Chapter IV", prev_blank=True, next_blank=True) == 2
    assert _is_heading_line("Chapter XIV", prev_blank=True, next_blank=True) == 2
