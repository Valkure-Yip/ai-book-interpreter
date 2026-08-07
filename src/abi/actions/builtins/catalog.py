"""Closed catalog binding ABI book work to typed, scoped Actions."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from abi.actions.builtins.inputs import (
    BuildEpubInput,
    ChapterBatchInput,
    EmptyInput,
    ReleaseInput,
    ResearchInput,
    ReviewBatchInput,
    SourceIngestInput,
    SourceSplitInput,
    SpotcheckInput,
)
from abi.actions.contracts import ActionAccess, ActionDefinition, ActionExecutionContext
from abi.actions.effects import expand_expected_artifacts
from abi.actions.predicates import PredicateCatalog
from abi.actions.registry import ActionRegistry
from abi.actions.validators import validator_catalog
from abi.epub.result import GateResult
from abi.project.artifacts import ArtifactConflictError, ArtifactStore, BufferedAttemptWriter
from abi.prompts.actions import ActionPromptRegistry, ActionPromptSnapshot
from abi.tools.belt import build_belt
from abi.tools.context import ToolContext
from abi.tools.permissions import ActionPathPermissions
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionArgument,
    ActionKind,
    ActionOutcome,
    ActionOutcomeEnvelope,
    ActionSpec,
    ActionStatus,
    AgentCompleted,
    ArtifactBundle,
    EffectSpec,
    EvidenceSpec,
    PermanentFailure,
    PredicateSpec,
    RepairRequired,
    RetryableFailure,
    RetryPolicySpec,
    Succeeded,
)
from abi.types.tools import GateRuntimeMetadata, ReviewActionIdentity, ToolBinding

if TYPE_CHECKING:
    from abi.providers.agent_runtime.runner import AgentResume


class ActionToolRef(FrozenModel):
    """A serializable tool name exposed before runtime binding resolution."""

    name: str


class ActionEnvelope(FrozenModel):
    """The least-privilege tool and filesystem view for one typed Action."""

    capability: str
    parameters: FrozenModel
    tools: tuple[ActionToolRef, ...]
    approval_tools: tuple[str, ...] = ()
    permissions: ActionPathPermissions
    gate_permissions: ActionPathPermissions | None = None
    skill_refs: tuple[str, ...]


class _GlossaryTerm(FrozenModel):
    term: str
    target: str
    status: str
    display_policy: str
    forbidden_body_renderings: str
    note: str


@dataclass(frozen=True, slots=True)
class _Builtin:
    capability: str
    description: str
    input_model: type[FrozenModel]
    kind: ActionKind
    dependencies: str | tuple[str, ...] | None
    effect: str
    evidence: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    read_set: tuple[str, ...]
    write_set: tuple[str, ...]
    estimated_cost: float
    approval_tools: tuple[str, ...] = ()


_QUALITY_SKILLS = (
    "skills/expert-translation-quality/SKILL.md",
    "skills/translation-quality-defect-families/SKILL.md",
)

_BUILTINS = (
    _Builtin(
        "source.ingest",
        "Parse and clean the source book.",
        SourceIngestInput,
        ActionKind.DETERMINISTIC,
        None,
        "source.manifest",
        "source.manifest",
        (),
        (),
        ("source",),
        ("source", "metadata"),
        0.0,
    ),
    _Builtin(
        "source.split",
        "Split source into explicit chapters.",
        SourceSplitInput,
        ActionKind.DETERMINISTIC,
        "source.ingest",
        "source.toc",
        "source.toc",
        (),
        (),
        ("source",),
        ("source", "chapters/src"),
        0.0,
    ),
    _Builtin(
        "research.global",
        "Capture universal translation research.",
        ResearchInput,
        ActionKind.AGENT,
        "source.split",
        "qa/benchmark",
        "qa/benchmark",
        ("read_file", "write_file", "grep"),
        _QUALITY_SKILLS,
        ("references",),
        ("qa/benchmark",),
        0.20,
    ),
    _Builtin(
        "research.book",
        "Research this book and define its style profile.",
        ResearchInput,
        ActionKind.AGENT,
        "source.split",
        "metadata/style_profile.md",
        "metadata/style_profile.md",
        ("read_file", "write_file", "grep"),
        _QUALITY_SKILLS,
        ("source", "references"),
        ("metadata",),
        0.50,
    ),
    _Builtin(
        "translation.trial",
        "Run and judge representative translation trials.",
        EmptyInput,
        ActionKind.AGENT,
        ("research.global", "research.book"),
        "qa/pretranslation",
        "qa/pretranslation",
        ("read_file", "write_file", "grep"),
        _QUALITY_SKILLS,
        ("source", "metadata", "skills"),
        ("qa/pretranslation", "metadata"),
        0.80,
    ),
    _Builtin(
        "glossary.prepare",
        "Create the persistent glossary and style guide.",
        EmptyInput,
        ActionKind.AGENT,
        "translation.trial",
        "glossary/terms.csv",
        "glossary/terms.csv",
        ("read_file", "write_file", "grep"),
        (),
        ("source", "metadata", "qa/pretranslation"),
        ("glossary",),
        0.40,
    ),
    _Builtin(
        "chapter.translate",
        "Translate an explicit, independently writable chapter batch.",
        ChapterBatchInput,
        ActionKind.AGENT,
        "glossary.prepare",
        "chapters/translated",
        "chapters/translated",
        ("read_file", "write_file", "grep"),
        (),
        ("chapters/src", "glossary"),
        ("chapters/translated",),
        1.50,
    ),
    _Builtin(
        "chapter.control",
        "Run zero-issue full-chapter post-translation control.",
        ChapterBatchInput,
        ActionKind.AGENT,
        "chapter.translate",
        "qa/chapter_controls",
        "qa/chapter_controls",
        ("read_file", "write_file", "edit_file", "grep"),
        _QUALITY_SKILLS,
        ("chapters/translated", "glossary", "skills"),
        ("chapters/controlled", "qa/chapter_controls"),
        0.70,
    ),
    _Builtin(
        "chapter.review",
        "Review, gate, and promote explicit chapters.",
        ReviewBatchInput,
        ActionKind.AGENT,
        "chapter.control",
        "chapters/final",
        "qa/gates",
        ("read_file", "write_file", "grep"),
        _QUALITY_SKILLS,
        ("chapters/src", "chapters/controlled", "glossary", "skills"),
        (
            "qa/fidelity",
            "qa/readability",
            "qa/imagery",
            "qa/terminology",
            "qa/gates",
            "chapters/final",
        ),
        1.00,
    ),
    _Builtin(
        "preproduction.spec",
        "Write the production specification.",
        EmptyInput,
        ActionKind.AGENT,
        "chapter.review",
        "preproduction/stage1",
        "preproduction/stage1",
        ("read_file", "write_file", "grep"),
        (),
        ("references", "metadata", "chapters/final"),
        ("preproduction/stage1", "frontmatter", "metadata"),
        0.30,
    ),
    _Builtin(
        "preproduction.sample",
        "Build and review a representative sample EPUB.",
        BuildEpubInput,
        ActionKind.AGENT,
        "preproduction.spec",
        "preproduction/stage2_sample",
        "preproduction/stage2_sample",
        ("read_file", "write_file", "grep", "build_sample_epub", "epubcheck"),
        (),
        ("chapters/final", "preproduction/stage1", "frontmatter", "metadata", "assets"),
        ("preproduction/stage2_sample", "output"),
        0.25,
    ),
    _Builtin(
        "epub.build",
        "Build and lint the full EPUB deterministically.",
        BuildEpubInput,
        ActionKind.DETERMINISTIC,
        "preproduction.sample",
        "output/book.epub",
        "output/book.epub",
        (),
        (),
        ("chapters/final", "frontmatter", "metadata", "assets"),
        ("output",),
        0.0,
    ),
    _Builtin(
        "review.spotcheck",
        "Run isolated stratified random review.",
        SpotcheckInput,
        ActionKind.COMPOSITE,
        "epub.build",
        "reviews/random_spotcheck",
        "reviews/random_spotcheck",
        (
            "read_file",
            "grep",
            "select_random_review_passages",
            "validate_random_spotcheck",
            "spawn_review_agent",
        ),
        _QUALITY_SKILLS,
        ("chapters/src", "chapters/final", "references", "skills"),
        ("reviews/random_spotcheck",),
        2.00,
    ),
    _Builtin(
        "review.independent",
        "Run two independent final reviewers.",
        ReviewBatchInput,
        ActionKind.COMPOSITE,
        "epub.build",
        "reviews/agent_a",
        "reviews/agent_b",
        ("read_file", "write_file", "grep", "spawn_review_agent"),
        _QUALITY_SKILLS,
        (
            "chapters/src",
            "chapters/final",
            "references",
            "skills",
            "output/book.epub",
            "output/epubcheck.json",
            "output/publication_lint.json",
            "output/asset_manifest_check.json",
        ),
        ("reviews/agent_a", "reviews/agent_b", "reviews/revision_route.md"),
        1.20,
    ),
    _Builtin(
        "release.prepare",
        "Create a versioned release artifact.",
        ReleaseInput,
        ActionKind.DETERMINISTIC,
        ("review.spotcheck", "review.independent"),
        "output/release",
        "output/release",
        (),
        (),
        ("output", "reviews/random_spotcheck", "metadata"),
        ("output/release", "output/private_artifacts"),
        0.0,
    ),
    _Builtin(
        "output.finalize",
        "Write the final evidence manifest.",
        EmptyInput,
        ActionKind.AGENT,
        "release.prepare",
        "output/final_manifest.md",
        "output/final_manifest.md",
        ("read_file", "write_file", "grep"),
        (),
        ("output", "reviews", "metadata"),
        ("output/final_manifest.md",),
        0.10,
        ("write_file",),
    ),
    _Builtin(
        "retrospective.capture",
        "Capture reusable findings without committing state.",
        EmptyInput,
        ActionKind.AGENT,
        "output.finalize",
        "retrospective",
        "retrospective",
        ("read_file", "write_file", "grep"),
        _QUALITY_SKILLS,
        ("qa", "reviews", "output", "skills"),
        ("retrospective",),
        0.20,
    ),
)

_BY_CAPABILITY = {item.capability: item for item in _BUILTINS}

_CJK_CHAR = r"[\u4e00-\u9fff]"


def _normalize_zh_final_text(path: str, content: str) -> str:
    """Enforce deterministic Chinese quote glyphs at the staged write boundary."""
    if not path.startswith("chapters/final/") or not path.endswith(".md"):
        return content

    def replace_pairs(text: str, ascii_mark: str, opening: str, closing: str) -> str:
        is_open = False

        def replacement(_match: re.Match[str]) -> str:
            nonlocal is_open
            result = closing if is_open else opening
            is_open = not is_open
            return result

        pattern = rf"(?<={_CJK_CHAR}){re.escape(ascii_mark)}|{re.escape(ascii_mark)}(?={_CJK_CHAR})"
        return re.sub(pattern, replacement, text)

    content = replace_pairs(content, '"', "“", "”")
    return replace_pairs(content, "'", "‘", "’")


def _action_succeeded(snapshot: object, arguments: tuple[ActionArgument, ...]) -> bool:
    if len(arguments) != 1 or arguments[0].name != "capability":
        return False
    try:
        dependency = json.loads(arguments[0].value_json)
    except json.JSONDecodeError:
        return False
    if not isinstance(dependency, str):
        return False
    actions = getattr(snapshot, "actions", ())
    return any(
        action.capability == dependency
        and action.status == ActionStatus.SUCCEEDED
        and action.outputs_current
        for action in actions
    )


def _predicate_for(
    dependencies: str | tuple[str, ...] | None,
) -> tuple[PredicateSpec, ...]:
    if dependencies is None:
        return ()
    normalized = (dependencies,) if isinstance(dependencies, str) else dependencies
    return tuple(
        PredicateSpec(
            name="action.succeeded",
            arguments=(ActionArgument(name="capability", value_json=json.dumps(dependency)),),
        )
        for dependency in normalized
    )


def _unbound_tool() -> str:
    raise RuntimeError("tool metadata is not executable; bind a ToolBelt before Action execution")


def _declared_tools() -> dict[str, ToolBinding]:
    names = sorted({name for item in _BUILTINS for name in item.tools})
    return {
        name: ToolBinding(name, f"Declared built-in tool {name}.", EmptyInput, _unbound_tool)
        for name in names
    }


def _fixed_arguments_for(
    capability: str, *, tool_context: ToolContext | None
) -> tuple[ActionArgument, ...]:
    """Return deterministic argument values the Planner may copy into a proposal."""
    if capability == "release.prepare":
        # A fresh autonomous run publishes its first immutable release.  Release
        # versioning is controller-owned so neither the planner nor the agent may
        # invent or override the semantic version at the final boundary.
        return (ActionArgument(name="version", value_json='"v0.0.1"'),)
    if not isinstance(tool_context, ToolContext) or capability not in {
        "source.split",
        "chapter.translate",
        "chapter.control",
        "chapter.review",
        "review.independent",
        "review.spotcheck",
    }:
        return ()
    source_relpath = "source/source_text_raw.txt"
    try:
        source_data = tool_context.read_authorized_bytes(
            source_relpath,
            ActionPathPermissions(read_dirs=("source",)),
        )
    except (FileNotFoundError, PermissionError):
        return ()

    from abi.ir import ingest_bytes
    from abi.ir.split import plan_chapters

    book, _ = ingest_bytes(source_data, source_name=source_relpath)
    chapters = tuple(entry.slug for entry in plan_chapters(book))
    if not chapters:
        return ()
    if capability == "chapter.review":
        snapshot = tool_context.get_run_snapshot()
        chapter_repair_evidence = "\n".join(
            incident.message
            for incident in snapshot.incidents
            if incident.reason_code
            in {"chapter_typography_failed", "translation_quality_failed"}
        )
        affected = tuple(
            chapter
            for chapter in chapters
            if (
                f"chapters/final/{chapter}.md" in chapter_repair_evidence
                or f"{chapter}.md" in chapter_repair_evidence
            )
        )
        if affected:
            chapters = affected
    chapter_json = json.dumps(chapters, separators=(",", ":"))
    if capability == "source.split":
        return (
            ActionArgument(name="expected_chapters", value_json=chapter_json),
            ActionArgument(name="refine_toc", value_json="true"),
            ActionArgument(
                name="source_relpath", value_json=json.dumps(source_relpath)
            ),
        )
    if capability == "review.independent":
        return (
            ActionArgument(name="chapters", value_json=chapter_json),
            ActionArgument(
                name="reviewers",
                value_json=json.dumps(("agent_a", "agent_b"), separators=(",", ":")),
            ),
        )
    if capability == "review.spotcheck":
        return (
            ActionArgument(name="round_id", value_json='"round_001"'),
            ActionArgument(
                name="reviewers",
                value_json=json.dumps(("agent_a", "agent_b"), separators=(",", ":")),
            ),
            ActionArgument(name="chapters", value_json=chapter_json),
            ActionArgument(name="samples_per_agent", value_json="1"),
            ActionArgument(name="seed", value_json="42"),
        )
    return (ActionArgument(name="chapters", value_json=chapter_json),)


def _permissions_for(capability: str, parameters: FrozenModel) -> ActionPathPermissions:
    access = _access_for(capability, parameters)

    def split_paths(paths: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        files = tuple(path for path in paths if PurePosixPath(path).suffix)
        directories = tuple(path for path in paths if not PurePosixPath(path).suffix)
        return files, directories

    read_files, read_dirs = split_paths(access.read_set)
    write_files, write_dirs = split_paths(access.write_set)

    return ActionPathPermissions(
        read_files=read_files,
        read_dirs=read_dirs,
        write_files=write_files,
        write_dirs=write_dirs,
    )


def _access_for(capability: str, parameters: FrozenModel) -> ActionAccess:
    """Expand chapter batches to exact resources while preserving shared read roots."""
    item = _BY_CAPABILITY[capability]
    if capability not in {"chapter.translate", "chapter.control", "chapter.review"}:
        return ActionAccess(read_set=item.read_set, write_set=item.write_set)

    if capability in {"chapter.translate", "chapter.control"}:
        if not isinstance(parameters, ChapterBatchInput):
            raise TypeError(f"{capability} requires ChapterBatchInput")
        chapters = parameters.chapters
    else:
        if not isinstance(parameters, ReviewBatchInput):
            raise TypeError("chapter.review requires ReviewBatchInput")
        chapters = parameters.chapters

    shared_reads = tuple(path for path in item.read_set if not path.startswith("chapters/"))
    if capability == "chapter.translate":
        chapter_reads = tuple(f"chapters/src/{chapter}.md" for chapter in chapters)
        writes = tuple(f"chapters/translated/{chapter}.md" for chapter in chapters)
    elif capability == "chapter.control":
        chapter_reads = tuple(f"chapters/translated/{chapter}.md" for chapter in chapters)
        writes = tuple(
            path
            for chapter in chapters
            for path in (
                f"chapters/controlled/{chapter}.md",
                f"qa/chapter_controls/{chapter}.control.md",
            )
        )
    else:
        chapter_reads = tuple(
            path
            for chapter in chapters
            for path in (
                f"chapters/src/{chapter}.md",
                f"chapters/controlled/{chapter}.md",
            )
        )
        writes = tuple(
            path
            for chapter in chapters
            for path in (
                f"chapters/final/{chapter}.md",
                f"qa/fidelity/{chapter}.md",
                f"qa/readability/{chapter}.md",
                f"qa/imagery/{chapter}.imagery.md",
                f"qa/terminology/{chapter}.md",
                f"qa/gates/{chapter}.gate.md",
            )
        )
    return ActionAccess(
        read_set=tuple(sorted((*chapter_reads, *shared_reads))),
        write_set=tuple(sorted(writes)),
    )


def build_action_envelope(capability: str, parameters: FrozenModel) -> ActionEnvelope:
    try:
        item = _BY_CAPABILITY[capability]
    except KeyError as exc:
        raise KeyError(
            f"unknown capability {capability}; no tools or paths may be granted"
        ) from exc
    if not isinstance(parameters, item.input_model):
        raise TypeError(
            f"{capability} requires {item.input_model.__name__}; parse parameters before execution"
        )
    permissions = _permissions_for(capability, parameters)
    gate_permissions = None
    if capability == "review.spotcheck":
        gate_permissions = permissions
        permissions = permissions.model_copy(
            update={"write_files": (), "write_dirs": ()}
        )
    return ActionEnvelope(
        capability=capability,
        parameters=parameters,
        tools=tuple(ActionToolRef(name=name) for name in item.tools),
        approval_tools=item.approval_tools,
        permissions=permissions,
        gate_permissions=gate_permissions,
        skill_refs=item.skills,
    )


def _prompt_snapshot(
    context: ActionExecutionContext,
    parameters: FrozenModel,
    *,
    capability: str,
    tool_context: ToolContext,
    permissions: ActionPathPermissions,
) -> ActionPromptSnapshot:
    project = context.project
    values = {
        "source_lang": context.source_lang,
        "target_lang": context.target_lang,
        "source_target": context.source_target,
        "publication_mode": context.publication_mode,
        "book_slug": context.book_slug,
        "profile": context.profile,
        "repair_context": context.repair_context
        or tuple(
            f"{incident.reason_code}: {incident.message}"
            for incident in context.snapshot.incidents
            if incident.repair_class == "semantic"
        ),
        "authorized_reference_paths": tuple(
            relpath
            for relpath in (
                "references/quality_gate_framework.md",
                "references/quality_standard.md",
                "references/chapter_title_policy.md",
                "references/stratified_random_spotcheck.md",
                "references/release_versioning.md",
                "references/epub_assets_figures_tables.md",
                "references/english_source_notes.md",
            )
            if permissions.can_read(relpath)
        ),
    }
    if capability != "chapter.translate" or not isinstance(parameters, ChapterBatchInput):
        return ActionPromptSnapshot(**values)
    source_parts = []
    for chapter in parameters.chapters:
        relpath = f"chapters/src/{chapter}.md"
        try:
            source = tool_context.read_authorized_bytes(
                relpath, permissions
            ).decode("utf-8")
        except FileNotFoundError:
            continue
        source_parts.append(f"## {chapter}\n{source}")
    rules = _style_rules(
        _read_optional_text(tool_context, permissions, project.style_guide)
    )
    terms = _matched_terms(
        _read_optional_text(tool_context, permissions, project.terms_csv),
        "\n".join(source_parts),
    )
    return ActionPromptSnapshot(
        **values,
        source_text="\n\n".join(source_parts),
        style_rules=rules,
        matched_terms=terms,
    )


def _read_optional_text(
    tool_context: ToolContext,
    permissions: ActionPathPermissions,
    path: Path,
) -> str | None:
    try:
        return tool_context.read_authorized_bytes(
            path.relative_to(tool_context.project.root).as_posix(), permissions
        ).decode("utf-8")
    except FileNotFoundError:
        return None


def _style_rules(text: str | None) -> tuple[str, ...]:
    if text is None:
        return ()
    candidates = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            candidates.append(stripped[2:].strip())
        elif stripped and not stripped.startswith("#"):
            candidates.append(stripped)
    return tuple(item for item in candidates if item)[:8]


def _matched_terms(text: str | None, source_text: str) -> tuple[str, ...]:
    if text is None:
        return ()
    matches: list[str] = []
    with io.StringIO(text, newline="") as handle:
        for row in csv.DictReader(handle):
            parsed = _GlossaryTerm.model_validate(row)
            if parsed.term and parsed.term in source_text:
                matches.append(f"{parsed.term} => {parsed.target}")
    return tuple(matches)


def _skill_context(
    tool_context: ToolContext,
    skill_refs: tuple[str, ...],
) -> str:
    sections: list[str] = []
    for skill_ref in skill_refs:
        try:
            # Registry-bound skill_refs are their own static allowlist, independent
            # of the planner-derived artifact read_set.
            content = tool_context.read_authorized_bytes(skill_ref, None).decode("utf-8")
        except (FileNotFoundError, IsADirectoryError) as exc:
            raise ValueError(
                f"registered skill {skill_ref} is missing from the book project; "
                "scaffold the project assets before executing this Action"
            ) from exc
        sections.append(f"## Skill: {skill_ref}\n{content}")
    return "\n\n".join(sections)


def _action_iteration_limit(manifest_size: int) -> int:
    """Bound the agent loop while leaving room for read/write turns per artifact."""
    if manifest_size < 1:
        raise ValueError("agent Action manifest must contain at least one artifact")
    return max(40, manifest_size * 2 + 20)


class AgentActionExecutor:
    """One implementation path for all agent and composite built-in Actions."""

    def __init__(
        self,
        capability: str,
        *,
        tool_context: ToolContext | None,
        prompts: ActionPromptRegistry,
    ) -> None:
        self._capability = capability
        self._tool_context = tool_context
        self._prompts = prompts

    async def __call__(
        self, context: ActionExecutionContext, parameters: FrozenModel
    ) -> ActionOutcomeEnvelope:
        if (
            self._capability == "chapter.review"
            and isinstance(parameters, ReviewBatchInput)
            and len(parameters.chapters) > 1
        ):
            return await self._execute_chapter_reviews(context, parameters)
        result = await self._execute(
            context, parameters, resume=None, inspect_only=False, thread_scope=None
        )
        return cast(ActionOutcomeEnvelope, result)

    async def _execute_chapter_reviews(
        self,
        context: ActionExecutionContext,
        parameters: ReviewBatchInput,
    ) -> ActionOutcomeEnvelope:
        """Run one bounded agent loop per chapter, then prove the full frozen manifest."""
        for chapter in parameters.chapters:
            result = cast(
                ActionOutcomeEnvelope,
                await self._execute(
                    context,
                    ReviewBatchInput(chapters=(chapter,)),
                    resume=None,
                    inspect_only=False,
                    thread_scope=chapter,
                ),
            )
            if not isinstance(result.outcome, Succeeded):
                return result

        action_id = context.action_id
        if action_id is None:
            parameter_hash = hashlib.sha256(
                parameters.model_dump_json().encode()
            ).hexdigest()[:12]
            action_id = f"{self._capability}:{parameter_hash}"
        store = ArtifactStore(context.project, None)
        try:
            manifest = expand_expected_artifacts(
                self._capability, action_id, parameters
            )
            bundle = store.rebuild_exact_staged_bundle(
                action_id, context.attempt, manifest
            )
        except ArtifactConflictError as exc:
            return ActionOutcomeEnvelope(
                action_id=action_id,
                attempt=context.attempt,
                outcome=RepairRequired(
                    repair_class="integrity",
                    repair_source="action_outcome",
                    reason_code="artifact_bundle_conflict",
                    defect_codes=("artifact_bundle_conflict",),
                    message=str(exc),
                ),
            )
        finally:
            store.close()
        return ActionOutcomeEnvelope(
            action_id=action_id,
            attempt=context.attempt,
            outcome=Succeeded(artifact_bundle=bundle, evidence_refs=()),
        )

    async def resume_hitl(
        self,
        context: ActionExecutionContext,
        parameters: FrozenModel,
        resume: object,
    ) -> ActionOutcomeEnvelope:
        """Resume this exact Action checkpoint through the Task 6 typed boundary."""
        from abi.providers.agent_runtime import HitlResume

        if not isinstance(resume, HitlResume):
            raise TypeError("agent Action HITL continuation requires HitlResume")
        result = await self._execute(
            context, parameters, resume=resume, inspect_only=False, thread_scope=None
        )
        return cast(ActionOutcomeEnvelope, result)

    async def inspect_hitl(
        self,
        context: ActionExecutionContext,
        parameters: FrozenModel,
        resume: object,
    ) -> object:
        """Inspect this exact Action checkpoint without executing it."""
        from abi.providers.agent_runtime import HitlResume

        if not isinstance(resume, HitlResume):
            raise TypeError("agent Action HITL inspection requires HitlResume")
        return await self._execute(
            context, parameters, resume=resume, inspect_only=True, thread_scope=None
        )

    async def _execute(
        self,
        context: ActionExecutionContext,
        parameters: FrozenModel,
        *,
        resume: AgentResume | None,
        inspect_only: bool,
        thread_scope: str | None,
    ) -> object:
        parameter_hash = hashlib.sha256(parameters.model_dump_json().encode()).hexdigest()[:12]
        action_id = context.action_id or f"{self._capability}:{parameter_hash}"
        if self._tool_context is None or self._tool_context.project.root != context.project.root:
            return ActionOutcomeEnvelope(
                action_id=action_id,
                attempt=context.attempt,
                outcome=PermanentFailure(
                    error_code="action_runtime_not_bound",
                    message="Bind this catalog to the current ToolContext before dispatch.",
                ),
            )
        envelope = build_action_envelope(self._capability, parameters)
        store = ArtifactStore(context.project, None)
        attempt_writer = store.writer(action_id, context.attempt)
        writer = (
            BufferedAttemptWriter(action_id, context.attempt)
            if self._capability == "review.spotcheck"
            else attempt_writer
        )
        manifest = expand_expected_artifacts(self._capability, action_id, parameters)

        def finalized_outcome(
            completion: AgentCompleted,
            *,
            recovered_bundle: ArtifactBundle | None = None,
        ) -> ActionOutcome:
            expected_effects = tuple(
                (item.canonical_relpath, item.media_type, item.evidence_role, item.metadata)
                for item in manifest.entries
            )
            actual_effects = tuple(
                (item.canonical_relpath, item.media_type, item.evidence_role, item.metadata)
                for item in (
                    recovered_bundle.entries
                    if recovered_bundle is not None
                    else writer.entries
                )
            )
            if actual_effects != expected_effects:
                expected_paths = {item[0] for item in expected_effects}
                actual_paths = {item[0] for item in actual_effects}
                missing_paths = tuple(sorted(expected_paths - actual_paths))
                unexpected_paths = tuple(sorted(actual_paths - expected_paths))
                if missing_paths and not unexpected_paths:
                    return RetryableFailure(
                        error_code="agent_incomplete_outputs",
                        message=(
                            "The agent completed before emitting the full authorized manifest. "
                            f"Missing: {', '.join(missing_paths)}. "
                            "Retry this side-effect-free Action in a fresh attempt."
                        ),
                    )
                return RepairRequired(
                    repair_class="integrity",
                    repair_source="action_outcome",
                    reason_code="artifact_bundle_conflict",
                    defect_codes=("artifact_bundle_conflict",),
                    message=(
                        "Recorded attempt effects do not equal the authorized manifest; "
                        f"the agent completion summary was {completion.summary!r}."
                    ),
                )
            if self._capability == "review.independent":
                invalid_results: list[str] = []
                for relpath in (
                    "reviews/agent_a/review.md",
                    "reviews/agent_b/review.md",
                    "reviews/revision_route.md",
                ):
                    try:
                        content = (
                            store.read_staged_bytes(action_id, context.attempt, relpath)
                            if recovered_bundle is not None
                            else writer.read_bytes(relpath)
                        ).decode("utf-8")
                    except (KeyError, OSError, UnicodeError):
                        invalid_results.append(relpath)
                        continue
                    if _terminal_review_result(content) is None:
                        invalid_results.append(relpath)
                if invalid_results:
                    return RetryableFailure(
                        error_code="review_result_protocol_invalid",
                        message=(
                            "Independent review outputs must end with exactly one plain "
                            "`result: PASS` or `result: FAIL` line. Invalid: "
                            f"{', '.join(invalid_results)}. Retry the side-effect-free review "
                            "in a fresh attempt."
                        ),
                    )
            bundle = (
                recovered_bundle
                if recovered_bundle is not None
                else writer.artifact_bundle()
            )
            if recovered_bundle is None and isinstance(writer, BufferedAttemptWriter):
                bundle = writer.flush_to(attempt_writer)
            return Succeeded(artifact_bundle=bundle, evidence_refs=())

        belt = build_belt(
            self._tool_context,
            get_run_snapshot=lambda: context.snapshot,
            permissions=envelope.permissions,
            gate_permissions=envelope.gate_permissions,
            action_identity=ReviewActionIdentity(
                run_id=context.run_id,
                action_id=action_id,
                attempt=context.attempt,
            ),
            capability=self._capability,
            runtime_metadata=GateRuntimeMetadata(
                target_language=context.target_lang,
                publication_mode=context.publication_mode,
            ),
            writer=writer,
            expected_artifacts={item.canonical_relpath: item for item in manifest.entries},
            spotcheck_input=parameters if isinstance(parameters, SpotcheckInput) else None,
            write_transform=(
                _normalize_zh_final_text
                if self._capability == "chapter.review"
                and context.target_lang.lower().startswith("zh")
                else None
            ),
        )
        try:
            tools = belt.resolve(tuple(tool.name for tool in envelope.tools))
            snapshot = _prompt_snapshot(
                context,
                parameters,
                capability=self._capability,
                tool_context=self._tool_context,
                permissions=envelope.permissions,
            )
            user_prompt = self._prompts.render(self._capability, parameters, snapshot)
            skill_context = _skill_context(self._tool_context, envelope.skill_refs)
            if skill_context:
                user_prompt = f"{user_prompt}\n\n# Action skills\n{skill_context}"
        except (OSError, TypeError, ValueError) as exc:
            store.close()
            return ActionOutcomeEnvelope(
                action_id=action_id,
                attempt=context.attempt,
                outcome=RepairRequired(
                    repair_class="integrity",
                    repair_source="action_outcome",
                    reason_code="action_envelope_invalid",
                    defect_codes=("action_envelope_invalid",),
                    message=str(exc),
                ),
            )
        from abi.providers.agent_runtime import AgentActionRequest

        try:
            request = AgentActionRequest(
                system_prompt=self._prompts.system_prompt(self._capability, snapshot),
                user_prompt=user_prompt,
                tools=tools,
                agent_name=self._capability.replace(".", "_"),
                thread_id=(
                    f"{context.run_id}/{action_id}/{context.attempt}"
                    + (f":{thread_scope}" if thread_scope is not None else "")
                ),
                checkpoint_path=context.project.action_checkpoints,
                max_iterations=_action_iteration_limit(len(manifest.entries)),
                may_have_side_effects=False,
                resume=resume,
                approval_tools=envelope.approval_tools,
            )
            if inspect_only:
                from abi.providers.agent_runtime import HitlCheckpointInspection

                inspection = await self._tool_context.services.agent.inspect_hitl_checkpoint(
                    request
                )
                if inspection.disposition != "outcome" or not isinstance(
                    inspection.outcome, AgentCompleted
                ):
                    return inspection
                try:
                    recovered_bundle = store.rebuild_exact_staged_bundle(
                        action_id, context.attempt, manifest
                    )
                except ArtifactConflictError as exc:
                    return HitlCheckpointInspection(
                        disposition="outcome",
                        outcome=RepairRequired(
                            repair_class="integrity",
                            repair_source="action_outcome",
                            reason_code="artifact_bundle_conflict",
                            defect_codes=("artifact_bundle_conflict",),
                            message=str(exc),
                        ),
                    )
                return HitlCheckpointInspection(
                    disposition="outcome",
                    outcome=finalized_outcome(
                        inspection.outcome, recovered_bundle=recovered_bundle
                    ),
                )
            result = await self._tool_context.services.agent.run_action(request)
            outcome: ActionOutcome
            if isinstance(result.outcome, AgentCompleted):
                continued_bundle: ArtifactBundle | None = None
                if resume is not None:
                    # A HITL/checkpoint continuation executes with tool bindings
                    # reconstructed around the same durable staging directory.  The
                    # new writer object does not carry the pre-pause in-memory entry
                    # list, so rebuild the exact authorized bundle from disk before
                    # deciding whether the Action emitted all required artifacts.
                    try:
                        continued_bundle = store.rebuild_exact_staged_bundle(
                            action_id, context.attempt, manifest
                        )
                    except ArtifactConflictError as exc:
                        outcome = RepairRequired(
                            repair_class="integrity",
                            repair_source="action_outcome",
                            reason_code="artifact_bundle_conflict",
                            defect_codes=("artifact_bundle_conflict",),
                            message=str(exc),
                        )
                    else:
                        outcome = finalized_outcome(
                            result.outcome, recovered_bundle=continued_bundle
                        )
                else:
                    outcome = finalized_outcome(result.outcome)
                    if (
                        isinstance(outcome, RetryableFailure)
                        and outcome.error_code == "agent_incomplete_outputs"
                    ):
                        emitted_paths = {
                            entry.canonical_relpath for entry in writer.entries
                        }
                        missing_paths = tuple(
                            entry.canonical_relpath
                            for entry in manifest.entries
                            if entry.canonical_relpath not in emitted_paths
                        )
                        completion_request = replace(
                            request,
                            user_prompt=(
                                request.user_prompt
                                + "\n\n# Required manifest completion\n\n"
                                "The previous bounded agent loop completed before emitting "
                                "the full authorized manifest. Continue the same Action using "
                                "the original task context above, but write only every missing "
                                "output listed below. Existing outputs are immutable: read them "
                                "if useful, but do not rewrite them. Do not stop until each "
                                "missing path has a successful write_file call.\n\n"
                                "Missing outputs:\n"
                                + "\n".join(f"- `{path}`" for path in missing_paths)
                            ),
                            thread_id=f"{request.thread_id}:manifest-completion",
                            max_iterations=_action_iteration_limit(len(missing_paths)),
                            resume=None,
                        )
                        completion_result = (
                            await self._tool_context.services.agent.run_action(
                                completion_request
                            )
                        )
                        if isinstance(completion_result.outcome, AgentCompleted):
                            outcome = finalized_outcome(completion_result.outcome)
                        else:
                            outcome = completion_result.outcome
            else:
                outcome = result.outcome
            return ActionOutcomeEnvelope(
                action_id=action_id, attempt=context.attempt, outcome=outcome
            )
        finally:
            store.close()


class DeterministicActionExecutor:
    """Invoke existing deterministic ABI functions without changing control state."""

    def __init__(self, capability: str, *, tool_context: ToolContext | None) -> None:
        self._capability = capability
        self._tool_context = tool_context

    async def __call__(
        self, context: ActionExecutionContext, parameters: FrozenModel
    ) -> ActionOutcomeEnvelope:
        action_id = context.action_id or self._capability.replace(".", "-")
        store = ArtifactStore(context.project, None)
        try:
            writer = store.writer(action_id, context.attempt)
            evidence_refs: tuple[str, ...]
            if self._capability == "source.ingest":
                if not isinstance(parameters, SourceIngestInput):
                    raise TypeError("source.ingest requires SourceIngestInput")
                from abi.ir import ingest_bytes

                if (
                    self._tool_context is None
                    or self._tool_context.project.root != context.project.root
                ):
                    raise RuntimeError("source Action requires its bound ToolContext")
                source_data = self._tool_context.read_authorized_bytes(
                    parameters.source_relpath,
                    _permissions_for(self._capability, parameters),
                )
                book, warnings = ingest_bytes(
                    source_data, source_name=parameters.source_relpath
                )
                paragraphs = book.iter_paragraphs()
                clean = "\n\n".join(
                    item.source_text for item in paragraphs if item.source_text.strip()
                )
                manifest = {
                    "authors": book.meta.authors,
                    "format": book.meta.source_format,
                    "paragraphs": len(paragraphs),
                    "sections": len(book.toc),
                    "sha256": hashlib.sha256(source_data).hexdigest(),
                    "source_file": parameters.source_relpath,
                    "source_language": book.meta.source_language,
                    "title": book.meta.title,
                    "warnings": warnings[:50],
                }
                writer.write_text(
                    "source/source_manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                    media_type="application/json",
                    evidence_role="source_manifest",
                )
                writer.write_text(
                    "source/source_text.txt",
                    clean,
                    media_type="text/plain",
                    evidence_role="source_text",
                )
                evidence_refs = ("source_manifest", "source_text")
            elif self._capability == "source.split":
                if not isinstance(parameters, SourceSplitInput):
                    raise TypeError("source.split requires SourceSplitInput")
                from abi.ir import ingest_bytes
                from abi.ir.split import render_chapters

                if (
                    self._tool_context is None
                    or self._tool_context.project.root != context.project.root
                ):
                    raise RuntimeError("source Action requires its bound ToolContext")
                permissions = _permissions_for(self._capability, parameters)
                source_relpath = parameters.source_relpath
                try:
                    source_data = self._tool_context.read_authorized_bytes(
                        source_relpath, permissions
                    )
                except FileNotFoundError:
                    source_relpath = "source/source_text.txt"
                    source_data = self._tool_context.read_authorized_bytes(
                        source_relpath, permissions
                    )
                book, _ = ingest_bytes(source_data, source_name=source_relpath)
                rendered = render_chapters(book)
                entries = [entry for entry, _ in rendered]
                actual_chapters = tuple(entry.slug for entry in entries)
                if actual_chapters != parameters.expected_chapters:
                    raise ValueError(
                        "actual source chapter stems do not exactly equal expected_chapters"
                    )
                for entry, content in rendered:
                    writer.write_text(
                        f"chapters/src/{entry.src_path}",
                        content,
                        media_type="text/markdown",
                        evidence_role="source_chapter",
                    )
                toc = [
                    {
                        "index": entry.index,
                        "slug": entry.slug,
                        "title": entry.title,
                        "src": f"chapters/src/{entry.src_path}",
                        "paragraphs": entry.paragraph_count,
                    }
                    for entry in entries
                ]
                writer.write_text(
                    "source/toc.json",
                    json.dumps(toc, ensure_ascii=False, indent=2),
                    media_type="application/json",
                    evidence_role="source_toc",
                )
                evidence_refs = (*parameters.expected_chapters, "source_toc")
            elif self._capability == "epub.build":
                if not isinstance(parameters, BuildEpubInput):
                    raise TypeError("epub.build requires BuildEpubInput")
                from abi.epub.assets import asset_manifest_check
                from abi.epub.build import build_epub_bytes
                from abi.epub.epubcheck import run_epubcheck_readonly
                from abi.epub.lint import publication_lint

                build_result, epub_bytes = build_epub_bytes(context.project)
                if not build_result.ok or not epub_bytes:
                    raise ValueError(build_result.message)
                asset_result = asset_manifest_check(context.project, write_report=False)
                lint_result = publication_lint(
                    context.project,
                    runtime_metadata=context.runtime_metadata,
                    write_report=False,
                )
                writer.write_bytes(
                    "output/asset_manifest_check.json",
                    _gate_result_json(asset_result),
                    media_type="application/json",
                    evidence_role="asset_gate",
                )
                writer.write_bytes(
                    "output/book.epub",
                    epub_bytes,
                    media_type="application/epub+zip",
                    evidence_role="epub",
                )
                epubcheck_result = run_epubcheck_readonly(writer.staged_path("output/book.epub"))
                writer.write_bytes(
                    "output/epubcheck.json",
                    _gate_result_json(epubcheck_result),
                    media_type="application/json",
                    evidence_role="epubcheck",
                )
                writer.write_bytes(
                    "output/publication_lint.json",
                    _gate_result_json(lint_result),
                    media_type="application/json",
                    evidence_role="publication_gate",
                )
                gate_failure: tuple[str, GateResult] | None = None
                if not epubcheck_result.ok:
                    gate_failure = (
                        "epubcheck_unavailable"
                        if epubcheck_result.message == "EPUBCheck not available"
                        else "epubcheck_failed",
                        epubcheck_result,
                    )
                elif not asset_result.ok:
                    gate_failure = ("asset_manifest_failed", asset_result)
                elif not lint_result.ok:
                    gate_failure = ("publication_lint_failed", lint_result)
                if gate_failure is not None:
                    error_code, failed_gate = gate_failure
                    detail = "; ".join(failed_gate.hard_errors) or failed_gate.message
                    if error_code == "publication_lint_failed":
                        return ActionOutcomeEnvelope(
                            action_id=action_id,
                            attempt=context.attempt,
                            outcome=RepairRequired(
                                repair_class="semantic",
                                repair_source="action_outcome",
                                reason_code="chapter_typography_failed",
                                defect_codes=(error_code,),
                                message=detail,
                            ),
                        )
                    return ActionOutcomeEnvelope(
                        action_id=action_id,
                        attempt=context.attempt,
                        outcome=PermanentFailure(
                            error_code=error_code,
                            message=detail,
                        ),
                    )
                evidence_refs = (
                    "asset_gate",
                    "epub",
                    "epubcheck",
                    "publication_gate",
                )
            elif self._capability == "release.prepare":
                if not isinstance(parameters, ReleaseInput):
                    raise TypeError("release.prepare requires ReleaseInput")
                if not context.project.book_epub.is_file():
                    raise FileNotFoundError("output/book.epub is missing")
                rounds = sorted(context.project.random_spotcheck_dir.glob("round_*"))
                if not rounds:
                    raise ValueError("random spot-check evidence is missing")
                report = json.loads(
                    (rounds[-1] / "validation_report.json").read_text(encoding="utf-8")
                )
                if not isinstance(report, dict) or report.get("status") != "PASS":
                    raise ValueError("random spot-check has not passed")
                version = (
                    parameters.version
                    if parameters.version.startswith("v")
                    else f"v{parameters.version}"
                )
                artifact_name = f"book_{version}.epub"
                state = {
                    "book": context.book_slug,
                    "producer": "ABI",
                    "latest_status": "PASS",
                    "latest_version": version,
                    "releases": [
                        {
                            "version": version,
                            "epub": artifact_name,
                            "created_at": "staged",
                            "status": "PASS",
                        }
                    ],
                }
                writer.write_bytes(
                    f"output/release/{artifact_name}",
                    context.project.book_epub.read_bytes(),
                    media_type="application/epub+zip",
                    evidence_role="release_epub",
                )
                writer.write_text(
                    "output/release/release_state.json",
                    json.dumps(state, ensure_ascii=False, indent=2),
                    media_type="application/json",
                    evidence_role="release_state",
                )
                evidence_refs = ("release_epub", "release_state")
            else:
                raise RuntimeError(
                    f"{self._capability} requires a staged sink adapter before execution"
                )
            bundle = writer.artifact_bundle()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return ActionOutcomeEnvelope(
                action_id=action_id,
                attempt=context.attempt,
                outcome=PermanentFailure(error_code="deterministic_action_error", message=str(exc)),
            )
        finally:
            store.close()
        return ActionOutcomeEnvelope(
            action_id=action_id,
            attempt=context.attempt,
            outcome=Succeeded(artifact_bundle=bundle, evidence_refs=evidence_refs),
        )


def _gate_result_json(result: GateResult) -> bytes:
    return json.dumps(
        {
            "ok": bool(result.ok),
            "message": str(result.message),
            "errors": list(result.hard_errors),
            "warnings": list(result.warnings),
            "details": dict(result.details),
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")


def build_action_registry(*, tool_context: ToolContext | None = None) -> ActionRegistry:
    """Build and startup-validate ABI's complete built-in capability registry."""
    validators = validator_catalog()
    registry = ActionRegistry(
        predicates=PredicateCatalog({"action.succeeded": _action_succeeded}),
        validators=validators,
        tools=_declared_tools(),
        skill_refs=_QUALITY_SKILLS,
        semantic_repair_mappings=(
            ("source_evidence_invalid", "source.ingest"),
            ("source_split_invalid", "source.split"),
            ("global_research_missing", "research.global"),
            ("book_research_missing", "research.book"),
            ("pretranslation_not_passed", "translation.trial"),
            ("glossary_terms_empty", "glossary.prepare"),
            ("glossary_terms_invalid", "glossary.prepare"),
            ("chapter_translation_missing", "chapter.translate"),
            ("chapter_control_not_passed", "chapter.control"),
            ("chapter_control_revision_invalid", "chapter.control"),
            ("chapter_review_scope_empty", "chapter.review"),
            ("chapter_gate_not_passed", "chapter.review"),
            ("chapter_final_missing", "chapter.review"),
            ("production_spec_missing", "preproduction.spec"),
            ("finalized_metadata_invalid", "preproduction.spec"),
            ("sample_review_not_passed", "preproduction.spec"),
            ("spotcheck_not_passed", "chapter.review"),
            ("independent_review_protocol_invalid", "review.independent"),
            ("translation_quality_failed", "chapter.review"),
            ("chapter_typography_failed", "chapter.review"),
            ("epub_quality_failed", "preproduction.spec"),
            ("release_not_passed", "release.prepare"),
            ("final_manifest_missing", "output.finalize"),
            ("retrospective_missing", "retrospective.capture"),
            ("term_drift", "glossary.prepare"),
        ),
    )
    prompts = ActionPromptRegistry()
    for item in _BUILTINS:
        spec = ActionSpec(
            capability=item.capability,
            description=item.description,
            input_schema=item.input_model.__name__,
            action_kind=item.kind,
            prerequisites=_predicate_for(item.dependencies),
            effects=(EffectSpec(name="artifact.produced", artifact_pattern=item.effect),),
            expected_evidence=(EvidenceSpec(name=item.evidence),),
            tool_allowlist=item.tools,
            approval_tools=item.approval_tools,
            skill_refs=item.skills,
            read_set=item.read_set,
            write_set=item.write_set,
            retry_policy=RetryPolicySpec(
                max_attempts=3,
                retryable_codes=(
                    "agent_incomplete_outputs",
                    "iteration_limit",
                    "review_result_protocol_invalid",
                    "transient_provider_error",
                    "provider_timeout",
                ),
                base_delay_s=1.0,
                max_delay_s=30.0,
            ),
            validator=item.capability,
            resource_class="review" if item.kind == ActionKind.COMPOSITE else "default",
            estimated_cost_usd=item.estimated_cost,
            # Built-ins write only to attempt staging. This flag is reserved for
            # uncertain external effects that require probe-before-retry authority.
            may_have_side_effects=False,
        )
        executor = (
            DeterministicActionExecutor(item.capability, tool_context=tool_context)
            if item.kind == ActionKind.DETERMINISTIC
            else AgentActionExecutor(item.capability, tool_context=tool_context, prompts=prompts)
        )
        validator = validators[item.capability]
        registry.register(
            ActionDefinition(
                spec=spec,
                input_model=item.input_model,
                executor=executor,
                validator=validator,
                effect_expander=expand_expected_artifacts,
                access_expander=_access_for,
                fixed_arguments=_fixed_arguments_for(
                    item.capability, tool_context=tool_context
                ),
            )
        )
    registry.validate_startup()
    return registry


_REVIEW_RESULT_LINE = re.compile(r"(?m)^result: (PASS|FAIL)$")


def _terminal_review_result(text: str) -> str | None:
    """Parse the one canonical reviewer result line at the end of a report."""
    stripped = text.rstrip()
    if not stripped:
        return None
    match = _REVIEW_RESULT_LINE.search(stripped)
    if match is None or match.end() != len(stripped):
        return None
    if len(_REVIEW_RESULT_LINE.findall(stripped)) != 1:
        return None
    return match.group(1)
