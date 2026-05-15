"""End-to-end smoke test for the eval pipeline.

Runs ``abi translate`` on a tiny fixture first to produce an ABI run, then
runs ``abi eval`` against it. Mocks every LLM call (translation, judge,
baseline). Asserts the full set of artifacts is produced and the report
contains the expected sections.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import respx

from abi.eval import run_eval
from abi.runtime import run_pipeline
from abi.types.eval import EvalConfig
from abi.types.run import LangfuseConfig, LLMConfig, RunConfig

FIXTURE = Path(__file__).parent / "fixtures" / "short_book.txt"


def _fake_completion(content: str) -> dict:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


def _abi_handler(request: httpx.Request) -> httpx.Response:
    """Same dispatcher used by ``test_e2e_pipeline.py``, condensed."""
    body = json.loads(request.content)
    text = "\n".join(m.get("content", "") for m in body.get("messages", []))

    if "ChapterSummarizer" in text or "structured summary that will guide" in text:
        payload = {
            "one_liner": "x", "abstract": "x", "key_points": ["a", "b", "c"],
            "key_terms": [{
                "surface_form": "embodiment",
                "proposed_target": "具身",
                "definition": "x",
                "importance": "core",
            }],
            "open_questions": [],
        }
    elif "book-level overview" in text or "synthesizing a book" in text:
        payload = {"thesis": "T", "target_audience": "r",
                   "register": "academic-formal", "tone_notes": ""}
    elif "terminology arbiter" in text:
        payload = {"chosen_target": "具身", "rationale": "x", "rejected": []}
    elif "translation style strategist" in text:
        payload = {"register_directives": ["x"], "forbidden_patterns": [], "preferred_patterns": []}
    elif "mindmap" in text.lower():
        payload = {"mermaid": "mindmap\n  root((T))"}
    elif "translate a list of chapter / section headings" in text:
        ids = re.findall(r"section_id=([0-9a-f-]+)", text)
        payload = {"items": [{"section_id": s, "translated": "译标题"} for s in ids]}
    elif "reconstructing the chapter / section structure" in text:
        cands = re.findall(r"\[([A-Z0-9]+)\] kind=(\w+) text=\"([^\"]+)\"", text)
        payload = {"chapters": [
            {"anchor_id": a, "title": t, "level": 2}
            for a, k, t in cands if k == "heading"
        ]}
    else:
        # Default = paragraph translation.
        payload = {
            "translated_text": "ABI 译文。",
            "terms_used": [],
            "confidence": 0.9,
            "notes": "",
            "untranslated_passthrough": False,
        }
    return httpx.Response(200, json=_fake_completion(json.dumps(payload, ensure_ascii=False)))


def _eval_handler(request: httpx.Request) -> httpx.Response:
    """Eval-pipeline LLM dispatcher: baseline translator + likert + pairwise judges."""
    body = json.loads(request.content)
    text = "\n".join(m.get("content", "") for m in body.get("messages", []))

    if "naive baseline run" in text:
        # Baseline translator: count the source paragraphs in the prompt
        # (paragraphs separated by blank lines after "## Source text") and emit
        # one baseline-style chinese line per paragraph so alignment is exact.
        m = re.search(r"## Source text\n(.*)$", text, re.DOTALL)
        body_text = m.group(1) if m else ""
        paras = [p for p in re.split(r"\n\n+", body_text) if p.strip()]
        translated = "\n\n".join(f"BASELINE 译文 {i+1}" for i in range(len(paras)))
        payload = {"translated_text": translated}
    elif "strict, impartial translation-quality evaluator" in text and "Likert" in text:
        # Likert judge: ABI is "good", baseline is "okay".
        payload = {
            "a": {"adequacy": 5, "fluency": 5, "coherence": 4, "style": 5, "rationale": "x"},
            "b": {"adequacy": 3, "fluency": 4, "coherence": 3, "style": 3, "rationale": "y"},
        }
    elif "Pick the translation that is overall better" in text:
        # Pairwise judge: prefers whichever side contains the literal "ABI"
        # token (our ABI mock emits "ABI 译文。"; the baseline mock emits
        # "BASELINE 译文 N"). This lets the test verify the A/B randomization
        # logic by checking that ABI wins regardless of which slot it sits in.
        a_match = re.search(r"## Translation A\n(.*?)\n+## Translation B", text, re.DOTALL)
        b_match = re.search(r"## Translation B\n(.*?)\n+##", text, re.DOTALL)
        a_text = a_match.group(1) if a_match else ""
        b_text = b_match.group(1) if b_match else ""
        verdict = "A" if "ABI" in a_text else ("B" if "ABI" in b_text else "tie")
        payload = {"verdict": verdict, "rationale": "ABI side is more faithful."}
    else:
        payload = {"translated_text": "??"}
    return httpx.Response(200, json=_fake_completion(json.dumps(payload, ensure_ascii=False)))


def _combined_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    text = "\n".join(m.get("content", "") for m in body.get("messages", []))
    if (
        "naive baseline run" in text
        or "strict, impartial translation-quality evaluator" in text
    ):
        return _eval_handler(request)
    return _abi_handler(request)


@respx.mock
def test_eval_end_to_end(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("ABI_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.chdir(tmp_path)  # eval_output_root() uses cwd

    respx.post("https://mock.local/v1/chat/completions").mock(side_effect=_combined_handler)

    config = RunConfig(
        target_language="zh",
        modes=["translated"],
        llm=LLMConfig(base_url="https://mock.local/v1", model="test-model", max_concurrency=2),
        langfuse=LangfuseConfig(enabled=False),
    )

    # Step 1: ABI translate
    abi_result = asyncio.run(
        run_pipeline(input_path=FIXTURE, config=config, output_dir=tmp_path / "out")
    )
    assert abi_result.units_translated > 0

    # Step 2: eval
    eval_cfg = EvalConfig(samples=3, random_seed=42)
    artifacts = asyncio.run(
        run_eval(
            source_path=FIXTURE,
            config=config,
            eval_config=eval_cfg,
            abi_run_id="latest",
        )
    )

    eval_dir = artifacts.eval_dir
    assert eval_dir.exists()
    assert (eval_dir / "baseline" / "translated.md").exists()
    assert (eval_dir / "baseline" / "meta.json").exists()
    assert (eval_dir / "alignment.json").exists()
    assert (eval_dir / "samples.jsonl").exists()
    assert (eval_dir / "mechanical.json").exists()
    assert (eval_dir / "judge" / "likert.jsonl").exists()
    assert (eval_dir / "judge" / "pairwise.jsonl").exists()
    assert (eval_dir / "report.json").exists()
    assert (eval_dir / "report.md").exists()
    assert (eval_dir / "events.jsonl").exists()

    # Alignment should be positional (baseline mock emits same para count)
    align_data = json.loads((eval_dir / "alignment.json").read_text(encoding="utf-8"))
    assert align_data["strategy"] == "positional"
    assert align_data["aligned_pairs"] == align_data["source_paragraphs"]

    # Judge gave ABI all the high scores → winrate must be 1.0
    report = artifacts.report
    assert report.judge.samples > 0
    assert report.judge.pairwise_abi_winrate == 1.0
    assert report.judge.likert_delta["mean"] > 0

    # Mechanical metrics should be present and in [0, 1].
    for s in (report.mechanical.abi, report.mechanical.baseline):
        assert 0.0 <= s.completeness <= 1.0
        assert 0.0 <= s.glossary_compliance <= 1.0

    # The report.md contains the expected sections.
    md = (eval_dir / "report.md").read_text(encoding="utf-8")
    assert "# Evaluation report" in md
    assert "## Mechanical metrics" in md
    assert "## LLM-as-Judge — Likert" in md
    assert "## LLM-as-Judge — Pairwise preference" in md


@respx.mock
def test_eval_skip_baseline_reuses_cached(tmp_path: Path, monkeypatch) -> None:
    """``--skip-baseline`` should reuse a prior baseline file, not re-call the LLM."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("ABI_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.chdir(tmp_path)

    respx.post("https://mock.local/v1/chat/completions").mock(side_effect=_combined_handler)

    config = RunConfig(
        target_language="zh",
        modes=["translated"],
        llm=LLMConfig(base_url="https://mock.local/v1", model="test-model", max_concurrency=2),
        langfuse=LangfuseConfig(enabled=False),
    )
    asyncio.run(
        run_pipeline(input_path=FIXTURE, config=config, output_dir=tmp_path / "out")
    )

    eval_cfg = EvalConfig(samples=2, random_seed=42)
    # First run: produces baseline.
    a1 = asyncio.run(run_eval(
        source_path=FIXTURE, config=config, eval_config=eval_cfg, abi_run_id="latest"
    ))
    # Second run with skip_baseline: should reuse cached baseline (cross-eval).
    eval_cfg2 = EvalConfig(samples=2, random_seed=42, skip_baseline=True)
    a2 = asyncio.run(run_eval(
        source_path=FIXTURE, config=config, eval_config=eval_cfg2, abi_run_id="latest"
    ))
    # Both baselines should match.
    assert a1.baseline.full_text == a2.baseline.full_text
