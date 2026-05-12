"""Stability tests for ID functions (D6). These tests MUST stay green."""

from __future__ import annotations

import pytest

from abi.types.ids import book_id, paragraph_id, section_id


class TestParagraphId:
    def test_pure_function(self) -> None:
        assert paragraph_id("hello", 0) == paragraph_id("hello", 0)

    def test_format(self) -> None:
        pid = paragraph_id("hello world", 42)
        assert len(pid) == 17  # 10 + 1 + 6
        prefix, suffix = pid.split("-")
        assert len(prefix) == 10
        assert suffix == "000042"

    def test_whitespace_normalized(self) -> None:
        a = paragraph_id("hello  world", 1)
        b = paragraph_id("hello\tworld", 1)
        c = paragraph_id("hello\nworld", 1)
        d = paragraph_id("  hello world  ", 1)
        assert a == b == c == d

    def test_nfkc_normalized(self) -> None:
        # Half-width vs full-width digits should be normalized.
        a = paragraph_id("section 1", 0)
        b = paragraph_id("section １", 0)  # full-width '1'
        assert a == b

    def test_different_position_different_id(self) -> None:
        assert paragraph_id("same text", 1) != paragraph_id("same text", 2)

    def test_different_text_different_id(self) -> None:
        assert paragraph_id("a", 0) != paragraph_id("b", 0)

    def test_negative_position_rejected(self) -> None:
        with pytest.raises(ValueError):
            paragraph_id("x", -1)

    def test_unicode_safe(self) -> None:
        pid = paragraph_id("具身认知是一种理论", 7)
        assert len(pid) == 17

    @pytest.mark.parametrize(
        ("text", "position", "expected"),
        [
            ("hello", 0, "aaf4c61ddc-000000"),
            ("hello world", 0, "2aae6c35c9-000000"),
            ("", 0, "da39a3ee5e-000000"),
        ],
    )
    def test_golden_ids(self, text: str, position: int, expected: str) -> None:
        """If these break, every existing run on disk is invalidated. Don't change."""
        assert paragraph_id(text, position) == expected


class TestSectionId:
    def test_pure_function(self) -> None:
        a = section_id(["Part I", "Chapter 1"])
        b = section_id(["Part I", "Chapter 1"])
        assert a == b
        assert len(a) == 12

    def test_trail_matters(self) -> None:
        a = section_id(["Part I", "Chapter 1"])
        b = section_id(["Part II", "Chapter 1"])
        assert a != b


class TestBookId:
    def test_pure_function(self) -> None:
        data = b"Once upon a time..."
        assert book_id(data) == book_id(data)
        assert len(book_id(data)) == 12

    def test_different_bytes_different_id(self) -> None:
        assert book_id(b"a") != book_id(b"b")
