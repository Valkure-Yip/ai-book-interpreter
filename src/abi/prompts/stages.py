"""Staged prompt chain (PDBT 00->19) ported into ABI's prompt registry.

Each :class:`StageSpec` binds a numbered stage to its prompt template, the
``Status`` it produces on success, an optional gate name, the tool profile the
agent gets, and an iteration cap. The orchestrator walks ``STAGE_SEQUENCE``.

Prompts are Jinja2 templates parameterized by ``{source_lang, target_lang,
source_target, ...}`` so one chain serves any language pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from abi.project.state import Status

STAGES_DIR = Path(__file__).resolve().parent / "actions"

ToolProfile = Literal["authoring", "production", "review"]


@dataclass(frozen=True)
class StageSpec:
    stage_id: str           # e.g. "07_translate_chapters"
    title: str
    template: str           # template filename under stages/
    produces: Status        # status set on successful completion
    tool_profile: ToolProfile = "authoring"
    gate: str | None = None  # gate name recorded in state.gates
    max_iterations: int = 40


# The macro pipeline. Mirrors en-zh-Hans/prompts/00_orchestrator execution order.
STAGE_SEQUENCE: list[StageSpec] = [
    StageSpec("01_ingest_clean", "Ingest & clean source", "01_ingest_clean.md.j2",
              Status.SOURCE_INGESTED, "authoring", max_iterations=20),
    StageSpec("02_split", "Split into chapters", "02_split.md.j2",
              Status.SOURCE_SPLIT, "authoring", max_iterations=15),
    StageSpec("03_global_translation_research", "Global translation research",
              "03_global_research.md.j2", Status.GLOBAL_RESEARCH_DONE, "authoring"),
    StageSpec("04_book_specific_research", "Book-specific research + style profile",
              "04_book_research.md.j2", Status.BOOK_RESEARCH_DONE, "authoring"),
    StageSpec("05_pretranslation_trials", "A/B/C/D pretranslation trials",
              "05_pretranslation_trials.md.j2", Status.PRETRANSLATION_PASS,
              "authoring", gate="pretranslation", max_iterations=50),
    StageSpec("06_glossary_style", "Glossary + style guide",
              "06_glossary_style.md.j2", Status.GLOSSARY_STYLE_DONE, "authoring"),
    StageSpec("07_translate_chapters", "Translate chapters (slim calls)",
              "07_translate_chapters.md.j2", Status.TRANSLATED, "authoring",
              max_iterations=120),
    StageSpec("08a_chapter_post_translation_control", "Per-chapter post-translation control",
              "08a_chapter_control.md.j2", Status.CHAPTER_POST_CONTROL_PASS,
              "authoring", gate="chapter_controls", max_iterations=120),
    StageSpec("11_chapter_quality_gate", "Chapter reviews + quality gate",
              "11_chapter_gate.md.j2", Status.CHAPTER_GATES_PASS, "authoring",
              gate="chapter_gates", max_iterations=120),
    StageSpec("13_preproduction_stage1_spec", "Preproduction spec",
              "13_preproduction_spec.md.j2", Status.PREPRODUCTION_SPEC_DONE, "production"),
    StageSpec("14_preproduction_stage2_sample", "Sample EPUB",
              "14_preproduction_sample.md.j2", Status.PREPRODUCTION_SAMPLE_PASS,
              "production", gate="sample", max_iterations=40),
    StageSpec("15_full_book_production", "Full EPUB build",
              "15_full_build.md.j2", Status.EPUB_BUILT, "production",
              gate="epub_build", max_iterations=40),
    StageSpec("16a_stratified_random_spotcheck", "Stratified random spot-check",
              "16a_random_spotcheck.md.j2", Status.RANDOM_SPOTCHECK_PASS, "review",
              gate="random_spotcheck", max_iterations=60),
    StageSpec("16_independent_review_agents", "Independent dual review",
              "16_independent_review.md.j2", Status.INDEPENDENT_REVIEW_PASS, "review",
              gate="independent_review", max_iterations=40),
    StageSpec("18a_release_versioning", "Versioned release",
              "18a_release.md.j2", Status.RELEASE_PASS, "production",
              gate="release", max_iterations=30),
    StageSpec("18_final_output", "Final output manifest",
              "18_final_output.md.j2", Status.FINAL_OUTPUT_PASS, "production"),
    StageSpec("19_retrospective_template_update", "Retrospective",
              "19_retrospective.md.j2", Status.RETROSPECTIVE_DONE, "authoring"),
]

STAGE_BY_PRODUCES: dict[Status, StageSpec] = {s.produces: s for s in STAGE_SEQUENCE}


class StagePromptRegistry:
    def __init__(self, root: Path = STAGES_DIR) -> None:
        self._env = Environment(
            loader=FileSystemLoader(str(root)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def system_prompt(self, **vars: Any) -> str:
        return self._env.get_template("_system.md.j2").render(**vars)

    def stage_prompt(self, spec: StageSpec, **vars: Any) -> str:
        return self._env.get_template(spec.template).render(stage=spec, **vars)


_REGISTRY: StagePromptRegistry | None = None


def get_stage_registry() -> StagePromptRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = StagePromptRegistry()
    return _REGISTRY
