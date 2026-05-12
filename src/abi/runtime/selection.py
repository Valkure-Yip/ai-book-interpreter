"""Parse and apply ``--chapters`` selection over a Book's top-level TOC.

The selection is expressed as a 1-indexed compact range list (e.g. ``"1,3-5,8"``)
and is matched against the **post-filter** ``book.toc`` (i.e. after non-content
sections like Index / Guide are already stripped).
"""

from __future__ import annotations

from abi.types.book import Book, Section


def parse_chapter_selection(spec: str) -> set[int]:
    """Parse ``"1,3-5,8"`` → ``{1, 3, 4, 5, 8}``.

    Raises ``ValueError`` on malformed input. Empty / whitespace-only spec
    returns the empty set (caller should treat that as "no filtering").
    """
    spec = (spec or "").strip()
    if not spec:
        return set()
    out: set[int] = set()
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        if "-" in token:
            lo_s, hi_s = token.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            if lo < 1 or hi < lo:
                raise ValueError(f"invalid range {token!r} in chapter selection")
            out.update(range(lo, hi + 1))
        else:
            n = int(token)
            if n < 1:
                raise ValueError(f"chapter index must be >= 1, got {n}")
            out.add(n)
    return out


def filter_book_by_chapters(book: Book, selection: set[int]) -> tuple[Book, list[str]]:
    """Return a Book containing only the selected top-level toc entries.

    Selection is 1-indexed against ``book.toc``. Out-of-range indices are
    reported as warnings (not errors) so a "1,3-5,99" spec on a 4-chapter
    book degrades gracefully.

    When ``selection`` is empty, returns the book unchanged with no warnings.
    """
    if not selection:
        return book, []

    warnings: list[str] = []
    n_toc = len(book.toc)
    valid = {i for i in selection if 1 <= i <= n_toc}
    invalid = sorted(selection - valid)
    if invalid:
        warnings.append(
            f"chapter_selection_out_of_range:{invalid}(book_has_{n_toc}_top_sections)"
        )

    if not valid:
        # User selected only out-of-range indices. Don't silently translate
        # nothing; surface as a warning and keep the full TOC. (Caller can
        # observe the warning and decide whether to abort.)
        warnings.append("chapter_selection_yielded_empty_kept_full_toc")
        return book, warnings

    kept: list[Section] = []
    dropped_headings: list[str] = []
    for i, section in enumerate(book.toc, start=1):
        if i in valid:
            kept.append(section)
        else:
            dropped_headings.append(section.heading)

    warnings.append(
        f"chapter_selection_applied:kept_{len(kept)}_of_{n_toc}"
    )
    if dropped_headings:
        warnings.append(
            "chapter_selection_dropped:" + "|".join(dropped_headings[:10])
        )

    new_book = book.model_copy(update={"toc": kept})
    return new_book, warnings
