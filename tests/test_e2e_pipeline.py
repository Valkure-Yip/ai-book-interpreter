"""End-to-end smoke test using a mocked OpenAI-compatible endpoint.

This test runs the full Pass 0 → 1 → 2 → 3 pipeline against an in-memory
HTTP mock, asserting that artifacts are produced and the book IR is fully
covered by translation units.
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
    """Dispatch responses based on prompt content keywords."""
    body = json.loads(request.content)
    messages = body.get("messages", [])
    text = "\n".join(m.get("content", "") for m in messages)

    if "ChapterSummarizer" in text or "structured summary that will guide later" in text:
        payload = {
            "one_liner": "本章一句话",
            "abstract": "本章摘要内容。",
            "key_points": ["要点1", "要点2", "要点3"],
            "key_terms": [
                {
                    "surface_form": "embodiment",
                    "proposed_target": "具身",
                    "definition": "身体与认知的统一",
                    "importance": "core",
                }
            ],
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
        payload = {"chosen_target": "具身", "rationale": "domain accuracy", "rejected": []}
    elif "translation style strategist" in text:
        payload = {
            "register_directives": ["使用书面学术汉语"],
            "forbidden_patterns": [],
            "preferred_patterns": [],
        }
    elif "mindmap" in text.lower():
        payload = {"mermaid": "mindmap\n  root((Test))\n    Ch1\n    Ch2"}
    elif "translate a list of chapter / section headings" in text:
        # heading_translator: echo back a fake Chinese title per requested section_id
        import re

        ids = re.findall(r"section_id=([0-9a-f-]+)", text)
        payload = {
            "items": [
                {"section_id": sid, "translated": f"译标题-{sid[:6]}"}
                for sid in ids
            ]
        }
    elif "reconstructing the chapter / section structure" in text:
        # toc_detector: pick every "heading" kind candidate. This keeps the
        # heuristic structure intact while still exercising the LLM path.
        import re

        # Each candidate line looks like:
        # [C0001] kind=heading text="Chapter X"
        #   next: "..."
        cands = re.findall(
            r"\[([A-Z0-9]+)\] kind=(\w+) text=\"([^\"]+)\"", text
        )
        items = []
        for anchor, kind, title in cands:
            if kind == "heading":
                items.append({"anchor_id": anchor, "title": title, "level": 2})
        payload = {"chapters": items}
    elif "Translate this paragraph" in text or "professional academic translator" in text:
        # paragraph translator
        payload = {
            "translated_text": "这是译文。",
            "terms_used": [
                {"term": "embodiment", "rendered_as": "具身", "compliant": True}
            ],
            "confidence": 0.9,
            "notes": "",
            "untranslated_passthrough": False,
        }
    else:
        # default fallback: pretend it's a translation
        payload = {
            "translated_text": "默认译文",
            "terms_used": [],
            "confidence": 0.8,
            "notes": "",
            "untranslated_passthrough": False,
        }

    return httpx.Response(200, json=_fake_completion(json.dumps(payload, ensure_ascii=False)))


@respx.mock
def test_full_pipeline_end_to_end(tmp_path: Path, monkeypatch) -> None:
    # Set up fake key for the LLM router.
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    # Disable Langfuse (no keys).
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    # Route runs into tmp.
    monkeypatch.setenv("ABI_RUNS_DIR", str(tmp_path / "runs"))

    # Mock the OpenAI-compatible endpoint.
    respx.post("https://mock.local/v1/chat/completions").mock(side_effect=_handle_request)

    config = RunConfig(
        target_language="zh",
        modes=["translated", "bilingual", "annotated"],
        llm=LLMConfig(
            base_url="https://mock.local/v1",
            model="test-model",
            max_concurrency=2,
        ),
        langfuse=LangfuseConfig(enabled=False),
    )

    out = tmp_path / "out"
    result = asyncio.run(
        run_pipeline(input_path=FIXTURE, config=config, output_dir=out)
    )

    # Assert run artifacts
    assert result.run_dir.exists()
    assert (result.run_dir / "ir" / "book.json").exists()
    assert (result.run_dir / "survey" / "overview.json").exists()
    assert (result.run_dir / "survey" / "glossary.json").exists()
    assert (result.run_dir / "survey" / "mindmap.mmd").exists()
    assert (result.run_dir / "events.jsonl").exists()
    assert (result.run_dir / "metrics.json").exists()

    # Assert output files
    assert (out / "translated.md").exists()
    assert (out / "bilingual.md").exists()
    assert (out / "annotated.md").exists()
    assert (out / "report.md").exists()

    # The fixture has 8 prose paragraphs (rough), check at least 5 translated
    assert result.units_translated >= 5

    # All paragraph translations have unique IDs
    paragraphs_dir = result.run_dir / "translate" / "paragraphs"
    files = list(paragraphs_dir.glob("*.json"))
    assert files
    ids = [f.stem for f in files]
    assert len(set(ids)) == len(ids)

    # The translated.md actually contains our mock translation
    content = (out / "translated.md").read_text(encoding="utf-8")
    assert "这是译文" in content or "默认译文" in content

    # report contains cost section
    report = (out / "report.md").read_text(encoding="utf-8")
    assert "翻译报告" in report
    assert "Confidence" in report


@respx.mock
def test_toc_refiner_repairs_misclassified_chapters(
    tmp_path: Path, monkeypatch
) -> None:
    """A file where heuristic ingest dumps everything into "Front Matter" should
    be repaired by the refiner: top-level chapters appear in the final TOC.
    """
    import re

    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("ABI_RUNS_DIR", str(tmp_path / "runs"))

    # Build a minimal "Manifesto-shaped" fixture: Roman-numeral two-line
    # chapter headings. The heuristic parser joins each pair into a single
    # prose paragraph instead of recognizing them as headings, putting all
    # content into a synthetic "Front Matter" wrapper.
    fixture = tmp_path / "manifesto_like.txt"
    fixture.write_text(
        "Preamble paragraph about the manifesto.\n"
        "\n"
        "I.\n"
        "BOURGEOIS AND PROLETARIANS\n"
        "\n"
        "The history of all hitherto existing societies is the history "
        "of class struggles.\n"
        "\n"
        "Freeman and slave, patrician and plebeian, lord and serf.\n"
        "\n"
        "II.\n"
        "PROLETARIANS AND COMMUNISTS\n"
        "\n"
        "In what relation do the Communists stand to the proletarians?\n"
        "\n"
        "The Communists do not form a separate party opposed to other "
        "working-class parties.\n",
        encoding="utf-8",
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        text = "\n".join(m.get("content", "") for m in body.get("messages", []))

        if "reconstructing the chapter / section structure" in text:
            # Pick the two Roman-numeral paragraph anchors.
            cands = re.findall(
                r"\[([A-Z0-9]+)\] kind=(\w+) text=\"([^\"]+)\"", text
            )
            items = []
            for anchor, _kind, t in cands:
                if t.startswith("I.") and "BOURGEOIS" in t:
                    items.append(
                        {"anchor_id": anchor, "title": "I. Bourgeois and Proletarians", "level": 1}
                    )
                elif t.startswith("II.") and "PROLETARIANS" in t:
                    items.append(
                        {"anchor_id": anchor, "title": "II. Proletarians and Communists", "level": 1}
                    )
            return httpx.Response(
                200,
                json=_fake_completion(json.dumps({"chapters": items}, ensure_ascii=False)),
            )

        # Reuse the shared dispatcher for everything else (survey, translation,
        # heading_translator, etc.).
        return _handle_request(request)

    respx.post("https://mock.local/v1/chat/completions").mock(side_effect=_handler)

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
    result = asyncio.run(run_pipeline(input_path=fixture, config=config, output_dir=out))

    # The refined book.json on disk must reflect the LLM's TOC, not the
    # heuristic "Front Matter" wrapper.
    from abi.types.book import Book

    refined = Book.model_validate_json(
        (result.run_dir / "ir" / "book.json").read_text(encoding="utf-8")
    )
    headings = [s.heading for s in refined.toc]
    assert "I. Bourgeois and Proletarians" in headings
    assert "II. Proletarians and Communists" in headings

    # Metadata artifact records that the refiner ran via LLM.
    refinement_path = result.run_dir / "ir" / "toc-refinement.json"
    assert refinement_path.exists()
    meta = json.loads(refinement_path.read_text(encoding="utf-8"))
    assert meta["method"] == "llm"
    assert meta["detected"] == 2


@respx.mock
def test_no_refine_toc_skips_refiner(tmp_path: Path, monkeypatch) -> None:
    """``refine_toc=False`` must skip the LLM call and keep heuristic TOC."""
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
        refine_toc=False,
        llm=LLMConfig(
            base_url="https://mock.local/v1",
            model="test-model",
            max_concurrency=2,
        ),
        langfuse=LangfuseConfig(enabled=False),
    )

    result = asyncio.run(
        run_pipeline(input_path=FIXTURE, config=config, output_dir=tmp_path / "out")
    )

    # No toc_detector LLM call should have been issued.
    for call in route.calls:
        body = json.loads(call.request.content)
        msgs = "\n".join(m.get("content", "") for m in body.get("messages", []))
        assert "reconstructing the chapter / section structure" not in msgs, (
            "toc_detector must not be invoked when refine_toc=False"
        )

    # And the toc-refinement.json artifact must NOT be written.
    assert not (result.run_dir / "ir" / "toc-refinement.json").exists()

    # The events log records the explicit skip reason.
    events_text = (result.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "disabled_by_config" in events_text


@respx.mock
def test_resume_reuses_run_dir_and_survey(tmp_path: Path, monkeypatch) -> None:
    """Second invocation with --resume reuses the same run_dir + survey artifacts."""
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("ABI_RUNS_DIR", str(tmp_path / "runs"))
    respx.post("https://mock.local/v1/chat/completions").mock(side_effect=_handle_request)

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
    first = asyncio.run(run_pipeline(input_path=FIXTURE, config=config, output_dir=out))
    first_run_id = first.run_dir.name

    # Snapshot count of mocked LLM requests after first run.
    calls_after_first = respx.calls.call_count

    # Now re-run with resume=latest. It must reuse the same run_dir, NOT spawn a new
    # one, and must short-circuit Pass 1 (no new chapter_summarized events).
    second = asyncio.run(
        run_pipeline(
            input_path=FIXTURE,
            config=config,
            output_dir=out,
            resume="latest",
        )
    )
    assert second.run_dir == first.run_dir, "resume must reuse the existing run dir"
    assert second.run_dir.name == first_run_id

    # Per-paragraph checkpoints from first run are reused → second run makes
    # strictly fewer LLM calls. (Survey is fully cached; paragraphs are cached.)
    calls_after_second = respx.calls.call_count
    new_calls = calls_after_second - calls_after_first
    # At most a handful of revision/retry calls should happen; survey is cached.
    assert new_calls <= 1, f"resume should not re-run survey, but made {new_calls} new calls"

    # Survey resumed event must be present.
    events_text = (second.run_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "survey.resumed_from_disk" in events_text

    # And outputs still produced.
    assert (out / "translated.md").exists()
