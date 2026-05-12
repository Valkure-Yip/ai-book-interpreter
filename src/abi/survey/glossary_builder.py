"""Merge per-chapter term candidates into a global Glossary."""

from __future__ import annotations

from collections import Counter, defaultdict

from abi.prompts import get_registry
from abi.providers.llm.factory import LLMRouter, system_message, user_message
from abi.survey._schemas import GlossaryArbiterOutput
from abi.types.glossary import Glossary, GlossaryEntry
from abi.types.survey import ChapterSummary, TermCandidate


def _normalize_surface(s: str) -> str:
    return " ".join(s.lower().split())


async def build_glossary(
    *,
    router: LLMRouter | None,
    chapter_summaries: list[ChapterSummary],
    book_id: str,
    target_language: str,
) -> Glossary:
    """Aggregate, dedup, arbitrate. ``router`` may be None for tests; we then
    skip LLM arbitration and pick by frequency."""

    groups: dict[str, list[TermCandidate]] = defaultdict(list)
    for chapter in chapter_summaries:
        for term in chapter.key_terms:
            key = _normalize_surface(term.surface_form)
            if not key:
                continue
            groups[key].append(term)

    entries: list[GlossaryEntry] = []
    registry = get_registry()
    for key, members in groups.items():
        surface_forms = sorted({m.surface_form for m in members})
        targets = Counter(m.proposed_target for m in members if m.proposed_target)
        if not targets:
            continue
        if len(targets) == 1 or router is None:
            chosen = targets.most_common(1)[0][0]
            alt_targets = [t for t, _ in targets.most_common()[1:]]
        else:
            # Arbitrate.
            prompt = registry.render(
                "glossary_arbiter",
                target_language=target_language,
                term=key,
                surface_forms=surface_forms,
                candidates=[
                    {
                        "target": t,
                        "count": c,
                        "definition": next(
                            (m.definition for m in members if m.proposed_target == t),
                            "",
                        ),
                    }
                    for t, c in targets.most_common()
                ],
                context_snippet=next((m.definition for m in members), ""),
            )
            messages = [
                system_message("You output strict JSON only."),
                user_message(prompt),
            ]
            try:
                parsed, _ = await router.invoke_structured(
                    GlossaryArbiterOutput,
                    messages,
                    agent_name="glossary_arbiter",
                    prompt_version=registry.version_for("glossary_arbiter"),
                    metadata={"term": key},
                )
                chosen = parsed.chosen_target
                alt_targets = [t for t in targets if t != chosen]
            except Exception:
                chosen = targets.most_common(1)[0][0]
                alt_targets = [t for t, _ in targets.most_common()[1:]]

        importance_counter = Counter(m.importance for m in members)
        is_core = importance_counter.get("core", 0) >= 1

        longest_def = max((m.definition for m in members), key=len, default="")
        first_pid = next(
            (m.first_surface_paragraph_id for m in members if m.first_surface_paragraph_id),
            "",
        )

        entries.append(
            GlossaryEntry(
                term=key,
                surface_forms=surface_forms,
                target=chosen,
                alt_targets=alt_targets,
                definition=longest_def,
                first_seen=first_pid,
                locked=True,
                is_core=is_core,
                source="survey",
            )
        )

    # Sort for stable output: core first, then alpha.
    entries.sort(key=lambda e: (not e.is_core, e.term))
    return Glossary(book_id=book_id, target_language=target_language, entries=entries)
