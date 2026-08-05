"""Runtime path and tool permissions for built-in Actions."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from abi.actions.builtins.catalog import build_action_envelope
from abi.actions.builtins.inputs import (
    BuildEpubInput,
    ChapterBatchInput,
    EmptyInput,
    ReviewBatchInput,
    SpotcheckInput,
)
from abi.actions.effects import expand_expected_artifacts
from abi.project.artifacts import ArtifactStore
from abi.project.layout import BookProject
from abi.tools.fs import make_fs_tools
from abi.tools.gates import make_gate_tools
from abi.tools.permissions import ActionPathPermissions
from abi.types.tools import GateRuntimeMetadata


def _context(root: Path) -> SimpleNamespace:
    project = SimpleNamespace(
        root=root,
        within=lambda path: path.is_relative_to(root),
        rel=lambda path: path.relative_to(root).as_posix(),
        append_log=lambda line: None,
    )
    return SimpleNamespace(
        project=project,
        resolve=lambda relpath: (root / relpath).resolve(),
    )


def test_action_receives_only_allowlisted_tools_and_paths() -> None:
    envelope = build_action_envelope("chapter.translate", ChapterBatchInput(chapters=("001",)))
    assert {tool.name for tool in envelope.tools} == {"read_file", "write_file", "grep"}
    assert envelope.permissions.can_write("chapters/translated/001.md")
    assert not envelope.permissions.can_write("glossary/terms.csv")


def test_filesystem_handler_rechecks_action_permissions(tmp_path: Path) -> None:
    parameters = ChapterBatchInput(chapters=("001",))
    envelope = build_action_envelope("chapter.translate", parameters)
    project = BookProject(tmp_path)
    store = ArtifactStore(project, None)
    writer = store.writer("translate-001", 1)
    manifest = expand_expected_artifacts("chapter.translate", "translate-001", parameters)
    tools = make_fs_tools(
        _context(tmp_path),
        permissions=envelope.permissions,
        writer=writer,
        expected_artifacts={item.canonical_relpath: item for item in manifest.entries},
    )
    write_file = next(tool for tool in tools if tool.name == "write_file")

    write_file.callable(path="chapters/translated/001.md", content="allowed")
    with pytest.raises(PermissionError, match="not allowed to write"):
        write_file.callable(path="glossary/terms.csv", content="forbidden")

    assert (tmp_path / "state/staging/translate-001/1/chapters/translated/001.md").read_text(
        encoding="utf-8"
    ) == "allowed"
    assert not (tmp_path / "chapters/translated/001.md").exists()
    assert not (tmp_path / "glossary/terms.csv").exists()
    store.close()


@pytest.mark.parametrize("chapter", ("../001", "Chapter-01", "章节一", "001/other"))
def test_chapter_parameters_reject_nonportable_or_escaping_names(chapter: str) -> None:
    with pytest.raises(ValidationError, match="chapter"):
        ChapterBatchInput(chapters=(chapter,))


def test_unknown_capability_cannot_receive_a_default_permission_set() -> None:
    with pytest.raises(KeyError, match=r"invented\.capability"):
        build_action_envelope("invented.capability", ChapterBatchInput(chapters=("001",)))


def test_reviewers_cannot_write_canonical_translation_or_output() -> None:
    envelope = build_action_envelope(
        "review.spotcheck",
        SpotcheckInput(
            round_id="round_001",
            reviewers=("agent_a", "agent_b"),
            chapters=("001",),
            samples_per_agent=1,
            seed=1,
        ),
    )

    assert envelope.permissions.can_read("chapters/final/001.md")
    assert not envelope.permissions.can_write(
        "reviews/random_spotcheck/round_001/validation_report.json"
    )
    assert not envelope.permissions.can_write("chapters/final/001.md")
    assert not envelope.permissions.can_write("output/book.epub")


def test_chapter_review_batches_have_disjoint_exact_qa_outputs() -> None:
    first = build_action_envelope("chapter.review", ReviewBatchInput(chapters=("001",)))
    second = build_action_envelope("chapter.review", ReviewBatchInput(chapters=("002",)))

    assert first.permissions.can_write("qa/fidelity/001.md")
    assert not first.permissions.can_write("qa/fidelity/002.md")
    assert second.permissions.can_write("qa/fidelity/002.md")
    assert not second.permissions.can_write("qa/fidelity/001.md")
    assert set(first.permissions.write_files).isdisjoint(second.permissions.write_files)
    assert first.permissions.write_dirs == ()
    assert second.permissions.write_dirs == ()


def test_retrospective_can_propose_but_not_mutate_shared_skills() -> None:
    envelope = build_action_envelope("retrospective.capture", EmptyInput())

    assert envelope.permissions.can_write("retrospective/template_update_suggestions.md")
    assert not envelope.permissions.can_write("skills/translation-quality-defect-families/skill.md")


def test_build_epub_input_rejects_ignored_path_and_chapter_options() -> None:
    with pytest.raises(ValidationError):
        BuildEpubInput(chapter_slugs=("001",))
    with pytest.raises(ValidationError):
        BuildEpubInput(output_relpath="output/alternate.epub")


def test_gate_handler_checks_all_read_roots_before_calling_lower_layer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = BookProject(tmp_path)
    called = False

    def forbidden_lower_layer(project: BookProject) -> object:
        nonlocal called
        called = True
        raise AssertionError("lower layer must not run without declared read roots")

    monkeypatch.setattr("abi.epub.lint.publication_lint", forbidden_lower_layer)
    context = SimpleNamespace(project=project, resolve=lambda path: project.root / path)
    permissions = ActionPathPermissions(
        read_dirs=("chapters/final", "frontmatter"),
        write_files=("output/publication_lint.json",),
    )
    tool = next(
        item
        for item in make_gate_tools(context, permissions=permissions)
        if item.name == "publication_lint"
    )

    with pytest.raises(PermissionError, match="metadata"):
        tool.callable()
    assert called is False


def test_publication_lint_gate_uses_typed_metadata_without_legacy_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = BookProject(tmp_path)
    project.book_yaml.parent.mkdir(parents=True)
    project.book_yaml.write_text("title: Fixture\nlanguage: zh-Hans\n", encoding="utf-8")
    project.chapters_final.mkdir(parents=True)
    (project.chapters_final / "001.md").write_text("# 第一章\n\n正文。\n", encoding="utf-8")
    (project.root / "frontmatter").mkdir()

    def forbidden_state_access(self: BookProject) -> object:
        raise AssertionError("Action gate must not read pipeline_state.json")

    monkeypatch.setattr(BookProject, "load_state", forbidden_state_access, raising=False)
    permissions = ActionPathPermissions(
        read_dirs=("frontmatter", "chapters/final", "metadata"),
        write_files=("output/publication_lint.json",),
    )
    context = SimpleNamespace(project=project, resolve=lambda path: project.root / path)
    store = ArtifactStore(project, None)
    writer = store.writer("lint-1", 1)
    tool = next(
        item
        for item in make_gate_tools(
            context,
            permissions=permissions,
            runtime_metadata=GateRuntimeMetadata(
                target_language="zh-Hans", publication_mode="public_domain"
            ),
            writer=writer,
        )
        if item.name == "publication_lint"
    )

    assert tool.callable().startswith("PASS:")
    assert writer.staged_path("output/publication_lint.json").is_file()
    assert not project.publication_lint_report.exists()
    store.close()


def _release_gate_project(tmp_path: Path) -> BookProject:
    project = BookProject(tmp_path)
    project.book_epub.parent.mkdir(parents=True)
    project.book_epub.write_bytes(b"epub")
    project.book_yaml.parent.mkdir(parents=True, exist_ok=True)
    project.book_yaml.write_text("title: Fixture\nlanguage: zh-Hans\n", encoding="utf-8")
    report = project.random_spotcheck_dir / "round_001/validation_report.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"status":"PASS"}', encoding="utf-8")
    return project


def test_release_gate_uses_typed_mode_without_legacy_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _release_gate_project(tmp_path)

    def forbidden_state_access(self: BookProject) -> object:
        raise AssertionError("Action release must not read pipeline_state.json")

    monkeypatch.setattr(BookProject, "load_state", forbidden_state_access, raising=False)
    permissions = ActionPathPermissions(
        read_files=("output/book.epub",),
        read_dirs=("reviews/random_spotcheck", "metadata"),
        write_dirs=("output/release",),
    )
    context = SimpleNamespace(project=project, resolve=lambda path: project.root / path)
    tool = next(
        item
        for item in make_gate_tools(
            context,
            permissions=permissions,
            runtime_metadata=GateRuntimeMetadata(
                target_language="zh-Hans", publication_mode="public_domain"
            ),
        )
        if item.name == "create_release"
    )

    with pytest.raises(PermissionError, match="deterministic built-in"):
        tool.callable(version="v0.0.1")
    assert not project.release_dir.exists()


@pytest.mark.parametrize(
    "missing_root",
    ("output/book.epub", "reviews/random_spotcheck", "metadata", "output/release"),
)
def test_release_gate_checks_every_read_and_destination_root_before_lower_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_root: str,
) -> None:
    project = _release_gate_project(tmp_path)
    called = False

    def forbidden_lower_layer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("release lower layer must not run without every root")

    monkeypatch.setattr("abi.release.create.create_release", forbidden_lower_layer)
    read_files = () if missing_root == "output/book.epub" else ("output/book.epub",)
    read_dirs = tuple(
        root for root in ("reviews/random_spotcheck", "metadata") if root != missing_root
    )
    write_dirs = () if missing_root == "output/release" else ("output/release",)
    permissions = ActionPathPermissions(
        read_files=read_files,
        read_dirs=read_dirs,
        write_dirs=write_dirs,
    )
    context = SimpleNamespace(project=project, resolve=lambda path: project.root / path)
    tool = next(
        item
        for item in make_gate_tools(
            context,
            permissions=permissions,
            runtime_metadata=GateRuntimeMetadata(
                target_language="zh-Hans", publication_mode="public_domain"
            ),
        )
        if item.name == "create_release"
    )

    with pytest.raises(PermissionError, match=missing_root):
        tool.callable(version="v0.0.1")
    assert called is False


def test_release_gate_legacy_fallback_authorizes_the_inferred_private_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _release_gate_project(tmp_path)
    project.private_use_declaration.write_text("private", encoding="utf-8")
    called = False

    def forbidden_lower_layer(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("lower layer must not cross the authorized release mode")

    monkeypatch.setattr("abi.release.create.create_release", forbidden_lower_layer)
    permissions = ActionPathPermissions(
        read_files=("output/book.epub",),
        read_dirs=("reviews/random_spotcheck", "metadata"),
        write_dirs=("output/release",),
    )
    context = SimpleNamespace(project=project, resolve=lambda path: project.root / path)
    tool = next(
        item
        for item in make_gate_tools(context, permissions=permissions)
        if item.name == "create_release"
    )

    with pytest.raises(PermissionError, match="output/private_artifacts"):
        tool.callable(version="v0.0.1")
    assert called is False


def test_action_gate_lower_layers_do_not_reference_legacy_state() -> None:
    from abi.epub.lint import publication_lint
    from abi.release.create import create_release

    assert "load_state" not in inspect.getsource(publication_lint)
    assert "load_state" not in inspect.getsource(create_release)
