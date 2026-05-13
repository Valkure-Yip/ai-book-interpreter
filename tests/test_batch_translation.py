"""End-to-end check that ``ABI_BATCH_SIZE > 1`` actually batches LLM calls.

We re-use the e2e mock pattern: every chat completion records the prompts it
saw. With ``batch_size=3`` we expect strictly fewer paragraph-related LLM
requests than the paragraph count, AND we expect the batch prompt template's
signature ('Paragraphs to translate (THIS BATCH)') to appear in at least one
request body.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import respx

from abi.runtime import run_pipeline
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


def _handle_request(request: httpx.Request) -> httpx.Response:
    import re

    body = json.loads(request.content)
    text = "\n".join(m.get("content", "") for m in body.get("messages", []))

    if "ChapterSummarizer" in text or "structured summary that will guide later" in text:
        payload = {
            "one_liner": "本章一句话",
            "abstract": "本章摘要内容。",
            "key_points": ["要点1", "要点2", "要点3"],
            "key_terms": [],
            "open_questions": [],
        }
    elif "book-level overview" in text or "synthesizing a book" in text:
        payload = {
            "thesis": "全书论点",
            "target_audience": "researchers",
            "register": "academic-formal",
            "tone_notes": "",
        }
    elif "terminology arbiter" in text:
        payload = {"chosen_target": "X", "rationale": "ok", "rejected": []}
    elif "translation style strategist" in text:
        payload = {
            "register_directives": ["书面"],
            "forbidden_patterns": [],
            "preferred_patterns": [],
        }
    elif "mindmap" in text.lower():
        payload = {"mermaid": "mindmap\n  root((Test))"}
    elif "translate a list of chapter / section headings" in text:
        ids = re.findall(r"section_id=([0-9a-f-]+)", text)
        payload = {
            "items": [
                {"section_id": sid, "translated": f"译-{sid[:6]}"} for sid in ids
            ]
        }
    elif "reconstructing the chapter / section structure" in text:
        # Echo back the heading-kind candidates so refinement is a no-op rewrite.
        cands = re.findall(
            r"\[([A-Z0-9]+)\] kind=(\w+) text=\"([^\"]+)\"", text
        )
        payload = {
            "chapters": [
                {"anchor_id": a, "title": t, "level": 2}
                for a, k, t in cands
                if k == "heading"
            ]
        }
    elif "Paragraphs to translate (THIS BATCH)" in text:
        # Batch path: extract paragraph_ids and answer each.
        ids = re.findall(r"paragraph_id: ([0-9a-f-]+)", text)
        payload = {
            "items": [
                {
                    "paragraph_id": pid,
                    "translated_text": f"批量译文 for {pid[:6]}",
                    "terms_used": [],
                    "confidence": 0.9,
                    "notes": "",
                    "untranslated_passthrough": False,
                }
                for pid in ids
            ]
        }
    elif "Translate this paragraph" in text or "professional academic translator" in text:
        payload = {
            "translated_text": "单段译文。",
            "terms_used": [],
            "confidence": 0.9,
            "notes": "",
            "untranslated_passthrough": False,
        }
    else:
        payload = {
            "translated_text": "默认译文",
            "terms_used": [],
            "confidence": 0.8,
            "notes": "",
            "untranslated_passthrough": False,
        }

    return httpx.Response(200, json=_fake_completion(json.dumps(payload, ensure_ascii=False)))


@respx.mock
def test_batch_size_reduces_llm_calls_for_paragraphs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("ABI_RUNS_DIR", str(tmp_path / "runs"))
    route = respx.post("https://mock.local/v1/chat/completions").mock(
        side_effect=_handle_request
    )

    config = RunConfig(
        target_language="zh",
        modes=["translated"],
        batch_size=3,
        llm=LLMConfig(
            base_url="https://mock.local/v1",
            model="test-model",
            max_concurrency=2,
        ),
        langfuse=LangfuseConfig(enabled=False),
    )

    out = tmp_path / "out"
    result = asyncio.run(run_pipeline(input_path=FIXTURE, config=config, output_dir=out))

    # Inspect every request body — count batch-template hits.
    batch_calls = 0
    single_para_calls = 0
    for call in route.calls:
        body = json.loads(call.request.content)
        text = "\n".join(m.get("content", "") for m in body.get("messages", []))
        if "Paragraphs to translate (THIS BATCH)" in text:
            batch_calls += 1
        elif "Translate this paragraph" in text or "translate **one paragraph**" in text:
            single_para_calls += 1

    assert batch_calls >= 1, "expected at least one batched LLM call when batch_size=3"
    # With batch_size=3 across multiple paragraphs, total paragraph-related
    # calls must be < paragraph count.
    total_para_calls = batch_calls + single_para_calls
    assert total_para_calls < result.units_translated, (
        f"batching did not amortize: {total_para_calls} calls for "
        f"{result.units_translated} paragraphs"
    )

    # Each batch.translated event must show up in the events log.
    events_text = (result.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "batch.translated" in events_text


@respx.mock
def test_batch_size_one_uses_single_path(tmp_path: Path, monkeypatch) -> None:
    """Default (batch_size=1) must NOT invoke the batch template."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("ABI_RUNS_DIR", str(tmp_path / "runs"))
    route = respx.post("https://mock.local/v1/chat/completions").mock(
        side_effect=_handle_request
    )

    config = RunConfig(
        target_language="zh",
        modes=["translated"],
        llm=LLMConfig(
            base_url="https://mock.local/v1",
            model="test-model",
            max_concurrency=2,
        ),
        langfuse=LangfuseConfig(enabled=False),
    )

    out = tmp_path / "out"
    asyncio.run(run_pipeline(input_path=FIXTURE, config=config, output_dir=out))

    for call in route.calls:
        body = json.loads(call.request.content)
        text = "\n".join(m.get("content", "") for m in body.get("messages", []))
        assert "Paragraphs to translate (THIS BATCH)" not in text, (
            "batch template must not be used when batch_size=1"
        )
