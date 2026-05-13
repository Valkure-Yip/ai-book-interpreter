"""Load Jinja2 prompt templates from disk, with version pinning per agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

PROMPTS_DIR = Path(__file__).parent / "templates"

# Default version per agent. Bump here when a new template is shipped.
DEFAULT_VERSIONS: dict[str, str] = {
    "chapter_summarizer": "v1",
    "book_synthesizer": "v1",
    "glossary_arbiter": "v1",
    "style_guide_deriver": "v1",
    "paragraph_translator": "v1",
    "revision_translator": "v1",
    "mindmap_drawer": "v1",
    "heading_translator": "v1",
    "paragraph_batch_translator": "v1",
    "toc_detector": "v1",
}


class PromptRegistry:
    def __init__(self, root: Path = PROMPTS_DIR) -> None:
        self._root = root
        self._env = Environment(
            loader=FileSystemLoader(str(root)),
            autoescape=select_autoescape(default=False, default_for_string=False),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, agent: str, *, version: str | None = None, **vars: Any) -> str:
        v = version or DEFAULT_VERSIONS.get(agent)
        if v is None:
            raise KeyError(f"unknown agent: {agent}")
        template_path = f"{agent}/{v}.j2"
        template = self._env.get_template(template_path)
        return template.render(**vars)

    def version_for(self, agent: str) -> str:
        v = DEFAULT_VERSIONS.get(agent)
        if v is None:
            raise KeyError(f"unknown agent: {agent}")
        return v


_GLOBAL: PromptRegistry | None = None


def get_registry() -> PromptRegistry:
    global _GLOBAL
    if _GLOBAL is None:
        _GLOBAL = PromptRegistry()
    return _GLOBAL
