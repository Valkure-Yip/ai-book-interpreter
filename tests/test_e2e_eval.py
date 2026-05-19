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


def _three_way_slot_text(text: str, slot: str) -> str:
    """Extract the text of slot A, B, or C from a 3-way judge prompt."""
    if slot == "A":
        m = re.search(r"## Translation A\n(.*?)\n+## Translation B", text, re.DOTALL)
    elif slot == "B":
        m = re.search(r"## Translation B\n(.*?)\n+## Translation C", text, re.DOTALL)
    else:
        m = re.search(r"## Translation C\n(.*?)\n+##", text, re.DOTALL)
    return m.group(1) if m else ""


def _which_slot_has(text: str, marker: str) -> str | None:
    for slot in ("A", "B", "C"):
        if marker in _three_way_slot_text(text, slot):
            return slot
    return None


def _classify_slots(text: str) -> dict[str, str]:
    """Return ``{"A": "abi"|"baseline"|"reference", ...}`` for a 3-way prompt.

    Heuristic: the slot with ``ABI 译文`` is ABI; the slot with
    ``BASELINE 译文`` is baseline; the leftover slot is reference.
    """
    kinds: dict[str, str] = {}
    for slot in ("A", "B", "C"):
        s = _three_way_slot_text(text, slot)
        if "ABI 译文" in s:
            kinds[slot] = "abi"
        elif "BASELINE 译文" in s:
            kinds[slot] = "baseline"
        else:
            kinds[slot] = "reference"
    return kinds


def _eval_handler(request: httpx.Request) -> httpx.Response:
    """Eval-pipeline LLM dispatcher: baseline translator + likert + pairwise judges.

    Handles both the 2-way (legacy) and 3-way (reference-present) paths.
    """
    body = json.loads(request.content)
    text = "\n".join(m.get("content", "") for m in body.get("messages", []))

    if "naive baseline run" in text:
        m = re.search(r"## Source text\n(.*)$", text, re.DOTALL)
        body_text = m.group(1) if m else ""
        paras = [p for p in re.split(r"\n\n+", body_text) if p.strip()]
        translated = "\n\n".join(f"BASELINE 译文 {i+1}" for i in range(len(paras)))
        payload = {"translated_text": translated}
    elif "score\nTHREE independent translations" in text:
        # 3-way Likert: ABI = 5, reference = 5, baseline = 3.
        # Slots: ABI contains "ABI 译文"; baseline contains "BASELINE 译文";
        # reference is the remaining one.
        kinds = _classify_slots(text)
        scores: dict[str, dict] = {}
        for slot in ("a", "b", "c"):
            kind = kinds[slot.upper()]
            if kind in {"abi", "reference"}:
                scores[slot] = {"adequacy": 5, "fluency": 5, "coherence": 5, "style": 5, "rationale": "x"}
            else:
                scores[slot] = {"adequacy": 3, "fluency": 3, "coherence": 3, "style": 3, "rationale": "y"}
        payload = scores
    elif "strict, impartial translation-quality evaluator" in text and "Likert" in text:
        # 2-way Likert: ABI is "good", baseline is "okay".
        payload = {
            "a": {"adequacy": 5, "fluency": 5, "coherence": 4, "style": 5, "rationale": "x"},
            "b": {"adequacy": 3, "fluency": 4, "coherence": 3, "style": 3, "rationale": "y"},
        }
    elif "three pairwise verdicts" in text or "Issue three independent" in text:
        # 3-way pairwise: ABI and reference are tied at the top; baseline loses.
        kinds = _classify_slots(text)
        ranks = {"abi": 2, "reference": 2, "baseline": 1}

        def winner(left: str, right: str) -> str:
            lr, rr = ranks[kinds[left]], ranks[kinds[right]]
            if lr == rr:
                return "tie"
            return left if lr > rr else right

        payload = {
            "a_vs_b": winner("A", "B"),
            "a_vs_c": winner("A", "C"),
            "b_vs_c": winner("B", "C"),
            "rationale": "ABI and reference are strong; baseline is weaker.",
        }
    elif "Pick the translation that is overall better" in text:
        # 2-way pairwise: prefer whichever side contains "ABI".
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
def test_eval_three_way_with_dataset(tmp_path: Path, monkeypatch) -> None:
    """End-to-end eval using ``--dataset stub`` (the wmt24pp stub).

    Exercises:
      - dataset spec → materialize → ingest → translate (auto-translate)
      - 3-way alignment (references attached positionally)
      - 3-way Likert + pairwise judge
      - mechanical metrics for the reference column
      - report.md renders the 3-way sections
      - Langfuse experiment auto-disables when keys are missing
    """
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

    from abi.eval import run_eval
    from abi.eval.datasets import load_eval_dataset
    from abi.types.eval import EvalConfig

    # Sanity: stub dataset reachable without HF Hub.
    ds = load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true")
    assert len(ds.paragraphs) == 3

    # Tweak the ABI handler so its translated_text contains "ABI 译文" — the
    # judge mock keys off that token to recognise the ABI slot. The default
    # already produces "ABI 译文。" so no override needed.

    eval_cfg = EvalConfig(
        samples=3,
        random_seed=42,
        dataset_spec="wmt24pp:en-zh_CN:literary:stub=true",
        auto_translate=True,
        langfuse_experiment=False,  # explicitly off — no keys anyway
    )
    artifacts = asyncio.run(
        run_eval(
            source_path=None,
            config=config,
            eval_config=eval_cfg,
            abi_run_id=None,
        )
    )

    report = artifacts.report
    # Reference column populated.
    assert report.reference_paragraphs == 3
    assert report.mechanical.reference is not None
    # Dataset spec stored for provenance.
    assert report.dataset_spec == "wmt24pp:en-zh_CN:literary:stub=true"
    # Three-way fields are present in JudgeAggregate.
    assert report.judge.likert_reference != {}
    # No Langfuse run when keys are missing.
    assert report.langfuse_dataset_run_url is None

    # Per-sample structure: likert_reference is filled when has_reference.
    assert all(r.likert_reference is not None for r in artifacts.judge_results)

    # report.md must include the 3-way sections.
    md = (artifacts.eval_dir / "report.md").read_text(encoding="utf-8")
    assert "## LLM-as-Judge — Likert" in md
    assert "Reference" in md
    assert "ABI vs Reference" in md
    assert "Baseline vs Reference" in md


@respx.mock
def test_eval_three_way_with_langfuse_experiment(tmp_path: Path, monkeypatch) -> None:
    """3-way eval + mocked Langfuse experiment client.

    Verifies that ensure_dataset / link / attach_scores / finalize_run all
    run end-to-end, that per-sample + run-level scores get pushed, and that
    the report records the Langfuse run URL.
    """
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("ABI_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.chdir(tmp_path)

    respx.post("https://mock.local/v1/chat/completions").mock(side_effect=_combined_handler)

    # Install a fake Langfuse client so we don't hit cloud.
    from tests.test_langfuse_experiment import FakeLangfuseClient
    fake = FakeLangfuseClient()
    monkeypatch.setattr(
        "abi.eval.pipeline.get_langfuse_client",
        lambda _config: fake,
    )

    config = RunConfig(
        target_language="zh",
        modes=["translated"],
        llm=LLMConfig(base_url="https://mock.local/v1", model="test-model", max_concurrency=2),
        langfuse=LangfuseConfig(enabled=True, host="https://langfuse.local"),
    )

    from abi.eval import run_eval
    from abi.types.eval import EvalConfig

    eval_cfg = EvalConfig(
        samples=3,
        random_seed=42,
        dataset_spec="wmt24pp:en-zh_CN:literary:stub=true",
        auto_translate=True,
        langfuse_experiment=True,
    )
    artifacts = asyncio.run(
        run_eval(
            source_path=None,
            config=config,
            eval_config=eval_cfg,
            abi_run_id=None,
        )
    )

    report = artifacts.report
    # Langfuse fields populated.
    assert report.langfuse_dataset_name == "wmt24pp-en-zh_CN-literary-v1"
    assert report.langfuse_dataset_run_id == report.eval_id
    assert report.langfuse_dataset_run_url is not None
    assert "wmt24pp-en-zh_CN-literary-v1" in report.langfuse_dataset_run_url

    # Dataset upsert happened once.
    assert len(fake.datasets_created) == 1
    # One Langfuse item per dataset paragraph (3 in the stub).
    assert len(fake.items_created) == 3
    # Per-sample scores pushed: likert.abi.mean + others, for 3 samples.
    sample_score_names = {s["name"] for s in fake.scores}
    assert "likert.abi.mean" in sample_score_names
    assert "likert.reference.mean" in sample_score_names
    assert "pairwise.abi_vs_baseline" in sample_score_names
    # Run-level aggregate scores pushed via the summary trace.
    assert "abi.winrate_vs_baseline" in sample_score_names
    assert "abi.winrate_vs_reference" in sample_score_names
    # A summary trace exists.
    assert len(fake.traces) == 1
    assert fake.traces[0].name == "abi.eval.run_summary"


@respx.mock
def test_eval_judge_model_override_from_env(tmp_path: Path, monkeypatch) -> None:
    """``EVAL_JUDGE_MODEL`` must flow through to the judge LLM calls."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "judge-only-model")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("ABI_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.chdir(tmp_path)

    requests_seen: list[dict] = []

    def _record(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests_seen.append(body)
        return _combined_handler(request)

    respx.post("https://mock.local/v1/chat/completions").mock(side_effect=_record)

    config = RunConfig(
        target_language="zh",
        modes=["translated"],
        llm=LLMConfig(base_url="https://mock.local/v1", model="translator-model", max_concurrency=2),
        langfuse=LangfuseConfig(enabled=False),
    )

    from abi.eval import run_eval
    from abi.runtime import run_pipeline
    from abi.types.eval import EvalConfig

    asyncio.run(run_pipeline(input_path=FIXTURE, config=config, output_dir=tmp_path / "out"))

    eval_cfg = EvalConfig(samples=2, random_seed=42)
    artifacts = asyncio.run(run_eval(
        source_path=FIXTURE, config=config, eval_config=eval_cfg, abi_run_id="latest"
    ))

    # Distinct models actually hit the wire.
    models_used = {req.get("model") for req in requests_seen}
    assert "translator-model" in models_used
    assert "judge-only-model" in models_used
    # Report records both.
    assert artifacts.report.translate_model == "translator-model"
    assert artifacts.report.judge_model == "judge-only-model"


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
