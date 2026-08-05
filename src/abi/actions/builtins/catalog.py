"""Closed catalog binding ABI book work to typed, scoped Actions."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from abi.actions.builtins.inputs import (
    BuildEpubInput,
    ChapterBatchInput,
    EmptyInput,
    ReleaseInput,
    ResearchInput,
    ReviewBatchInput,
    SourceIngestInput,
    SourceSplitInput,
)
from abi.actions.contracts import ActionDefinition, ActionExecutionContext
from abi.actions.predicates import PredicateCatalog
from abi.actions.registry import ActionRegistry
from abi.actions.validators import validator_catalog
from abi.project.layout import BookProject
from abi.prompts.actions import ActionPromptRegistry, ActionPromptSnapshot
from abi.tools.belt import ToolBelt, build_belt
from abi.tools.context import ToolContext
from abi.tools.permissions import ActionPathPermissions
from abi.types._base import FrozenModel
from abi.types.orchestration import (
    ActionArgument,
    ActionKind,
    ActionOutcomeEnvelope,
    ActionSpec,
    ActionStatus,
    EffectSpec,
    EvidenceSpec,
    PermanentFailure,
    PredicateSpec,
    RepairRequired,
    RetryPolicySpec,
    Succeeded,
)
from abi.types.tools import GateRuntimeMetadata, ReviewActionIdentity, ToolBinding


class ActionToolRef(FrozenModel):
    """A serializable tool name exposed before runtime binding resolution."""

    name: str


class ActionEnvelope(FrozenModel):
    """The least-privilege tool and filesystem view for one typed Action."""

    capability: str
    parameters: FrozenModel
    tools: tuple[ActionToolRef, ...]
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


_QUALITY_SKILLS = (
    "skills/expert-translation-quality/SKILL.md",
    "skills/translation-quality-defect-families/SKILL.md",
)

_BUILTINS = (
    _Builtin("source.ingest", "Parse and clean the source book.", SourceIngestInput,
             ActionKind.DETERMINISTIC, None, "source.manifest", "source.manifest",
             (), (), ("source",), ("source", "metadata"), 0.0),
    _Builtin("source.split", "Split source into explicit chapters.", SourceSplitInput,
             ActionKind.DETERMINISTIC, "source.ingest", "source.toc", "source.toc",
             (), (), ("source",), ("source", "chapters/src"), 0.0),
    _Builtin("research.global", "Capture universal translation research.", ResearchInput,
             ActionKind.AGENT, "source.split", "qa/benchmark", "qa/benchmark",
             ("read_file", "write_file", "grep"), (), ("references",), ("qa/benchmark",), 0.20),
    _Builtin("research.book", "Research this book and define its style profile.", ResearchInput,
             ActionKind.AGENT, "source.split", "metadata/style_profile.md", "metadata/style_profile.md",
             ("read_file", "write_file", "grep"), (), ("source", "references"), ("metadata",), 0.50),
    _Builtin("translation.trial", "Run and judge representative translation trials.", EmptyInput,
             ActionKind.AGENT, ("research.global", "research.book"), "qa/pretranslation", "qa/pretranslation",
             ("read_file", "write_file", "grep"), _QUALITY_SKILLS,
             ("source", "metadata", "skills"), ("qa/pretranslation", "metadata"), 0.80),
    _Builtin("glossary.prepare", "Create the persistent glossary and style guide.", EmptyInput,
             ActionKind.AGENT, "translation.trial", "glossary/terms.csv", "glossary/terms.csv",
             ("read_file", "write_file", "grep"), (),
             ("source", "metadata", "qa/pretranslation"), ("glossary",), 0.40),
    _Builtin("chapter.translate", "Translate an explicit, independently writable chapter batch.",
             ChapterBatchInput, ActionKind.AGENT, "glossary.prepare", "chapters/translated",
             "chapters/translated", ("read_file", "write_file", "grep"), (),
             ("chapters/src", "glossary"), ("chapters/translated",), 1.50),
    _Builtin("chapter.control", "Run zero-issue full-chapter post-translation control.",
             ChapterBatchInput, ActionKind.AGENT, "chapter.translate", "qa/chapter_controls",
             "qa/chapter_controls", ("read_file", "write_file", "edit_file", "grep"),
             _QUALITY_SKILLS, ("chapters/translated", "glossary", "skills"),
             ("chapters/translated", "qa/chapter_controls"), 0.70),
    _Builtin("chapter.review", "Review, gate, and promote explicit chapters.", ReviewBatchInput,
             ActionKind.AGENT, "chapter.control", "chapters/final", "qa/gates",
             ("read_file", "write_file", "grep"), _QUALITY_SKILLS,
             ("chapters/src", "chapters/translated", "glossary", "skills"),
             ("qa/fidelity", "qa/readability", "qa/imagery", "qa/terminology", "qa/gates", "chapters/final"),
             1.00),
    _Builtin("preproduction.spec", "Write the production specification.", EmptyInput,
             ActionKind.AGENT, "chapter.review", "preproduction/stage1", "preproduction/stage1",
             ("read_file", "write_file", "grep"), (),
             ("references", "metadata", "chapters/final"),
             ("preproduction/stage1", "frontmatter", "metadata"), 0.30),
    _Builtin("preproduction.sample", "Build and review a representative sample EPUB.",
             BuildEpubInput, ActionKind.AGENT, "preproduction.spec", "preproduction/stage2_sample",
             "preproduction/stage2_sample", ("read_file", "write_file", "grep", "build_sample_epub", "epubcheck"),
             (), ("chapters/final", "preproduction/stage1", "frontmatter", "metadata", "assets"),
             ("preproduction/stage2_sample", "output"), 0.25),
    _Builtin("epub.build", "Build and lint the full EPUB deterministically.", BuildEpubInput,
             ActionKind.DETERMINISTIC, "preproduction.sample", "output/book.epub", "output/book.epub",
             (), (), ("chapters/final", "frontmatter", "metadata", "assets"), ("output",), 0.0),
    _Builtin("review.spotcheck", "Run isolated stratified random review.", ReviewBatchInput,
             ActionKind.COMPOSITE, "epub.build", "reviews/random_spotcheck", "reviews/random_spotcheck",
             ("read_file", "grep", "select_random_review_passages",
              "validate_random_spotcheck", "spawn_review_agent"), _QUALITY_SKILLS,
             ("chapters/src", "chapters/final", "references", "skills", "output"),
             ("reviews/random_spotcheck",), 2.00),
    _Builtin("review.independent", "Run two independent final reviewers.", ReviewBatchInput,
             ActionKind.COMPOSITE, "epub.build", "reviews/agent_a", "reviews/agent_b",
             ("read_file", "write_file", "grep", "spawn_review_agent"), _QUALITY_SKILLS,
             ("chapters/src", "chapters/final", "references", "skills", "output"),
             ("reviews/agent_a", "reviews/agent_b", "reviews/revision_route.md"), 1.20),
    _Builtin("release.prepare", "Create a versioned release artifact.", ReleaseInput,
             ActionKind.DETERMINISTIC, ("review.spotcheck", "review.independent"), "output/release", "output/release",
             (), (), ("output", "reviews/random_spotcheck", "metadata"),
             ("output/release", "output/private_artifacts"), 0.0),
    _Builtin("output.finalize", "Write the final evidence manifest.", EmptyInput,
             ActionKind.AGENT, "release.prepare", "output/final_manifest.md", "output/final_manifest.md",
             ("read_file", "write_file", "grep"), (),
             ("output", "reviews", "metadata"), ("output/final_manifest.md",), 0.10),
    _Builtin("retrospective.capture", "Capture reusable findings without committing state.", EmptyInput,
             ActionKind.AGENT, "output.finalize", "retrospective", "retrospective",
             ("read_file", "write_file", "grep"), _QUALITY_SKILLS,
             ("qa", "reviews", "output", "skills"), ("retrospective",), 0.20),
)

_BY_CAPABILITY = {item.capability: item for item in _BUILTINS}


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
        action.capability == dependency and action.status == ActionStatus.SUCCEEDED
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
            arguments=(
                ActionArgument(name="capability", value_json=json.dumps(dependency)),
            ),
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


def _permissions_for(capability: str, parameters: FrozenModel) -> ActionPathPermissions:
    item = _BY_CAPABILITY[capability]
    read_dirs = list(item.read_set)
    write_dirs = list(item.write_set)
    read_files: list[str] = []
    write_files: list[str] = []

    if capability in {"chapter.translate", "chapter.control"}:
        if not isinstance(parameters, ChapterBatchInput):
            raise TypeError(f"{capability} requires ChapterBatchInput")
        read_dirs = [path for path in read_dirs if not path.startswith("chapters/")]
        write_dirs = [path for path in write_dirs if not path.startswith("chapters/") and not path.startswith("qa/chapter_controls")]
        for chapter in parameters.chapters:
            read_files.append(f"chapters/src/{chapter}.md")
            if capability == "chapter.control":
                read_files.append(f"chapters/translated/{chapter}.md")
                write_files.extend(
                    (
                        f"chapters/translated/{chapter}.md",
                        f"qa/chapter_controls/{chapter}.control.md",
                    )
                )
            else:
                write_files.append(f"chapters/translated/{chapter}.md")
    elif capability == "chapter.review":
        if not isinstance(parameters, ReviewBatchInput):
            raise TypeError("chapter.review requires ReviewBatchInput")
        read_dirs = [path for path in read_dirs if not path.startswith("chapters/")]
        write_dirs = []
        for chapter in parameters.chapters:
            read_files.extend(
                (f"chapters/src/{chapter}.md", f"chapters/translated/{chapter}.md")
            )
            write_files.extend(
                (
                    f"qa/fidelity/{chapter}.md",
                    f"qa/readability/{chapter}.md",
                    f"qa/imagery/{chapter}.imagery.md",
                    f"qa/terminology/{chapter}.md",
                    f"qa/gates/{chapter}.gate.md",
                    f"chapters/final/{chapter}.md",
                )
            )
    elif capability == "review.spotcheck":
        write_dirs = []

    return ActionPathPermissions(
        read_files=tuple(read_files),
        read_dirs=tuple(read_dirs),
        write_files=tuple(write_files),
        write_dirs=tuple(write_dirs),
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
        gate_permissions = permissions.model_copy(
            update={"write_dirs": ("reviews/random_spotcheck",)}
        )
    return ActionEnvelope(
        capability=capability,
        parameters=parameters,
        tools=tuple(ActionToolRef(name=name) for name in item.tools),
        permissions=permissions,
        gate_permissions=gate_permissions,
        skill_refs=item.skills,
    )


def _prompt_snapshot(
    context: ActionExecutionContext,
    parameters: FrozenModel,
    *,
    capability: str,
) -> ActionPromptSnapshot:
    project = context.project
    values = {
        "source_lang": context.source_lang,
        "target_lang": context.target_lang,
        "source_target": context.source_target,
        "publication_mode": context.publication_mode,
        "book_slug": context.book_slug,
        "profile": context.profile,
    }
    if capability != "chapter.translate" or not isinstance(parameters, ChapterBatchInput):
        return ActionPromptSnapshot(**values)
    source_parts = []
    for chapter in parameters.chapters:
        path = project.chapters_src / f"{chapter}.md"
        if path.exists():
            source_parts.append(f"## {chapter}\n{path.read_text(encoding='utf-8')}")
    rules = _style_rules(project.style_guide)
    terms = _matched_terms(project.terms_csv, "\n".join(source_parts))
    return ActionPromptSnapshot(
        **values,
        source_text="\n\n".join(source_parts),
        style_rules=rules,
        matched_terms=terms,
    )


def _style_rules(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    candidates = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            candidates.append(stripped[2:].strip())
        elif stripped and not stripped.startswith("#"):
            candidates.append(stripped)
    return tuple(item for item in candidates if item)[:8]


def _matched_terms(path: Path, source_text: str) -> tuple[str, ...]:
    if not path.exists():
        return ()
    matches: list[str] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            parsed = _GlossaryTerm.model_validate(row)
            if parsed.term and parsed.term in source_text:
                matches.append(f"{parsed.term} => {parsed.target}")
    return tuple(matches)


def _skill_context(project: BookProject, skill_refs: tuple[str, ...]) -> str:
    sections: list[str] = []
    for skill_ref in skill_refs:
        path = (project.root / skill_ref).resolve()
        if not project.within(path) or not path.is_file():
            raise ValueError(
                f"registered skill {skill_ref} is missing from the book project; "
                "scaffold the project assets before executing this Action"
            )
        sections.append(f"## Skill: {skill_ref}\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(sections)


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
        if self._tool_context is None or self._tool_context.project.root != context.project.root:
            return ActionOutcomeEnvelope(
                outcome=PermanentFailure(
                    error_code="action_runtime_not_bound",
                    message="Bind this catalog to the current ToolContext before dispatch.",
                )
            )
        envelope = build_action_envelope(self._capability, parameters)
        parameter_hash = hashlib.sha256(parameters.model_dump_json().encode()).hexdigest()[:12]
        action_id = context.action_id or f"{self._capability}:{parameter_hash}"
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
        )
        try:
            tools = belt.resolve(tuple(tool.name for tool in envelope.tools))
            snapshot = _prompt_snapshot(
                context,
                parameters,
                capability=self._capability,
            )
            user_prompt = self._prompts.render(self._capability, parameters, snapshot)
            skill_context = _skill_context(context.project, envelope.skill_refs)
            if skill_context:
                user_prompt = f"{user_prompt}\n\n# Action skills\n{skill_context}"
        except (OSError, TypeError, ValueError) as exc:
            return ActionOutcomeEnvelope(
                outcome=RepairRequired(
                    defect_codes=("action_envelope_invalid",),
                    message=str(exc),
                )
            )
        from abi.providers.agent_runtime import AgentActionRequest

        result = await self._tool_context.services.agent.run_action(
            AgentActionRequest(
                system_prompt=self._prompts.system_prompt(self._capability, snapshot),
                user_prompt=user_prompt,
                tools=tools,
                agent_name=self._capability.replace(".", "_"),
                thread_id=f"{context.run_id}:{self._capability}:{parameter_hash}",
                checkpoint_path=context.project.graph_checkpoints,
                max_iterations=40,
                may_have_side_effects=bool(envelope.permissions.write_files or envelope.permissions.write_dirs),
            )
        )
        return ActionOutcomeEnvelope(outcome=result.outcome)


class DeterministicActionExecutor:
    """Invoke existing deterministic ABI functions without changing control state."""

    def __init__(self, capability: str, *, tool_context: ToolContext | None) -> None:
        self._capability = capability
        self._tool_context = tool_context

    async def __call__(
        self, context: ActionExecutionContext, parameters: FrozenModel
    ) -> ActionOutcomeEnvelope:
        try:
            if self._capability in {"source.ingest", "source.split"}:
                if self._tool_context is None:
                    raise RuntimeError("bind ToolContext before executing source Actions")
                belt: ToolBelt = build_belt(
                    self._tool_context,
                    get_run_snapshot=lambda: context.snapshot,
                    permissions=_permissions_for(self._capability, parameters),
                )
                tool_name = "ingest_source" if self._capability == "source.ingest" else "split_chapters"
                tool = next(item for item in belt.content if item.name == tool_name)
                if isinstance(parameters, SourceIngestInput):
                    summary = str(tool.callable(source_relpath=parameters.source_relpath))
                elif isinstance(parameters, SourceSplitInput):
                    summary = str(
                        tool.callable(
                            source_relpath=parameters.source_relpath,
                            refine_toc=parameters.refine_toc,
                        )
                    )
                else:
                    raise TypeError(f"invalid typed parameters for {self._capability}")
                if summary.startswith("ERROR:"):
                    return ActionOutcomeEnvelope(
                        outcome=RepairRequired(
                            defect_codes=("deterministic_action_failed",), message=summary
                        )
                    )
                evidence = "source/source_manifest.json" if self._capability == "source.ingest" else "source/toc.json"
            elif self._capability == "epub.build":
                from abi.epub.assets import asset_manifest_check
                from abi.epub.build import build_epub
                from abi.epub.epubcheck import run_epubcheck
                from abi.epub.lint import publication_lint

                checks = (
                    publication_lint(
                        context.project,
                        runtime_metadata=GateRuntimeMetadata(
                            target_language=context.target_lang,
                            publication_mode=context.publication_mode,
                        ),
                    ),
                    asset_manifest_check(context.project),
                )
                failed = next((result for result in checks if not result.ok), None)
                if failed is None:
                    failed = build_epub(context.project)
                if failed.ok:
                    failed = run_epubcheck(context.project, context.project.book_epub)
                if not failed.ok:
                    return ActionOutcomeEnvelope(
                        outcome=RepairRequired(
                            defect_codes=("epub_gate_failed",), message=failed.summary()
                        )
                    )
                evidence = "output/book.epub"
            elif self._capability == "release.prepare":
                from abi.release.create import create_release

                if not isinstance(parameters, ReleaseInput):
                    raise TypeError("release.prepare requires ReleaseInput")
                result = create_release(
                    context.project,
                    version=parameters.version,
                    runtime_metadata=GateRuntimeMetadata(
                        target_language=context.target_lang,
                        publication_mode=context.publication_mode,
                    ),
                )
                if not result.ok:
                    return ActionOutcomeEnvelope(
                        outcome=RepairRequired(
                            defect_codes=("release_gate_failed",), message=result.summary()
                        )
                    )
                evidence = "output/release"
            else:
                raise RuntimeError(f"no deterministic executor for {self._capability}")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return ActionOutcomeEnvelope(
                outcome=PermanentFailure(
                    error_code="deterministic_action_error", message=str(exc)
                )
            )
        return ActionOutcomeEnvelope(outcome=Succeeded(staging_relpath=evidence))


def build_action_registry(*, tool_context: ToolContext | None = None) -> ActionRegistry:
    """Build and startup-validate ABI's complete built-in capability registry."""
    validators = validator_catalog()
    registry = ActionRegistry(
        predicates=PredicateCatalog({"action.succeeded": _action_succeeded}),
        validators=validators,
        tools=_declared_tools(),
        skill_refs=_QUALITY_SKILLS,
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
            skill_refs=item.skills,
            read_set=item.read_set,
            write_set=item.write_set,
            retry_policy=RetryPolicySpec(
                max_attempts=3,
                retryable_codes=("transient_provider_error", "provider_timeout"),
                base_delay_s=1.0,
                max_delay_s=30.0,
            ),
            validator=item.capability,
            resource_class="review" if item.kind == ActionKind.COMPOSITE else "default",
            estimated_cost_usd=item.estimated_cost,
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
            )
        )
    registry.validate_startup()
    return registry
