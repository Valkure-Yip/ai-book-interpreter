"""Project scaffolding + state-machine persistence."""

from __future__ import annotations

from pathlib import Path

from abi.project import ScaffoldRequest, Status, scaffold_project
from abi.project.state import HAPPY_PATH, happy_index


def _req(root: Path) -> ScaffoldRequest:
    return ScaffoldRequest(
        target_root=root / "zh-Hans",
        book_slug="测试书_作者",
        source_lang="en",
        target_lang="zh-Hans",
        source_target="en-zh-Hans",
    )


def test_scaffold_creates_contract_and_state(tmp_path: Path) -> None:
    project = scaffold_project(_req(tmp_path))
    assert project.root.name == "0001_测试书_作者"
    assert project.exists()
    assert (project.root / "references/quality_gate_framework.md").exists()
    assert (project.root / "references/quality_standard.md").exists()
    assert (project.root / "skills/expert-translation-quality/SKILL.md").exists()
    assert project.chapters_src.is_dir()
    st = project.load_state()
    assert st.status == Status.INIT
    assert st.source_target == "en-zh-Hans"


def test_state_round_trip_and_advance(tmp_path: Path) -> None:
    project = scaffold_project(_req(tmp_path))
    st = project.load_state()
    st.advance(Status.SOURCE_INGESTED, step="01", note="done")
    st.record_gate("pretranslation", "PASS")
    project.save_state(st)

    reloaded = project.load_state()
    assert reloaded.status == Status.SOURCE_INGESTED
    assert reloaded.gates["pretranslation"] == "PASS"
    assert reloaded.history[-1].step == "01"


def test_happy_path_is_monotonic() -> None:
    assert HAPPY_PATH[0] == Status.INIT
    assert HAPPY_PATH[-1] == Status.DONE
    assert happy_index(Status.RELEASE_PASS) < happy_index(Status.FINAL_OUTPUT_PASS)
    assert happy_index(Status.FAILED) == -1


def test_private_use_overlay(tmp_path: Path) -> None:
    req = ScaffoldRequest(
        target_root=tmp_path / "private" / "zh-Hans",
        book_slug="b",
        source_lang="en",
        target_lang="zh-Hans",
        source_target="en-zh-Hans",
        publication_mode="private_use",
    )
    project = scaffold_project(req)
    assert (project.root / "references/private_use_policy.md").exists()
    assert project.private_use_declaration.exists()
    assert (project.root / ".gitignore").exists()
