"""End-to-end test for TXT ingest."""

from __future__ import annotations

from pathlib import Path

from abi.ir import ingest

FIXTURE = Path(__file__).parent / "fixtures" / "short_book.txt"


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
