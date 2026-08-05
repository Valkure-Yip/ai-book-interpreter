"""Capability-addressed prompt registry for isolated Action harnesses."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from abi.actions.builtins.inputs import ChapterBatchInput
from abi.types._base import FrozenModel

ACTIONS_DIR = Path(__file__).resolve().parent / "actions"

_TEMPLATES = {
    "source.ingest": "01_ingest_clean.md.j2",
    "source.split": "02_split.md.j2",
    "research.global": "03_global_research.md.j2",
    "research.book": "04_book_research.md.j2",
    "translation.trial": "05_pretranslation_trials.md.j2",
    "glossary.prepare": "06_glossary_style.md.j2",
    "chapter.translate": "07_translate_chapters.md.j2",
    "chapter.control": "08a_chapter_control.md.j2",
    "chapter.review": "11_chapter_gate.md.j2",
    "preproduction.spec": "13_preproduction_spec.md.j2",
    "preproduction.sample": "14_preproduction_sample.md.j2",
    "epub.build": "15_full_build.md.j2",
    "review.spotcheck": "16a_random_spotcheck.md.j2",
    "review.independent": "16_independent_review.md.j2",
    "release.prepare": "18a_release.md.j2",
    "output.finalize": "18_final_output.md.j2",
    "retrospective.capture": "19_retrospective.md.j2",
}


class ActionPromptSnapshot(FrozenModel):
    """Typed, bounded artifact content supplied to one Action prompt."""

    source_lang: str = "source"
    target_lang: str = "target"
    source_target: str = "source-target"
    publication_mode: str = "public_domain"
    book_slug: str = "book"
    profile: str | None = None
    source_text: str = ""
    style_rules: tuple[str, ...] = ()
    matched_terms: tuple[str, ...] = ()


class ActionPromptRegistry:
    """Render the existing ABI prompt content by registered capability."""

    def __init__(self, root: Path = ACTIONS_DIR) -> None:
        self._env = Environment(
            loader=FileSystemLoader(str(root)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def system_prompt(self, snapshot: ActionPromptSnapshot) -> str:
        return self._env.get_template("_system.md.j2").render(**snapshot.model_dump())

    def render(
        self,
        capability: str,
        parameters: FrozenModel,
        snapshot: ActionPromptSnapshot,
    ) -> str:
        try:
            template = _TEMPLATES[capability]
        except KeyError as exc:
            raise KeyError(
                f"unknown prompt capability {capability}; register a template before execution"
            ) from exc
        variables = snapshot.model_dump()
        variables["parameters"] = parameters.model_dump(mode="json")
        rendered = self._env.get_template(template).render(**variables)
        if capability != "chapter.translate":
            return rendered
        if not isinstance(parameters, ChapterBatchInput):
            raise TypeError("chapter.translate requires parsed ChapterBatchInput parameters")
        if not snapshot.source_text:
            raise ValueError("chapter.translate requires source text in its bounded snapshot")
        if not 5 <= len(snapshot.style_rules) <= 8:
            raise ValueError("chapter.translate requires 5-8 style rules")
        chapters = ", ".join(parameters.chapters)
        rules = "\n".join(f"- {rule}" for rule in snapshot.style_rules)
        terms = "\n".join(f"- {term}" for term in snapshot.matched_terms) or "- (none)"
        return (
            f"{rendered}\n\n## Authorized translation envelope\n"
            f"Chapters: {chapters}\n\n"
            f"### Source\n{snapshot.source_text}\n\n"
            f"### Style rules (exactly {len(snapshot.style_rules)})\n{rules}\n\n"
            f"### Matched terms only\n{terms}\n\n"
            "Write only the translation for the authorized chapter paths."
        )
