"""Attempt-only writes, typed effects, and staging-aware evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from abi.actions.builtins.catalog import build_action_registry
from abi.actions.builtins.inputs import (
    BuildEpubInput,
    ChapterBatchInput,
    ResearchInput,
    ReviewBatchInput,
    SourceIngestInput,
    SourceSplitInput,
    SpotcheckInput,
)
from abi.actions.contracts import ActionExecutionContext
from abi.actions.effects import expand_expected_artifacts
from abi.actions.evidence import StagingEvidenceView
from abi.actions.validators import validate_evidence
from abi.project.artifacts import ArtifactStore
from abi.project.layout import BookProject
from abi.tools.context import ToolContext
from abi.types.orchestration import (
    ArtifactBundle,
    ArtifactBundleEntry,
    ArtifactMetadata,
    ArtifactRef,
    RunSnapshot,
    RunStatus,
    Succeeded,
)


def test_attempt_writer_maps_logical_canonical_paths_and_records_ordered_effects(
    tmp_path: Path,
) -> None:
    """Catch a tool opening canonical output or returning an untyped single-file effect."""
    project = BookProject(tmp_path)
    store = ArtifactStore(project, None)
    try:
        writer = store.writer("a1", 1)
        entry = writer.write_bytes(
            "reports/a.json",
            b'{"ok":true}',
            media_type="application/json",
            evidence_role="report",
            metadata=(ArtifactMetadata(name="kind", value_json='"summary"'),),
        )
        assert entry.staged_relpath == "state/staging/a1/1/reports/a.json"
        assert entry.canonical_relpath == "reports/a.json"
        assert not (tmp_path / "reports/a.json").exists()
        assert (tmp_path / entry.staged_relpath).read_bytes() == b'{"ok":true}'
        assert writer.artifact_bundle().entries == (entry,)
    finally:
        store.close()


def test_effect_expansion_is_exact_sorted_and_parameter_sensitive() -> None:
    """Catch a write-set ceiling being mistaken for the required effect manifest."""
    manifest = expand_expected_artifacts(
        "chapter.translate", "a1", ChapterBatchInput(chapters=("002", "001"))
    )
    assert tuple(item.canonical_relpath for item in manifest.entries) == (
        "chapters/translated/001.md",
        "chapters/translated/002.md",
    )
    assert all(item.evidence_role == "translation" for item in manifest.entries)


def test_spotcheck_effects_are_frozen_from_durable_parameters() -> None:
    parameters = SpotcheckInput(
        round_id="round_007",
        reviewers=("agent_a", "agent_b"),
        chapters=("001", "002"),
        samples_per_agent=12,
        seed=41,
    )
    manifest = expand_expected_artifacts("review.spotcheck", "spot-1", parameters)
    assert tuple(item.canonical_relpath for item in manifest.entries) == (
        "reviews/random_spotcheck/round_007/reviews/agent_a_review.md",
        "reviews/random_spotcheck/round_007/reviews/agent_a_summary.json",
        "reviews/random_spotcheck/round_007/reviews/agent_b_review.md",
        "reviews/random_spotcheck/round_007/reviews/agent_b_summary.json",
        "reviews/random_spotcheck/round_007/round_manifest.json",
        "reviews/random_spotcheck/round_007/samples/agent_a/samples.json",
        "reviews/random_spotcheck/round_007/samples/agent_a/samples.md",
        "reviews/random_spotcheck/round_007/samples/agent_b/samples.json",
        "reviews/random_spotcheck/round_007/samples/agent_b/samples.md",
        "reviews/random_spotcheck/round_007/validation_report.json",
    )


def test_independent_review_effects_include_both_reviewers_and_revision_route() -> None:
    parameters = ReviewBatchInput(
        chapters=("001",), reviewers=("agent_a", "agent_b")
    )

    manifest = expand_expected_artifacts(
        "review.independent", "independent-1", parameters
    )

    assert tuple(
        (
            item.canonical_relpath,
            item.media_type,
            item.evidence_role,
            item.metadata,
        )
        for item in manifest.entries
    ) == (
        ("reviews/agent_a/review.md", "text/markdown", "independent_review", ()),
        ("reviews/agent_b/review.md", "text/markdown", "independent_review", ()),
        ("reviews/revision_route.md", "text/markdown", "revision_route", ()),
    )


@pytest.mark.asyncio
async def test_independent_review_success_writes_not_required_revision_route(
    tmp_path: Path,
) -> None:
    project = BookProject(tmp_path)
    for skill in (
        "skills/expert-translation-quality/SKILL.md",
        "skills/translation-quality-defect-families/SKILL.md",
    ):
        path = project.root / skill
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Review policy\n", encoding="utf-8")

    def provider_success() -> Succeeded:
        return Succeeded(
            artifact_bundle=ArtifactBundle(
                action_id="provider-result",
                attempt=1,
                entries=(
                    ArtifactBundleEntry(
                        staged_relpath="state/staging/provider-result/1/provider/result.json",
                        canonical_relpath="provider/result.json",
                        media_type="application/json",
                        evidence_role="provider_result",
                    ),
                ),
            )
        )

    class IndependentAgent:
        async def run_action(self, request: object) -> object:
            tools = {tool.name: tool.callable for tool in request.tools}  # type: ignore[attr-defined]
            agent_name = request.agent_name  # type: ignore[attr-defined]
            if agent_name == "review_independent":
                assert "status: NOT_REQUIRED" in request.user_prompt  # type: ignore[attr-defined]
                await tools["spawn_review_agent"](
                    agent_label="agent_a", instructions="translation review"
                )
                await tools["spawn_review_agent"](
                    agent_label="agent_b", instructions="EPUB review"
                )
                tools["write_file"](
                    path="reviews/revision_route.md",
                    content="status: NOT_REQUIRED\nresult: PASS\n",
                )
                return SimpleNamespace(outcome=provider_success())

            reviewer = agent_name.removeprefix("review_")
            tools["write_file"](
                path=f"reviews/{reviewer}/review.md",
                content=f"# {reviewer}\n\nresult: PASS\n",
            )
            return SimpleNamespace(outcome=provider_success())

    snapshot = RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
    tool_context = ToolContext(
        project=project,
        services=SimpleNamespace(agent=IndependentAgent()),  # type: ignore[arg-type]
        run_id="run-1",
        get_run_snapshot=lambda: snapshot,
    )
    result = await build_action_registry(tool_context=tool_context).get(
        "review.independent"
    ).executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            action_id="independent-1",
            attempt=1,
            snapshot=snapshot,
        ),
        ReviewBatchInput(reviewers=("agent_a", "agent_b")),
    )

    assert isinstance(result.outcome, Succeeded)
    assert tuple(
        item.canonical_relpath for item in result.outcome.artifact_bundle.entries
    ) == (
        "reviews/agent_a/review.md",
        "reviews/agent_b/review.md",
        "reviews/revision_route.md",
    )
    assert not (project.root / "reviews/revision_route.md").exists()


@pytest.mark.asyncio
async def test_spotcheck_executor_buffers_two_reviewers_then_flushes_exact_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ABI_SPOTCHECK_PASS_ROUNDS", "1")
    project = BookProject(tmp_path)
    project.chapters_final.mkdir(parents=True)
    (project.chapters_final / "001.md").write_text(
        "# One\n\nA complete paragraph for deterministic review.", encoding="utf-8"
    )
    for skill in (
        "skills/expert-translation-quality/SKILL.md",
        "skills/translation-quality-defect-families/SKILL.md",
    ):
        path = project.root / skill
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Review policy\n", encoding="utf-8")

    parameters = SpotcheckInput(
        round_id="round_001",
        reviewers=("agent_a", "agent_b"),
        chapters=("001",),
        samples_per_agent=1,
        seed=7,
    )
    files_seen_before_executor_flush: list[tuple[str, ...]] = []

    def provider_success() -> Succeeded:
        return Succeeded(
            artifact_bundle=ArtifactBundle(
                action_id="provider-result",
                attempt=1,
                entries=(
                    ArtifactBundleEntry(
                        staged_relpath="state/staging/provider-result/1/provider/result.json",
                        canonical_relpath="provider/result.json",
                        media_type="application/json",
                        evidence_role="provider_result",
                    ),
                ),
            )
        )

    class CompositeAgent:
        async def run_action(self, request: object) -> object:
            tools = {tool.name: tool.callable for tool in request.tools}  # type: ignore[attr-defined]
            agent_name = request.agent_name  # type: ignore[attr-defined]
            if agent_name == "review_spotcheck":
                tools["select_random_review_passages"](
                    agents=2, samples_per_agent=1, target_confidence=0.80
                )
                await tools["spawn_review_agent"](
                    agent_label="agent_a", instructions="review your frozen sample"
                )
                await tools["spawn_review_agent"](
                    agent_label="agent_b", instructions="review your frozen sample"
                )
                staged = project.root / "state/staging/spot-1/1"
                files_seen_before_executor_flush.append(
                    tuple(str(path.relative_to(project.root)) for path in staged.rglob("*") if path.is_file())
                    if staged.exists()
                    else ()
                )
                tools["validate_random_spotcheck"](require_pass=True)
                files_seen_before_executor_flush.append(
                    tuple(str(path.relative_to(project.root)) for path in staged.rglob("*") if path.is_file())
                    if staged.exists()
                    else ()
                )
                return SimpleNamespace(outcome=provider_success())

            reviewer = agent_name.removeprefix("review_")
            root = "reviews/random_spotcheck/round_001/reviews"
            tools["write_file"](
                path=f"{root}/{reviewer}_review.md",
                content=f"# {reviewer}\n\nPASS",
            )
            tools["write_file"](
                path=f"{root}/{reviewer}_summary.json",
                content=json.dumps(
                    {
                        "average_score": 96,
                        "lowest_score": 94,
                        "open_p0_p1_p2": 0,
                        "confidence": 0.95,
                        "samples": [{"unit_id": "001#0001", "score": 94}],
                    }
                ),
            )
            return SimpleNamespace(outcome=provider_success())

    snapshot = RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
    tool_context = ToolContext(
        project=project,
        services=SimpleNamespace(agent=CompositeAgent()),  # type: ignore[arg-type]
        run_id="run-1",
        get_run_snapshot=lambda: snapshot,
    )
    registry = build_action_registry(tool_context=tool_context)
    result = await registry.get("review.spotcheck").executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            action_id="spot-1",
            snapshot=snapshot,
        ),
        parameters,
    )

    assert isinstance(result.outcome, Succeeded)
    expected = expand_expected_artifacts("review.spotcheck", "spot-1", parameters)
    assert tuple(entry.canonical_relpath for entry in result.outcome.artifact_bundle.entries) == tuple(
        entry.canonical_relpath for entry in expected.entries
    )
    assert files_seen_before_executor_flush == [(), ()]
    assert all(
        (project.root / entry.staged_relpath).is_file()
        for entry in result.outcome.artifact_bundle.entries
    )
    assert all(
        not (project.root / entry.canonical_relpath).exists() for entry in expected.entries
    )


def test_staging_evidence_shadows_canonical_and_hides_other_attempts(tmp_path: Path) -> None:
    """Catch validators passing from prewritten canonical or another attempt's staging."""
    project = BookProject(tmp_path)
    canonical = project.root / "chapters/translated/001.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("forged canonical", encoding="utf-8")
    store = ArtifactStore(project, None)
    try:
        current = store.writer("a1", 1)
        current.write_bytes(
            "chapters/translated/001.md",
            b"current staged translation",
            media_type="text/markdown",
            evidence_role="translation",
        )
        other = store.writer("a1", 2)
        other.write_bytes(
            "chapters/translated/002.md",
            b"other attempt",
            media_type="text/markdown",
            evidence_role="translation",
        )
        bundle = current.artifact_bundle()
        view = StagingEvidenceView.for_bundle(project, (), bundle)
        assert view.read_text("chapters/translated/001.md") == "current staged translation"
        assert view.exists("chapters/translated/002.md") is False
        with pytest.raises(PermissionError):
            view.read_text("source/source_text_raw.txt")
        decision = validate_evidence(
            "chapter.translate",
            view,
            ChapterBatchInput(chapters=("001",)),
            bundle,
        )
        assert decision.passed is True
        assert decision.bundle_digest == view.bundle_digest
        assert decision.artifact_checksums == view.artifact_checksums
    finally:
        store.close()


def test_staging_evidence_rejects_symlinked_committed_parent(tmp_path: Path) -> None:
    project = BookProject(tmp_path)
    outside = project.root / "outside"
    outside.mkdir()
    (outside / "fact.txt").write_text("outside", encoding="utf-8")
    (project.root / "deps").symlink_to(outside, target_is_directory=True)
    store = ArtifactStore(project, None)
    try:
        writer = store.writer("a1", 1)
        writer.write_text("reports/current.txt", "current", evidence_role="report")
        committed = (
            ArtifactRef(
                artifact_id="dep",
                relpath="deps/fact.txt",
                sha256=hashlib.sha256(b"outside").hexdigest(),
                producer_action_id="dep-action",
            ),
        )
        view = StagingEvidenceView.for_bundle(project, committed, writer.artifact_bundle())
        with pytest.raises(ValueError, match=r"regular|symlink|unsafe"):
            view.read_bytes("deps/fact.txt")
    finally:
        store.close()


def test_committed_evidence_hash_and_bytes_come_from_same_open_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = BookProject(tmp_path)
    committed_path = project.root / "deps/fact.txt"
    committed_path.parent.mkdir(parents=True)
    committed_path.write_bytes(b"original")
    store = ArtifactStore(project, None)
    try:
        writer = store.writer("a1", 1)
        writer.write_text("reports/current.txt", "current", evidence_role="report")
        view = StagingEvidenceView.for_bundle(
            project,
            (
                ArtifactRef(
                    artifact_id="dep",
                    relpath="deps/fact.txt",
                    sha256=hashlib.sha256(b"original").hexdigest(),
                    producer_action_id="dep-action",
                ),
            ),
            writer.artifact_bundle(),
        )

        def replace_after_hash(path: Path) -> str:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            path.write_bytes(b"replacement")
            return digest

        monkeypatch.setattr(
            "abi.actions.evidence.sha256_file", replace_after_hash, raising=False
        )
        assert view.read_bytes("deps/fact.txt") == b"original"
    finally:
        store.close()


def test_staged_evidence_checksum_and_validator_reads_share_one_pinned_snapshot(
    tmp_path: Path,
) -> None:
    project = BookProject(tmp_path)
    store = ArtifactStore(project, None)
    try:
        writer = store.writer("research-global", 1)
        original = b"complete research evidence"
        writer.write_bytes(
            "qa/benchmark/global_research_ack.md",
            original,
            media_type="text/markdown",
            evidence_role="research",
        )
        bundle = writer.artifact_bundle()
        view = StagingEvidenceView.for_bundle(project, (), bundle)
        staged = project.root / bundle.entries[0].staged_relpath
        staged.unlink()
        staged.write_bytes(b"")

        decision = validate_evidence(
            "research.global", view, ResearchInput(), bundle
        )

        assert view.artifact_checksums == (hashlib.sha256(original).hexdigest(),)
        assert view.read_bytes("qa/benchmark/global_research_ack.md") == original
        assert decision.passed is True
    finally:
        store.close()


@pytest.mark.asyncio
async def test_real_source_ingest_writes_and_validates_only_attempt_staging(
    tmp_path: Path,
) -> None:
    """Catch a real deterministic built-in writing canonical output before validation."""
    project = BookProject(tmp_path)
    project.source_raw.parent.mkdir(parents=True)
    project.source_raw.write_text("Chapter 1\n\nA source paragraph.", encoding="utf-8")
    registry = build_action_registry()
    definition = registry.get("source.ingest")
    result = await definition.executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            action_id="ingest-1",
            attempt=1,
            snapshot=RunSnapshot(run_id="run-1", status=RunStatus.RUNNING),
        ),
        SourceIngestInput(),
    )
    assert result.action_id == "ingest-1"
    assert result.attempt == 1
    assert isinstance(result.outcome, Succeeded)
    assert not project.source_clean.exists()
    assert not project.source_manifest.exists()
    bundle = result.outcome.artifact_bundle
    view = StagingEvidenceView.for_bundle(project, (), bundle)
    decision = validate_evidence("source.ingest", view, SourceIngestInput(), bundle)
    assert decision.passed is True
    assert view.read_text("source/source_text.txt") == "A source paragraph."


@pytest.mark.asyncio
async def test_source_split_writes_only_its_exact_staged_toc(tmp_path: Path) -> None:
    project = BookProject(tmp_path)
    project.source_raw.parent.mkdir(parents=True)
    project.source_raw.write_text("Chapter 1\n\nA source paragraph.", encoding="utf-8")
    result = await build_action_registry().get("source.split").executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            action_id="split-1",
            attempt=1,
            snapshot=RunSnapshot(run_id="run-1", status=RunStatus.RUNNING),
        ),
        SourceSplitInput(
            refine_toc=False,
            expected_chapters=("001_chapter_1",),
        ),
    )
    assert isinstance(result.outcome, Succeeded)
    assert tuple(
        entry.canonical_relpath for entry in result.outcome.artifact_bundle.entries
    ) == ("chapters/src/001_chapter_1.md", "source/toc.json")
    assert not (project.root / "source/toc.json").exists()
    assert not project.chapters_src.exists()


@pytest.mark.asyncio
async def test_epub_build_uses_a_shadow_build_and_stages_every_effect(tmp_path: Path) -> None:
    project = BookProject(tmp_path)
    project.chapters_final.mkdir(parents=True)
    (project.chapters_final / "001.md").write_text("# Chapter\n\nText", encoding="utf-8")
    project.book_yaml.parent.mkdir(parents=True, exist_ok=True)
    project.book_yaml.write_text("title: Fixture\nlanguage: en\n", encoding="utf-8")
    result = await build_action_registry().get("epub.build").executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            action_id="epub-1",
            attempt=1,
            snapshot=RunSnapshot(run_id="run-1", status=RunStatus.RUNNING),
        ),
        BuildEpubInput(),
    )
    assert isinstance(result.outcome, Succeeded)
    assert tuple(
        entry.canonical_relpath for entry in result.outcome.artifact_bundle.entries
    ) == tuple(
        entry.canonical_relpath
        for entry in expand_expected_artifacts("epub.build", "epub-1", BuildEpubInput()).entries
    )
    assert not project.book_epub.exists()
    assert not project.publication_lint_report.exists()
    assert not project.asset_manifest_report.exists()
    assert not project.epubcheck_log.exists()
