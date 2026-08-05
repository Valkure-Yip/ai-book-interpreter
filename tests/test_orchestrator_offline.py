"""End-to-end orchestrator run with a stubbed agent (no network / no LLM).

The stub inspects which stage is running and writes exactly the artifacts the
deterministic validators require, exercising the full state machine, gates, and
driver wiring from INIT to DONE.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from abi.orchestrator.driver import Orchestrator
from abi.project import ScaffoldRequest, Status, scaffold_project
from abi.providers.agent_runtime import AgentActionRequest
from abi.tools.context import ToolContext
from abi.types.orchestration import (
    AgentRunResult,
    ArtifactBundle,
    ArtifactBundleEntry,
    Succeeded,
)


class _FakeServices:
    """Minimal stand-in for RunServices and its Action runtime."""

    def __init__(self, project, ingest_split, gates) -> None:
        self.agent = _FakeAgent(project, ingest_split, gates)
        self.events = _NullEvents()
        self.metrics = _NullMetrics()

    def flush(self) -> None:
        pass


class _NullEvents:
    def event(self, *a, **k):
        pass


class _NullMetrics:
    def flush(self):
        pass

    def record_llm_call(self, **k):
        pass


class _FakeAgent:
    def __init__(self, project, ingest_split, gates) -> None:
        self._p = project
        self._ingest_split = ingest_split
        self._gates = gates

    async def run_action(self, request: AgentActionRequest) -> AgentRunResult:
        self._ingest_split(request.agent_name, self._p)
        self._gates(request.agent_name, self._p)
        return AgentRunResult(
            outcome=Succeeded(
                artifact_bundle=ArtifactBundle(
                    action_id="offline",
                    attempt=1,
                    entries=(
                        ArtifactBundleEntry(
                            staged_relpath="state/staging/offline/1/result.json",
                            canonical_relpath="result.json",
                            media_type="application/json",
                            evidence_role="offline_fixture",
                        ),
                    ),
                )
            ),
            tool_calls=0,
            llm_calls=0,
            cost_usd=0.0,
            stopped_reason="completed",
        )


def _make_stage_writer(project):
    """Return a function that writes the artifacts each stage needs to pass."""

    def write(agent_name: str, p) -> None:
        sid = agent_name
        if sid.startswith("01"):
            p.source_clean.write_text("clean text", encoding="utf-8")
            p.source_manifest.write_text(json.dumps({"format": "txt"}), encoding="utf-8")
        elif sid.startswith("02"):
            p.toc_json.write_text("[]", encoding="utf-8")
            for i in (1, 2):
                (p.chapters_src / f"{i:03d}_c.md").write_text(
                    f"# C{i}\n\nsrc {i}\n", encoding="utf-8"
                )
        elif sid.startswith("03"):
            (p.root / "qa/benchmark").mkdir(parents=True, exist_ok=True)
            (p.root / "qa/benchmark/global_research_ack.md").write_text("ack", encoding="utf-8")
        elif sid.startswith("04"):
            p.book_research.write_text("research", encoding="utf-8")
            p.style_profile.write_text("style", encoding="utf-8")
        elif sid.startswith("05"):
            p.pretranslation_report.write_text("result: PASS\n", encoding="utf-8")
        elif sid.startswith("06"):
            p.terms_csv.write_text("term,target,status\nfoo,甲,locked\n", encoding="utf-8")
            p.style_guide.write_text("rules", encoding="utf-8")
        elif sid.startswith("07"):
            for f in p.chapters_src.glob("*.md"):
                (p.chapters_translated / f.name).write_text("# 章\n\n译文\n", encoding="utf-8")
        elif sid.startswith("08a"):
            for f in p.chapters_translated.glob("*.md"):
                p.chapter_control(f.stem).write_text(
                    "scope: FULL_CHAPTER\nissues_found: 0\nfixes_applied: 0\n"
                    "unresolved_blocking_issues: 0\nlatest_round_status: PASS\n"
                    "allow_next_chapter: true\n",
                    encoding="utf-8",
                )
        elif sid.startswith("11"):
            for f in p.chapters_translated.glob("*.md"):
                p.chapter_gate(f.stem).write_text("result: PASS\n", encoding="utf-8")
                (p.chapters_final / f.name).write_text(
                    f.read_text(encoding="utf-8"), encoding="utf-8"
                )
        elif sid.startswith("13"):
            p.book_yaml.write_text(
                "title: 书\nlanguage: zh-Hans\nidentifier: ''\n", encoding="utf-8"
            )
            p.production_spec.write_text("spec", encoding="utf-8")
        elif sid.startswith("14"):
            from abi.epub.build import build_sample_epub

            build_sample_epub(p)
            p.sample_review.write_text("sample_review_status: PASS\n", encoding="utf-8")
        elif sid.startswith("15"):
            from abi.epub import asset_manifest_check, build_epub, publication_lint

            publication_lint(p)
            asset_manifest_check(p)
            build_epub(p)
        elif sid.startswith("16a"):
            from abi.qa import select_random_review_passages, validate_random_spotcheck

            for _ in range(2):
                select_random_review_passages(p, agents=2)
                rd = sorted(p.random_spotcheck_dir.glob("round_*"))[-1]
                for lbl in ("agent_a", "agent_b"):
                    (rd / "reviews" / f"{lbl}_summary.json").write_text(
                        json.dumps(
                            {
                                "average_score": 95,
                                "lowest_score": 91,
                                "open_p0_p1_p2": 0,
                                "confidence": 0.9,
                                "samples": [{"unit_id": "x", "score": 95}],
                            }
                        ),
                        encoding="utf-8",
                    )
                validate_random_spotcheck(p)
        elif sid.startswith("16_"):
            for lbl in ("agent_a", "agent_b"):
                (p.root / f"reviews/{lbl}").mkdir(parents=True, exist_ok=True)
                (p.root / f"reviews/{lbl}/review.md").write_text("result: PASS\n", encoding="utf-8")
        elif sid.startswith("18a"):
            from abi.release import create_release

            create_release(p)
        elif sid.startswith("18_"):
            p.final_manifest.write_text("manifest", encoding="utf-8")
        elif sid.startswith("19"):
            p.retrospective.write_text("retro", encoding="utf-8")
            (p.root / "retrospective/template_update_suggestions.md").write_text(
                "s", encoding="utf-8"
            )

    return write


@pytest.mark.asyncio
async def test_full_pipeline_offline(tmp_path: Path) -> None:
    project = scaffold_project(
        ScaffoldRequest(
            target_root=tmp_path / "zh-Hans",
            book_slug="t",
            source_lang="en",
            target_lang="zh-Hans",
            source_target="en-zh-Hans",
        )
    )
    writer = _make_stage_writer(project)
    services = _FakeServices(project, writer, lambda *_: None)
    ctx = ToolContext(project=project, services=services)  # type: ignore[arg-type]

    orch = Orchestrator(ctx, max_stage_attempts=1)
    result = await orch.run()

    assert result.final_status == Status.DONE, result.blocked_reason
    assert project.load_state().status == Status.DONE
    assert project.book_epub.exists()
    assert any(project.release_dir.glob("*.epub"))
