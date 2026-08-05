"""Runtime path and tool permissions for built-in Actions."""

from __future__ import annotations

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
)
from abi.project.layout import BookProject
from abi.tools.fs import make_fs_tools
from abi.tools.gates import make_gate_tools
from abi.tools.permissions import ActionPathPermissions


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
    envelope = build_action_envelope("chapter.translate", ChapterBatchInput(chapters=("001",)))
    tools = make_fs_tools(_context(tmp_path), permissions=envelope.permissions)
    write_file = next(tool for tool in tools if tool.name == "write_file")

    write_file.callable(path="chapters/translated/001.md", content="allowed")
    with pytest.raises(PermissionError, match="not allowed to write"):
        write_file.callable(path="glossary/terms.csv", content="forbidden")

    assert (tmp_path / "chapters/translated/001.md").read_text(encoding="utf-8") == "allowed"
    assert not (tmp_path / "glossary/terms.csv").exists()


@pytest.mark.parametrize("chapter", ("../001", "Chapter-01", "章节一", "001/other"))
def test_chapter_parameters_reject_nonportable_or_escaping_names(chapter: str) -> None:
    with pytest.raises(ValidationError, match="chapter"):
        ChapterBatchInput(chapters=(chapter,))


def test_unknown_capability_cannot_receive_a_default_permission_set() -> None:
    with pytest.raises(KeyError, match=r"invented\.capability"):
        build_action_envelope("invented.capability", ChapterBatchInput(chapters=("001",)))


def test_reviewers_cannot_write_canonical_translation_or_output() -> None:
    envelope = build_action_envelope("review.spotcheck", ReviewBatchInput())

    assert envelope.permissions.can_read("chapters/final/001.md")
    assert not envelope.permissions.can_write("reviews/random_spotcheck/round_001/validation_report.json")
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
    assert not envelope.permissions.can_write(
        "skills/translation-quality-defect-families/skill.md"
    )


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
        item for item in make_gate_tools(context, permissions=permissions)
        if item.name == "publication_lint"
    )

    with pytest.raises(PermissionError, match="metadata"):
        tool.callable()
    assert called is False
