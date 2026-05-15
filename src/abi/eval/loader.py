"""Load artifacts from a prior ABI run into memory for evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from abi.types.book import Book
from abi.types.glossary import Glossary
from abi.types.survey import BookOverview, StyleGuide
from abi.types.translation import TranslationUnit


@dataclass(frozen=True)
class AbiRunArtifacts:
    book: Book
    units: dict[str, TranslationUnit]
    glossary: Glossary
    overview: BookOverview | None
    style_guide: StyleGuide | None


def load_abi_run(run_dir: Path) -> AbiRunArtifacts:
    """Read the persisted book, survey artifacts, and per-paragraph units.

    Raises :class:`FileNotFoundError` if required files are missing — eval
    cannot proceed without the book and at least one translation unit.
    """
    book_path = run_dir / "ir" / "book.json"
    if not book_path.exists():
        raise FileNotFoundError(f"missing {book_path}")
    book = Book.model_validate_json(book_path.read_text(encoding="utf-8"))

    glossary_path = run_dir / "survey" / "glossary.json"
    if glossary_path.exists():
        glossary = Glossary.model_validate_json(
            glossary_path.read_text(encoding="utf-8")
        )
    else:
        glossary = Glossary(
            book_id=book.meta.book_id, target_language="zh", entries=[]
        )

    overview: BookOverview | None = None
    overview_path = run_dir / "survey" / "overview.json"
    if overview_path.exists():
        overview = BookOverview.model_validate_json(
            overview_path.read_text(encoding="utf-8")
        )

    style_guide: StyleGuide | None = None
    sg_path = run_dir / "survey" / "style-guide.json"
    if sg_path.exists():
        style_guide = StyleGuide.model_validate_json(
            sg_path.read_text(encoding="utf-8")
        )

    units: dict[str, TranslationUnit] = {}
    paragraphs_dir = run_dir / "translate" / "paragraphs"
    if paragraphs_dir.exists():
        for f in sorted(paragraphs_dir.glob("*.json")):
            try:
                unit = TranslationUnit.model_validate_json(
                    f.read_text(encoding="utf-8")
                )
            except Exception:
                continue
            units[unit.paragraph_id] = unit
    if not units:
        raise FileNotFoundError(
            f"no translation units found under {paragraphs_dir}; cannot evaluate"
        )

    return AbiRunArtifacts(
        book=book,
        units=units,
        glossary=glossary,
        overview=overview,
        style_guide=style_guide,
    )
