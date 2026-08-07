"""Attempt-only writes, typed effects, and staging-aware evidence."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from threading import Barrier, BrokenBarrierError
from types import SimpleNamespace

import pytest

from abi.actions.builtins.catalog import build_action_registry
from abi.actions.builtins.inputs import (
    BuildEpubInput,
    ChapterBatchInput,
    EmptyInput,
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
from abi.project.artifacts import (
    ArtifactConflictError,
    ArtifactStore,
    BufferedAttemptWriter,
)
from abi.project.layout import BookProject
from abi.tools.context import ToolContext
from abi.types.orchestration import (
    AgentCompleted,
    ArtifactBundleEntry,
    ArtifactMetadata,
    ArtifactRef,
    PermanentFailure,
    RunSnapshot,
    RunStatus,
    Succeeded,
)


def _approved_hitl_resume() -> object:
    from abi.providers.agent_runtime import (
        HitlDecision,
        HitlInterruptDecision,
        HitlResume,
    )

    return HitlResume(
        interrupts=(
            HitlInterruptDecision(
                interrupt_id="interrupt-1",
                decisions=(HitlDecision(decision="approve"),),
            ),
        )
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


def test_attempt_writer_canonicalizes_agent_write_order_at_bundle_boundary(
    tmp_path: Path,
) -> None:
    """Parallel tool scheduling must not decide durable artifact identity order."""
    project = BookProject(tmp_path)
    store = ArtifactStore(project, None)
    try:
        writer = store.writer("a1", 1)
        writer.write_text(
            "metadata/style_profile.md",
            "style",
            media_type="text/markdown",
            evidence_role="style_profile",
        )
        writer.write_text(
            "metadata/book_specific_translation_research.md",
            "research",
            media_type="text/markdown",
            evidence_role="research",
        )

        assert tuple(
            item.canonical_relpath for item in writer.artifact_bundle().entries
        ) == (
            "metadata/book_specific_translation_research.md",
            "metadata/style_profile.md",
        )
    finally:
        store.close()


def test_buffered_attempt_writer_serializes_concurrent_duplicate_writes() -> None:
    class RacingDict(dict[str, tuple[bytes, str, str, tuple[ArtifactMetadata, ...]]]):
        def __init__(self) -> None:
            super().__init__()
            self.barrier = Barrier(2)

        def __contains__(self, key: object) -> bool:
            present = super().__contains__(key)
            if not present:
                with suppress(BrokenBarrierError):
                    self.barrier.wait(timeout=0.1)
            return present

    writer = BufferedAttemptWriter("review-1", 1)
    writer._items = RacingDict()  # type: ignore[attr-defined]

    def write() -> str:
        try:
            writer.write_text(
                "reviews/a.md",
                "review",
                media_type="text/markdown",
                evidence_role="review",
            )
        except FileExistsError:
            return "duplicate"
        return "written"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(lambda _: write(), range(2)))

    assert sorted(outcomes) == ["duplicate", "written"]
    assert tuple(item.canonical_relpath for item in writer.entries) == (
        "reviews/a.md",
    )


@pytest.mark.asyncio
async def test_agent_completion_without_required_outputs_is_bounded_retry(
    tmp_path: Path,
) -> None:
    project = BookProject(tmp_path)
    for skill in (
        "skills/expert-translation-quality/SKILL.md",
        "skills/translation-quality-defect-families/SKILL.md",
    ):
        path = project.root / skill
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# policy\n", encoding="utf-8")

    class EmptyAgent:
        async def run_action(self, request: object) -> object:
            return SimpleNamespace(outcome=AgentCompleted(summary="done"))

    snapshot = RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
    tool_context = ToolContext(
        project=project,
        services=SimpleNamespace(agent=EmptyAgent()),  # type: ignore[arg-type]
        run_id="run-1",
        get_run_snapshot=lambda: snapshot,
    )

    result = await build_action_registry(tool_context=tool_context).get(
        "chapter.control"
    ).executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            action_id="control-1",
            snapshot=snapshot,
        ),
        ChapterBatchInput(chapters=("001",)),
    )

    assert result.outcome.kind == "retryable_failure"
    assert result.outcome.error_code == "agent_incomplete_outputs"
    assert "chapters/controlled/001.md" in result.outcome.message


@pytest.mark.asyncio
async def test_agent_completes_missing_manifest_in_same_attempt(
    tmp_path: Path,
) -> None:
    project = BookProject(tmp_path)
    for skill in (
        "skills/expert-translation-quality/SKILL.md",
        "skills/translation-quality-defect-families/SKILL.md",
    ):
        path = project.root / skill
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# policy\n", encoding="utf-8")

    requests: list[object] = []

    class PartialThenCompleteAgent:
        async def run_action(self, request: object) -> object:
            requests.append(request)
            write_file = next(
                tool
                for tool in request.tools  # type: ignore[attr-defined]
                if tool.name == "write_file"
            )
            if len(requests) == 1:
                write_file.callable(
                    path="chapters/controlled/001.md", content="# controlled\n"
                )
            else:
                write_file.callable(
                    path="qa/chapter_controls/001.control.md",
                    content="result: PASS\n",
                )
            return SimpleNamespace(outcome=AgentCompleted(summary="done"))

    snapshot = RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
    tool_context = ToolContext(
        project=project,
        services=SimpleNamespace(agent=PartialThenCompleteAgent()),  # type: ignore[arg-type]
        run_id="run-1",
        get_run_snapshot=lambda: snapshot,
    )

    result = await build_action_registry(tool_context=tool_context).get(
        "chapter.control"
    ).executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            action_id="control-1",
            snapshot=snapshot,
        ),
        ChapterBatchInput(chapters=("001",)),
    )

    assert isinstance(result.outcome, Succeeded)
    assert len(result.outcome.artifact_bundle.entries) == 2
    assert len(requests) == 2
    assert requests[1].thread_id.endswith(":manifest-completion")  # type: ignore[attr-defined]
    assert requests[0].user_prompt in requests[1].user_prompt  # type: ignore[attr-defined]
    assert "write only every missing output" in requests[1].user_prompt  # type: ignore[attr-defined]
    assert "qa/chapter_controls/001.control.md" in requests[1].user_prompt  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_chapter_review_uses_one_isolated_agent_loop_per_chapter(
    tmp_path: Path,
) -> None:
    project = BookProject(tmp_path)
    for skill in (
        "skills/expert-translation-quality/SKILL.md",
        "skills/translation-quality-defect-families/SKILL.md",
    ):
        path = project.root / skill
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# policy\n", encoding="utf-8")

    requests: list[object] = []

    class WritingAgent:
        async def run_action(self, request: object) -> object:
            requests.append(request)
            thread_id = request.thread_id  # type: ignore[attr-defined]
            chapter = thread_id.rsplit(":", 1)[1]
            write_file = next(
                tool
                for tool in request.tools  # type: ignore[attr-defined]
                if tool.name == "write_file"
            )
            for relpath in (
                f"chapters/final/{chapter}.md",
                f"qa/fidelity/{chapter}.md",
                f"qa/gates/{chapter}.gate.md",
                f"qa/imagery/{chapter}.imagery.md",
                f"qa/readability/{chapter}.md",
                f"qa/terminology/{chapter}.md",
            ):
                content = (
                    '所谓"现金交易"和\'永恒真理\'。\n'
                    if relpath.startswith("chapters/final/")
                    else "result: PASS\n"
                )
                write_file.callable(path=relpath, content=content)
            return SimpleNamespace(outcome=AgentCompleted(summary="chapter reviewed"))

    snapshot = RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
    tool_context = ToolContext(
        project=project,
        services=SimpleNamespace(agent=WritingAgent()),  # type: ignore[arg-type]
        run_id="run-1",
        get_run_snapshot=lambda: snapshot,
    )
    result = await build_action_registry(tool_context=tool_context).get(
        "chapter.review"
    ).executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            action_id="review-1",
            snapshot=snapshot,
            target_lang="zh-Hans",
            repair_context=(
                "translation_quality_failed: replace half-width straight quotes",
            ),
        ),
        ReviewBatchInput(chapters=("001", "002")),
    )

    assert isinstance(result.outcome, Succeeded)
    assert len(result.outcome.artifact_bundle.entries) == 12
    final_entry = next(
        entry
        for entry in result.outcome.artifact_bundle.entries
        if entry.canonical_relpath == "chapters/final/001.md"
    )
    final_text = (project.root / final_entry.staged_relpath).read_text(encoding="utf-8")
    assert final_text == "所谓“现金交易”和‘永恒真理’。\n"
    assert [request.thread_id for request in requests] == [  # type: ignore[attr-defined]
        "run-1/review-1/1:001",
        "run-1/review-1/1:002",
    ]
    assert all(
        "replace half-width straight quotes" in request.system_prompt
        for request in requests  # type: ignore[attr-defined]
    )


@pytest.mark.asyncio
async def test_hitl_inspection_rebuilds_completed_bundle_from_exact_staging(
    tmp_path: Path,
) -> None:
    """A process restart must not discard already-durable approved tool output."""
    from abi.providers.agent_runtime import HitlCheckpointInspection

    project = BookProject(tmp_path)
    action_id = "finalize-1"
    manifest = expand_expected_artifacts("output.finalize", action_id, EmptyInput())
    expected = manifest.entries[0]
    store = ArtifactStore(project, None)
    try:
        store.writer(action_id, 1).write_text(
            expected.canonical_relpath,
            "# Final manifest\n",
            media_type=expected.media_type,
            evidence_role=expected.evidence_role,
            metadata=expected.metadata,
        )
    finally:
        store.close()

    class CompletedCheckpointAgent:
        async def inspect_hitl_checkpoint(self, request: object) -> HitlCheckpointInspection:
            return HitlCheckpointInspection(
                disposition="outcome",
                outcome=AgentCompleted(summary="approved output was written before restart"),
            )

    snapshot = RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
    tool_context = ToolContext(
        project=project,
        services=SimpleNamespace(agent=CompletedCheckpointAgent()),  # type: ignore[arg-type]
        run_id="run-1",
        get_run_snapshot=lambda: snapshot,
    )
    executor = build_action_registry(tool_context=tool_context).get(
        "output.finalize"
    ).executor

    inspection = await executor.inspect_hitl(  # type: ignore[attr-defined]
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            action_id=action_id,
            attempt=1,
            snapshot=snapshot,
        ),
        EmptyInput(),
        _approved_hitl_resume(),
    )

    assert inspection.disposition == "outcome"
    assert isinstance(inspection.outcome, Succeeded)
    assert inspection.outcome.artifact_bundle.entries == tuple(
        ArtifactBundleEntry(
            staged_relpath=f"state/staging/{action_id}/1/{item.canonical_relpath}",
            canonical_relpath=item.canonical_relpath,
            media_type=item.media_type,
            evidence_role=item.evidence_role,
            metadata=item.metadata,
        )
        for item in manifest.entries
    )


@pytest.mark.asyncio
async def test_hitl_resume_rebuilds_completed_bundle_from_exact_staging(
    tmp_path: Path,
) -> None:
    """A resumed graph uses durable staging, not a fresh writer's empty entry list."""
    project = BookProject(tmp_path)
    action_id = "finalize-1"
    manifest = expand_expected_artifacts("output.finalize", action_id, EmptyInput())
    expected = manifest.entries[0]
    store = ArtifactStore(project, None)
    try:
        store.writer(action_id, 1).write_text(
            expected.canonical_relpath,
            "# Final manifest\n",
            media_type=expected.media_type,
            evidence_role=expected.evidence_role,
            metadata=expected.metadata,
        )
    finally:
        store.close()

    class CompletedContinuationAgent:
        async def run_action(self, request: object) -> object:
            return SimpleNamespace(outcome=AgentCompleted(summary="approved output committed"))

    snapshot = RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
    tool_context = ToolContext(
        project=project,
        services=SimpleNamespace(agent=CompletedContinuationAgent()),  # type: ignore[arg-type]
        run_id="run-1",
        get_run_snapshot=lambda: snapshot,
    )
    executor = build_action_registry(tool_context=tool_context).get(
        "output.finalize"
    ).executor

    envelope = await executor.resume_hitl(  # type: ignore[attr-defined]
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            action_id=action_id,
            attempt=1,
            snapshot=snapshot,
        ),
        EmptyInput(),
        _approved_hitl_resume(),
    )

    assert isinstance(envelope.outcome, Succeeded)
    assert envelope.outcome.artifact_bundle.entries == tuple(
        ArtifactBundleEntry(
            staged_relpath=f"state/staging/{action_id}/1/{item.canonical_relpath}",
            canonical_relpath=item.canonical_relpath,
            media_type=item.media_type,
            evidence_role=item.evidence_role,
            metadata=item.metadata,
        )
        for item in manifest.entries
    )


def test_hitl_staging_rebuild_rejects_extra_files(tmp_path: Path) -> None:
    project = BookProject(tmp_path)
    action_id = "finalize-1"
    manifest = expand_expected_artifacts("output.finalize", action_id, EmptyInput())
    expected = manifest.entries[0]
    store = ArtifactStore(project, None)
    try:
        writer = store.writer(action_id, 1)
        writer.write_text(
            expected.canonical_relpath,
            "# Final manifest\n",
            media_type=expected.media_type,
            evidence_role=expected.evidence_role,
            metadata=expected.metadata,
        )
        writer.write_text(
            "output/unexpected.md",
            "unexpected",
            media_type="text/markdown",
            evidence_role="unexpected",
        )

        with pytest.raises(ArtifactConflictError, match="contains extras"):
            store.rebuild_exact_staged_bundle(action_id, 1, manifest)
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
@pytest.mark.parametrize(
    ("review_result", "expected_kind"),
    (
        ("result: PASS", "succeeded"),
        ("Final Verdict: PASS", "retryable_failure"),
    ),
)
async def test_independent_review_enforces_terminal_machine_result_protocol(
    tmp_path: Path, review_result: str, expected_kind: str
) -> None:
    project = BookProject(tmp_path)
    for skill in (
        "skills/expert-translation-quality/SKILL.md",
        "skills/translation-quality-defect-families/SKILL.md",
    ):
        path = project.root / skill
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Review policy\n", encoding="utf-8")

    def provider_success() -> AgentCompleted:
        return AgentCompleted(summary="review files written")

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
                content=f"# {reviewer}\n\n{review_result}\n",
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

    assert result.outcome.kind == expected_kind
    if expected_kind == "retryable_failure":
        assert result.outcome.error_code == "review_result_protocol_invalid"
        return
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

    def provider_success() -> AgentCompleted:
        return AgentCompleted(summary="spot-check files written")

    class CompositeAgent:
        async def run_action(self, request: object) -> object:
            tools = {tool.name: tool.callable for tool in request.tools}  # type: ignore[attr-defined]
            agent_name = request.agent_name  # type: ignore[attr-defined]
            if agent_name == "review_spotcheck":
                tools["select_random_review_passages"]()
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
                tools["validate_random_spotcheck"]()
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
    snapshot = RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
    registry = build_action_registry(
        tool_context=ToolContext(
            project=project,
            services=SimpleNamespace(),  # type: ignore[arg-type]
            run_id="run-1",
            get_run_snapshot=lambda: snapshot,
        )
    )
    definition = registry.get("source.ingest")
    result = await definition.executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            action_id="ingest-1",
            attempt=1,
            snapshot=snapshot,
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
    snapshot = RunSnapshot(run_id="run-1", status=RunStatus.RUNNING)
    registry = build_action_registry(
        tool_context=ToolContext(
            project=project,
            services=SimpleNamespace(),  # type: ignore[arg-type]
            run_id="run-1",
            get_run_snapshot=lambda: snapshot,
        )
    )
    result = await registry.get("source.split").executor(
        ActionExecutionContext(
            project=project,
            run_id="run-1",
            action_id="split-1",
            attempt=1,
            snapshot=snapshot,
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
async def test_epub_build_uses_a_shadow_build_and_stages_every_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from abi.epub.result import GateResult

    monkeypatch.setattr(
        "abi.epub.epubcheck.run_epubcheck_readonly",
        lambda path: GateResult(True, "EPUBCheck passed"),
    )
    project = BookProject(tmp_path)
    project.chapters_final.mkdir(parents=True)
    (project.chapters_final / "001.md").write_text("# Chapter\n\nText", encoding="utf-8")
    project.finalized_book_yaml.parent.mkdir(parents=True, exist_ok=True)
    project.finalized_book_yaml.write_text(
        "title: Fixture\nlanguage: en\n", encoding="utf-8"
    )
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


@pytest.mark.asyncio
async def test_epubcheck_unavailable_is_a_precise_permanent_execution_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from abi.epub.result import GateResult

    project = BookProject(tmp_path)
    project.chapters_final.mkdir(parents=True)
    (project.chapters_final / "001.md").write_text(
        "# Chapter\n\nText", encoding="utf-8"
    )
    project.finalized_book_yaml.parent.mkdir(parents=True, exist_ok=True)
    project.finalized_book_yaml.write_text(
        "title: Fixture\nlanguage: en\nidentifier: fixture\nrights: test\n"
        "publisher: test\nauthors: []\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "abi.epub.epubcheck.run_epubcheck_readonly",
        lambda path: GateResult(False, "EPUBCheck not available"),
    )

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

    assert isinstance(result.outcome, PermanentFailure)
    assert result.outcome.error_code == "epubcheck_unavailable"
