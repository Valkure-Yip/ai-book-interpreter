"""Behavioral tests for ABI's closed built-in Action catalog."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from abi.actions.builtins.catalog import (
    AgentActionExecutor,
    build_action_envelope,
    build_action_registry,
)
from abi.actions.builtins.inputs import ChapterBatchInput, EmptyInput, SourceIngestInput
from abi.actions.contracts import ActionDefinition, ActionExecutionContext
from abi.actions.predicates import PredicateCatalog
from abi.actions.registry import ActionRegistry, RegistryConfigurationError
from abi.project.layout import BookProject
from abi.prompts.actions import ActionPromptRegistry, ActionPromptSnapshot
from abi.tools.belt import build_belt
from abi.tools.content import make_content_tools
from abi.types.orchestration import (
    ActionKind,
    ActionSpec,
    ActionStatus,
    ActionView,
    GateDecision,
    RunSnapshot,
    RunStatus,
    Succeeded,
)


def fake_context() -> SimpleNamespace:
    root = Path("/tmp/abi-task-7-fake-project")
    project = SimpleNamespace(
        root=root,
        graph_checkpoints=root / "state/graph_checkpoints.sqlite",
        within=lambda path: path.is_relative_to(root),
        rel=lambda path: path.relative_to(root).as_posix(),
        append_log=lambda line: None,
    )
    return SimpleNamespace(project=project, resolve=lambda path: root / path)


def test_builtin_catalog_has_closed_dependencies_and_validators() -> None:
    registry = build_action_registry()
    registry.validate_startup()
    assert {item.capability for item in registry.specs()} >= {
        "source.ingest", "source.split", "research.global", "research.book",
        "translation.trial", "glossary.prepare", "chapter.translate",
        "chapter.control", "chapter.review", "preproduction.spec",
        "preproduction.sample", "epub.build", "review.spotcheck",
        "review.independent", "release.prepare", "output.finalize",
        "retrospective.capture",
    }


def test_agent_visible_tools_cannot_mutate_control_state() -> None:
    names = {tool.name for tool in build_belt(fake_context()).all()}
    assert names.isdisjoint({"set_state", "record_gate", "commit_action", "mark_done"})


def test_deterministic_content_tool_does_not_mutate_control_state(tmp_path: Path) -> None:
    project = BookProject(tmp_path)
    project.source_raw.parent.mkdir(parents=True)
    project.source_raw.write_text("Chapter 1\n\nA source paragraph.", encoding="utf-8")

    def forbidden_state_access() -> object:
        raise AssertionError("content work must not read or mutate control state")

    context = SimpleNamespace(
        project=project,
        config=SimpleNamespace(refine_toc=False),
        state=forbidden_state_access,
        save_state=lambda state: forbidden_state_access(),
        resolve=lambda relpath: (project.root / relpath).resolve(),
    )
    ingest_source = next(
        tool for tool in make_content_tools(context) if tool.name == "ingest_source"
    )

    result = ingest_source.callable()

    assert "ingested" in result
    assert project.source_clean.exists()
    assert project.source_manifest.exists()


def test_action_receives_only_allowlisted_tools_and_paths() -> None:
    envelope = build_action_envelope("chapter.translate", ChapterBatchInput(chapters=("001",)))
    assert {tool.name for tool in envelope.tools} == {"read_file", "write_file", "grep"}
    assert envelope.permissions.can_write("chapters/translated/001.md")
    assert not envelope.permissions.can_write("glossary/terms.csv")


def test_agent_capabilities_share_one_executor_type() -> None:
    registry = build_action_registry()
    agent_definitions = tuple(
        registry.get(spec.capability)
        for spec in registry.specs()
        if spec.action_kind.value == "agent"
    )

    assert agent_definitions
    assert all(isinstance(item.executor, AgentActionExecutor) for item in agent_definitions)


def test_multi_dependency_capabilities_fail_closed_until_every_dependency_passes() -> None:
    registry = build_action_registry()
    only_book_research = RunSnapshot(
        run_id="run-1",
        status=RunStatus.RUNNING,
        actions=(
            ActionView(
                action_id="book-research",
                capability="research.book",
                status=ActionStatus.SUCCEEDED,
            ),
        ),
    )
    only_spotcheck = RunSnapshot(
        run_id="run-1",
        status=RunStatus.RUNNING,
        actions=(
            ActionView(
                action_id="spotcheck",
                capability="review.spotcheck",
                status=ActionStatus.SUCCEEDED,
            ),
        ),
    )

    assert "translation.trial" not in {
        action.capability for action in registry.eligible(only_book_research)
    }
    assert "release.prepare" not in {
        action.capability for action in registry.eligible(only_spotcheck)
    }


@pytest.mark.asyncio
async def test_source_action_executes_the_typed_source_path(tmp_path: Path) -> None:
    project = BookProject(tmp_path)
    alternate = project.root / "source/alternate.txt"
    alternate.parent.mkdir(parents=True)
    alternate.write_text("Chapter 1\n\nAlternate source.", encoding="utf-8")
    tool_context = SimpleNamespace(
        project=project,
        config=None,
        services=SimpleNamespace(),
        resolve=lambda relpath: (project.root / relpath).resolve(),
    )
    registry = build_action_registry(tool_context=tool_context)  # type: ignore[arg-type]
    result = await registry.get("source.ingest").executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            snapshot=RunSnapshot(run_id="run-1", status=RunStatus.RUNNING),
        ),
        SourceIngestInput(source_relpath="source/alternate.txt"),
    )

    assert isinstance(result.outcome, Succeeded)
    assert "Alternate source." in project.source_clean.read_text(encoding="utf-8")


def test_action_prompt_registry_selects_by_capability_and_rejects_unknown() -> None:
    prompts = ActionPromptRegistry()
    rendered = prompts.render(
        "source.ingest",
        EmptyInput(),
        ActionPromptSnapshot(
            source_lang="en",
            target_lang="zh-Hans",
            source_target="en-zh-Hans",
            publication_mode="public_domain",
            book_slug="fixture",
        ),
    )

    assert "Ingest & clean" in rendered
    assert "set_state" not in rendered
    with pytest.raises(KeyError, match=r"invented\.capability"):
        prompts.render("invented.capability", EmptyInput(), ActionPromptSnapshot())


def test_translation_prompt_requires_source_and_five_to_eight_style_rules() -> None:
    prompts = ActionPromptRegistry()
    parameters = ChapterBatchInput(chapters=("001",))
    valid = ActionPromptSnapshot(
        source_text="# Chapter\n\nSource paragraph.",
        style_rules=("one", "two", "three", "four", "five"),
        matched_terms=("source => target",),
    )

    rendered = prompts.render("chapter.translate", parameters, valid)

    assert "Source paragraph." in rendered
    assert all(rule in rendered for rule in valid.style_rules)
    assert "source => target" in rendered
    assert "Translate every" not in rendered
    assert "glob chapters" not in rendered
    with pytest.raises(ValueError, match="5-8 style rules"):
        prompts.render(
            "chapter.translate",
            parameters,
            valid.model_copy(update={"style_rules": ("one", "two", "three", "four")}),
        )


@pytest.mark.parametrize(
    ("capability", "read_set"),
    (
        ("Chapter.Translate", ("chapters/src",)),
        ("chapter.translate", ("chapters/译文",)),
        ("chapter.translate", ("../chapters",)),
    ),
)
def test_registry_rejects_nonportable_machine_namespaces(
    capability: str, read_set: tuple[str, ...]
) -> None:
    def validator(project: object, parameters: object) -> GateDecision:
        return GateDecision(passed=False, reason_code="unused", message="unused")

    async def executor(context: object, parameters: object) -> object:
        raise AssertionError("not executed")

    registry = ActionRegistry(
        predicates=PredicateCatalog(), validators={"unused": validator}
    )
    definition = ActionDefinition(
        spec=ActionSpec(
            capability=capability,
            description="invalid namespace fixture",
            input_schema="EmptyInput",
            action_kind=ActionKind.AGENT,
            read_set=read_set,
            validator="unused",
        ),
        input_model=EmptyInput,
        executor=executor,  # type: ignore[arg-type]
        validator=validator,  # type: ignore[arg-type]
    )

    with pytest.raises(RegistryConfigurationError, match="portable lowercase ASCII"):
        registry.register(definition)
