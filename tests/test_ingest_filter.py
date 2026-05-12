"""Verify that ``ingest`` drops universally non-content top-level sections.

These tests synthesize minimal TXT inputs to exercise the filter without
depending on a real EPUB fixture (which would be slow and version-fragile).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abi.ir import ingest


def _write(tmp: Path, name: str, body: str) -> Path:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return p


def test_filters_known_non_content_top_level_headings(tmp_path: Path) -> None:
    # The TXT parser recognises "Chapter N" as a heading (level 2) and the
    # all-caps short pattern as a heading too. So we can craft a synthetic
    # book whose top-level sections include both real chapters and junk like
    # 'INDEX'.
    body = (
        "Chapter 1 The Start\n"
        "\n"
        "This is the first paragraph of real content.\n"
        "\n"
        "Chapter 2 The Middle\n"
        "\n"
        "Another paragraph of body text in chapter two.\n"
        "\n"
        "INDEX\n"
        "\n"
        "alpha, 1\n"
        "\n"
        "beta, 2\n"
        "\n"
        "CONTENTS\n"
        "\n"
        "Chapter 1\n"
        "Chapter 2\n"
    )
    p = _write(tmp_path, "book.txt", body)
    book, warnings = ingest(p)

    headings = [s.heading for s in book.toc]
    assert "Chapter 1 The Start" in headings
    assert "Chapter 2 The Middle" in headings
    assert "INDEX" not in headings
    assert "CONTENTS" not in headings

    # Each drop emits a structured warning so the run record is auditable.
    skipped = [w for w in warnings if w.startswith("skipped_non_content_section:")]
    assert "skipped_non_content_section:INDEX" in skipped
    assert "skipped_non_content_section:CONTENTS" in skipped


def test_does_not_filter_nested_index_subsection(tmp_path: Path) -> None:
    """A child section named 'Index' under a kept parent must be retained."""
    body = (
        "Chapter 1 Real Chapter\n"
        "\n"
        "Body text here.\n"
        "\n"
        "1.1. Index of Concepts\n"
        "\n"
        "This subsection is genuine prose discussing an index of concepts.\n"
    )
    p = _write(tmp_path, "book.txt", body)
    book, _warnings = ingest(p)
    # The chapter is kept; child sub-section is unaffected by the top-level filter.
    top_headings = [s.heading for s in book.toc]
    assert "Chapter 1 Real Chapter" in top_headings
    nested_headings = {s.heading for s in book.iter_sections()}
    assert any("Index of Concepts" in h for h in nested_headings)


def test_case_and_whitespace_insensitive(tmp_path: Path) -> None:
    body = (
        "Chapter 1 Start\n"
        "\n"
        "Real content paragraph.\n"
        "\n"
        "TABLE OF CONTENTS\n"
        "\n"
        "Chapter 1 ........ 1\n"
    )
    p = _write(tmp_path, "book.txt", body)
    book, _ = ingest(p)
    headings = [s.heading for s in book.toc]
    assert "TABLE OF CONTENTS" not in headings
    assert "Chapter 1 Start" in headings


def test_real_chapters_untouched_when_no_junk_present(tmp_path: Path) -> None:
    body = (
        "Chapter 1 Alpha\n"
        "\n"
        "Paragraph A.\n"
        "\n"
        "Chapter 2 Beta\n"
        "\n"
        "Paragraph B.\n"
    )
    p = _write(tmp_path, "book.txt", body)
    book, warnings = ingest(p)
    assert [s.heading for s in book.toc] == ["Chapter 1 Alpha", "Chapter 2 Beta"]
    assert not any(w.startswith("skipped_non_content_section:") for w in warnings)


@pytest.mark.parametrize("heading", ["Cover", "Guide", "Index", "Imprint", "Colophon"])
def test_each_canonical_non_content_heading_is_dropped(tmp_path: Path, heading: str) -> None:
    # Use all-caps form so the TXT parser detects it as a heading.
    body = (
        "Chapter 1 Real\n"
        "\n"
        "Real content.\n"
        "\n"
        f"{heading.upper()}\n"
        "\n"
        "Some scaffolding content.\n"
    )
    p = _write(tmp_path, f"book_{heading}.txt", body)
    book, _ = ingest(p)
    assert heading.upper() not in [s.heading for s in book.toc]
