"""Behavioral tests for ABI's closed built-in Action catalog."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from abi.actions.builtins.catalog import (
    AgentActionExecutor,
    _prompt_snapshot,
    build_action_envelope,
    build_action_registry,
)
from abi.actions.builtins.inputs import (
    ChapterBatchInput,
    EmptyInput,
    ReleaseInput,
    ResearchInput,
    ReviewBatchInput,
    SourceIngestInput,
)
from abi.actions.contracts import ActionDefinition, ActionExecutionContext
from abi.actions.effects import expand_expected_artifacts
from abi.actions.predicates import PredicateCatalog
from abi.actions.registry import ActionRegistry, RegistryConfigurationError
from abi.actions.validators import _contains_field_pass
from abi.project.layout import BookProject
from abi.prompts.actions import ActionPromptRegistry, ActionPromptSnapshot
from abi.tools.belt import build_belt
from abi.tools.content import make_content_tools
from abi.tools.context import ToolContext
from abi.types.orchestration import (
    ActionArgument,
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
    snapshot = RunSnapshot(run_id="test-run", status=RunStatus.RUNNING)
    return SimpleNamespace(
        project=project,
        run_id="test-run",
        get_run_snapshot=lambda: snapshot,
        resolve=lambda path: root / path,
    )


def test_builtin_catalog_has_closed_dependencies_and_validators() -> None:
    registry = build_action_registry()
    registry.validate_startup()
    assert {item.capability for item in registry.specs()} >= {
        "source.ingest",
        "source.split",
        "research.global",
        "research.book",
        "translation.trial",
        "glossary.prepare",
        "chapter.translate",
        "chapter.control",
        "chapter.review",
        "preproduction.spec",
        "preproduction.sample",
        "epub.build",
        "review.spotcheck",
        "review.independent",
        "release.prepare",
        "output.finalize",
        "retrospective.capture",
    }


def test_builtin_registry_suggests_exact_source_chapters_to_planner(tmp_path: Path) -> None:
    """Catch source.split requiring chapter identities the Planner cannot observe."""
    project = BookProject(tmp_path)
    project.source_raw.parent.mkdir(parents=True)
    project.source_raw.write_text(
        "FIXTURE BOOK\n\nChapter 1: One\n\nFirst paragraph.\n\n"
        "Chapter 2: Two\n\nSecond paragraph.",
        encoding="utf-8",
    )
    snapshot = RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
    registry = build_action_registry(
        tool_context=ToolContext(
            project=project,
            services=SimpleNamespace(),  # type: ignore[arg-type]
            run_id="run-1",
            get_run_snapshot=lambda: snapshot,
        )
    )

    arguments = registry.get("source.split").fixed_arguments

    assert arguments == (
        ActionArgument(
            name="expected_chapters",
            value_json=(
                '["001_fixture_book","002_chapter_1_one","003_chapter_2_two"]'
            ),
        ),
        ActionArgument(name="refine_toc", value_json="true"),
        ActionArgument(
            name="source_relpath", value_json='"source/source_text_raw.txt"'
        ),
    )

    assert registry.get("review.independent").fixed_arguments == (
        ActionArgument(
            name="chapters",
            value_json='["001_fixture_book","002_chapter_1_one","003_chapter_2_two"]',
        ),
        ActionArgument(name="reviewers", value_json='["agent_a","agent_b"]'),
    )
    assert registry.get("review.spotcheck").fixed_arguments == (
        ActionArgument(name="round_id", value_json='"round_001"'),
        ActionArgument(name="reviewers", value_json='["agent_a","agent_b"]'),
        ActionArgument(
            name="chapters",
            value_json='["001_fixture_book","002_chapter_1_one","003_chapter_2_two"]',
        ),
        ActionArgument(name="samples_per_agent", value_json="1"),
        ActionArgument(name="seed", value_json="42"),
    )


def test_spotcheck_stops_after_one_validation_and_iteration_limit_is_retryable() -> None:
    registry = build_action_registry()
    prompt = ActionPromptRegistry().render(
        "review.spotcheck",
        registry.get("review.spotcheck").input_model(
            round_id="round_001",
            reviewers=("agent_a", "agent_b"),
            chapters=("001_fixture",),
            samples_per_agent=1,
            seed=42,
        ),
        ActionPromptSnapshot(),
    )

    assert "stop immediately whether it reports PASS or FAIL" in prompt
    assert "must not repeat sampling" in prompt
    assert "iteration_limit" in registry.get(
        "review.spotcheck"
    ).spec.retry_policy.retryable_codes


def test_agent_visible_tools_cannot_mutate_control_state() -> None:
    names = {tool.name for tool in build_belt(fake_context()).all()}
    assert names.isdisjoint({"set_state", "record_gate", "commit_action", "mark_done"})


def test_deterministic_content_tool_does_not_mutate_control_state(tmp_path: Path) -> None:
    project = BookProject(tmp_path)
    project.source_raw.parent.mkdir(parents=True)
    project.source_raw.write_text("Chapter 1\n\nA source paragraph.", encoding="utf-8")

    snapshot = RunSnapshot(run_id="test-run", status=RunStatus.RUNNING)
    context = ToolContext(
        project=project,
        services=SimpleNamespace(),
        run_id="test-run",
        get_run_snapshot=lambda: snapshot,
    )  # type: ignore[arg-type]
    ingest_source = next(
        tool for tool in make_content_tools(context) if tool.name == "ingest_source"
    )

    result = ingest_source.callable()

    assert "ingested" in result
    assert project.source_clean.exists()
    assert project.source_manifest.exists()
    assert not hasattr(context, "state")
    assert not hasattr(context, "save_state")


def test_action_receives_only_allowlisted_tools_and_paths() -> None:
    envelope = build_action_envelope("chapter.translate", ChapterBatchInput(chapters=("001",)))
    assert {tool.name for tool in envelope.tools} == {"read_file", "write_file", "grep"}
    assert envelope.permissions.can_write("chapters/translated/001.md")
    assert not envelope.permissions.can_write("glossary/terms.csv")


@pytest.mark.parametrize("capability", ("research.global", "research.book"))
def test_research_actions_receive_registered_translation_quality_skills(
    capability: str,
) -> None:
    """Catch research agents repeatedly requesting known quality skills without access."""
    envelope = build_action_envelope(capability, ResearchInput())

    assert envelope.skill_refs == (
        "skills/expert-translation-quality/SKILL.md",
        "skills/translation-quality-defect-families/SKILL.md",
    )
    assert all(not envelope.permissions.can_read(path) for path in envelope.skill_refs)


def test_chapter_pipeline_uses_immutable_disjoint_revision_paths() -> None:
    """Catch post-translation control trying to overwrite a committed translation."""
    chapters = ChapterBatchInput(chapters=("001",))
    translated = expand_expected_artifacts("chapter.translate", "translate", chapters)
    controlled = expand_expected_artifacts("chapter.control", "control", chapters)
    translated_paths = {entry.canonical_relpath for entry in translated.entries}
    controlled_paths = {entry.canonical_relpath for entry in controlled.entries}

    assert translated_paths.isdisjoint(controlled_paths)
    assert "chapters/controlled/001.md" in controlled_paths

    control = build_action_envelope("chapter.control", chapters).permissions
    review = build_action_envelope(
        "chapter.review", ReviewBatchInput(chapters=("001",))
    ).permissions
    assert control.can_read("chapters/translated/001.md")
    assert not control.can_read("chapters/src/001.md")
    assert control.can_write("chapters/controlled/001.md")
    assert not control.can_write("chapters/translated/001.md")
    assert review.can_read("chapters/controlled/001.md")
    assert not review.can_read("chapters/translated/001.md")


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
                outputs_current=True,
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
                outputs_current=True,
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
    snapshot = RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
    tool_context = ToolContext(
        project=project,
        services=SimpleNamespace(),  # type: ignore[arg-type]
        run_id="run-1",
        get_run_snapshot=lambda: snapshot,
    )
    registry = build_action_registry(tool_context=tool_context)
    result = await registry.get("source.ingest").executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            snapshot=snapshot,
        ),
        SourceIngestInput(source_relpath="source/alternate.txt"),
    )

    assert isinstance(result.outcome, Succeeded)
    clean = next(
        entry
        for entry in result.outcome.artifact_bundle.entries
        if entry.canonical_relpath == "source/source_text.txt"
    )
    assert "Alternate source." in (project.root / clean.staged_relpath).read_text(encoding="utf-8")
    assert not project.source_clean.exists()


@pytest.mark.asyncio
async def test_release_action_passes_typed_mode_without_legacy_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = BookProject(tmp_path)
    project.book_epub.parent.mkdir(parents=True)
    project.book_epub.write_bytes(b"epub")
    project.finalized_book_yaml.parent.mkdir(parents=True, exist_ok=True)
    project.finalized_book_yaml.write_text(
        "title: Fixture\nlanguage: zh-Hans\n", encoding="utf-8"
    )
    report = project.random_spotcheck_dir / "round_001/validation_report.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"status":"PASS"}', encoding="utf-8")

    def forbidden_state_access(self: BookProject) -> object:
        raise AssertionError("release.prepare must not read pipeline_state.json")

    monkeypatch.setattr(BookProject, "load_state", forbidden_state_access, raising=False)
    tool_context = SimpleNamespace(
        project=project,
        config=None,
        services=SimpleNamespace(),
        resolve=lambda relpath: (project.root / relpath).resolve(),
    )
    registry = build_action_registry(tool_context=tool_context)  # type: ignore[arg-type]
    assert registry.get("release.prepare").fixed_arguments == (
        ActionArgument(name="version", value_json='"v0.0.1"'),
    )

    result = await registry.get("release.prepare").executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            snapshot=RunSnapshot(run_id="run-1", status=RunStatus.RUNNING),
            action_id="release-1",
            target_lang="zh-Hans",
            publication_mode="public_domain",
        ),
        ReleaseInput(version="v0.0.1"),
    )

    assert isinstance(result.outcome, Succeeded)
    assert tuple(entry.canonical_relpath for entry in result.outcome.artifact_bundle.entries) == (
        "output/release/book_v0.0.1.epub",
        "output/release/release_state.json",
    )
    assert not project.release_dir.exists()


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


def test_preproduction_repair_prompt_requires_complete_replacement_generation() -> None:
    prompts = ActionPromptRegistry()
    repair = ActionPromptSnapshot(
        repair_context=("epub_quality_failed: metadata and typography disagree",),
    )

    rendered = prompts.render("preproduction.spec", EmptyInput(), repair)
    system = prompts.system_prompt("preproduction.spec", repair)

    assert "MUST call `write_file` for both" in rendered
    assert "Existing canonical outputs do not satisfy this repair Action" in system
    assert "Do not overwrite earlier canonical files" not in rendered


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
    system = prompts.system_prompt("chapter.translate", valid)
    assert "Source paragraph." in system
    assert all(rule in system for rule in valid.style_rules)
    assert "source => target" in system
    assert "EPUB" not in system
    assert "release" not in system.lower()
    assert "quality gate" not in system.lower()
    assert "skills/" not in system
    with pytest.raises(ValueError, match="5-8 style rules"):
        prompts.render(
            "chapter.translate",
            parameters,
            valid.model_copy(update={"style_rules": ("one", "two", "three", "four")}),
        )


def test_prompt_snapshot_never_reads_legacy_pipeline_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = BookProject(tmp_path)
    project.chapters_src.mkdir(parents=True)
    (project.chapters_src / "001.md").write_text("Source paragraph.", encoding="utf-8")
    project.style_guide.parent.mkdir(parents=True)
    project.style_guide.write_text(
        "\n".join(f"- rule {number}" for number in range(1, 6)), encoding="utf-8"
    )

    def forbidden_state_access() -> object:
        raise AssertionError("Action prompts must not read pipeline_state.json")

    monkeypatch.setattr(
        BookProject,
        "load_state",
        lambda self: forbidden_state_access(),
        raising=False,
    )
    context = ActionExecutionContext(
        project=project,
        run_id="run-1",
        snapshot=RunSnapshot(run_id="run-1", status=RunStatus.RUNNING),
        source_lang="en",
        target_lang="zh-Hans",
        book_slug="fixture",
    )
    tool_context = ToolContext(
        project=project,
        services=SimpleNamespace(),  # type: ignore[arg-type]
        run_id="run-1",
        get_run_snapshot=lambda: context.snapshot,
    )
    permissions = build_action_envelope(
        "chapter.translate", ChapterBatchInput(chapters=("001",))
    ).permissions

    prompt = _prompt_snapshot(
        context,
        ChapterBatchInput(chapters=("001",)),
        capability="chapter.translate",
        tool_context=tool_context,
        permissions=permissions,
    )

    assert prompt.source_lang == "en"
    assert prompt.target_lang == "zh-Hans"
    assert prompt.book_slug == "fixture"
    assert prompt.source_text == "## 001\nSource paragraph."


def test_translation_prompt_snapshot_rejects_symlinked_source(
    tmp_path: Path,
) -> None:
    project = BookProject(tmp_path)
    project.chapters_src.mkdir(parents=True)
    metadata = project.root / "metadata"
    metadata.mkdir(parents=True)
    secret = metadata / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    (project.chapters_src / "001.md").symlink_to(secret)
    project.style_guide.parent.mkdir(parents=True)
    project.style_guide.write_text(
        "\n".join(f"- rule {number}" for number in range(1, 6)), encoding="utf-8"
    )
    context = ActionExecutionContext(
        project=project,
        run_id="run-1",
        snapshot=RunSnapshot(run_id="run-1", status=RunStatus.RUNNING),
    )
    tool_context = ToolContext(
        project=project,
        services=SimpleNamespace(),  # type: ignore[arg-type]
        run_id="run-1",
        get_run_snapshot=lambda: context.snapshot,
    )
    parameters = ChapterBatchInput(chapters=("001",))
    permissions = build_action_envelope("chapter.translate", parameters).permissions

    with pytest.raises(PermissionError, match="symlink"):
        _prompt_snapshot(
            context,
            parameters,
            capability="chapter.translate",
            tool_context=tool_context,
            permissions=permissions,
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

    registry = ActionRegistry(predicates=PredicateCatalog(), validators={"unused": validator})
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
        effect_expander=expand_expected_artifacts,
    )

    with pytest.raises(RegistryConfigurationError, match="portable lowercase ASCII"):
        registry.register(definition)


def test_book_research_manifest_matches_prompt_and_project_layout() -> None:
    manifest = expand_expected_artifacts("research.book", "research-1", ResearchInput())

    assert tuple(item.canonical_relpath for item in manifest.entries) == (
        "metadata/book_specific_translation_research.md",
        "metadata/style_profile.md",
    )


def test_translation_trial_manifest_is_fixed_and_versioned() -> None:
    manifest = expand_expected_artifacts("translation.trial", "trial-1", EmptyInput())

    assert tuple(item.canonical_relpath for item in manifest.entries) == (
        "metadata/pretranslation_style_profile.md",
        "qa/pretranslation/pretranslation_report.md",
        "qa/pretranslation/source_01.md",
        "qa/pretranslation/source_02.md",
        "qa/pretranslation/source_03.md",
        "qa/pretranslation/source_04.md",
        "qa/pretranslation/source_05.md",
        "qa/pretranslation/trial_01.md",
        "qa/pretranslation/trial_02.md",
        "qa/pretranslation/trial_03.md",
        "qa/pretranslation/trial_04.md",
        "qa/pretranslation/trial_05.md",
    )


def test_fixed_agent_prompts_name_only_their_versioned_manifest_outputs() -> None:
    prompts = ActionPromptRegistry()
    snapshot = ActionPromptSnapshot()

    preproduction = prompts.render("preproduction.spec", EmptyInput(), snapshot)
    retrospective = prompts.render("retrospective.capture", EmptyInput(), snapshot)

    assert "MUST call `write_file` for both" in preproduction
    assert "obsolete generation" in preproduction
    assert "metadata/finalized_book.yaml" in preproduction
    assert tuple(
        item.canonical_relpath
        for item in expand_expected_artifacts(
            "preproduction.spec", "preproduction-1", EmptyInput()
        ).entries
    ) == (
        "metadata/finalized_book.yaml",
        "preproduction/stage1/production_spec.md",
    )
    assert "retrospective/retrospective.md" in retrospective
    assert "retrospective/book_retrospective.md" not in retrospective


@pytest.mark.parametrize("capability", ("chapter.control", "chapter.review"))
def test_chapter_postprocessing_prompts_render_authorized_chapter_names(
    capability: str,
) -> None:
    rendered = ActionPromptRegistry().render(
        capability,
        ChapterBatchInput(chapters=("001_intro", "002_body"))
        if capability == "chapter.control"
        else ReviewBatchInput(chapters=("001_intro", "002_body")),
        ActionPromptSnapshot(),
    )

    assert "001_intro" in rendered
    assert "002_body" in rendered
    assert "{NNN_slug}" not in rendered
    if capability == "chapter.control":
        assert 'expert_level_review_status: "PASS"' in rendered
        assert 'polysemy_translation_stage_review: "PASS"' in rendered
        assert 'polysemy_context_review: "PASS"' in rendered
        assert "Do not append a second expert-skill closure" in rendered


@pytest.mark.parametrize(
    "line",
    ("result: PASS", "**result: PASS**", "- **result: PASS**", "`result: PASS`"),
)
def test_gate_field_parser_accepts_common_markdown_wrappers(line: str) -> None:
    assert _contains_field_pass(f"# Report\n\n{line}\n")


@pytest.mark.parametrize(
    ("reason_code", "capability"),
    (
        ("pretranslation_not_passed", "translation.trial"),
        ("finalized_metadata_invalid", "preproduction.spec"),
        ("sample_review_not_passed", "preproduction.spec"),
        ("chapter_gate_not_passed", "chapter.review"),
        ("spotcheck_not_passed", "chapter.review"),
        ("translation_quality_failed", "chapter.review"),
        ("epub_quality_failed", "preproduction.spec"),
    ),
)
def test_builtin_semantic_failures_route_to_exact_repair_action(
    reason_code: str, capability: str
) -> None:
    registry = build_action_registry()

    assert registry.semantic_repair_capability(reason_code) == capability
