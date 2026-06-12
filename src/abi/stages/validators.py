"""Deterministic stage-completion validators.

After an agent claims a stage is done, the orchestrator calls the matching
validator: a stage only advances if the *filesystem* proves the exit condition.
This is the in-process equivalent of PDBT's ``check_template_workflow_gate.py``
and ``preflight:template`` hard gates — the agent cannot self-declare PASS.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from abi.project.layout import BookProject
from abi.project.state import Status


@dataclass(frozen=True)
class GateCheck:
    ok: bool
    reason: str


def _ok() -> GateCheck:
    return GateCheck(True, "ok")


def _fail(reason: str) -> GateCheck:
    return GateCheck(False, reason)


_ZERO_ISSUE_FIELDS = {
    "scope": "FULL_CHAPTER",
    "issues_found": "0",
    "fixes_applied": "0",
    "unresolved_blocking_issues": "0",
    "latest_round_status": "PASS",
    "allow_next_chapter": "true",
}


def _norm_line(line: str) -> str:
    """Strip markdown emphasis (``*``/`` ` ``) and leading list/heading markers
    so a ``key: value`` field is recognised even when the LLM writes it as
    ``**result: PASS**`` or ``- result: PASS``. Underscores are preserved because
    field names (e.g. ``issues_found``) contain them."""
    line = re.sub(r"[*`]", "", line)
    line = re.sub(r"^\s*[#>\-]+\s*", "", line)
    return line


def _field_values(text: str, field: str) -> list[str]:
    """All values for ``field:`` across lines, markdown-tolerant, in order."""
    pat = re.compile(rf"^\s*{field}\s*:\s*(.+?)\s*$", flags=re.IGNORECASE)
    out: list[str] = []
    for raw in text.splitlines():
        m = pat.match(_norm_line(raw))
        if m:
            out.append(m.group(1).strip().strip('"').strip())
    return out


def _has_zero_issue_pass(text: str) -> bool:
    """True if the LAST occurrence of each control field matches a zero-issue PASS."""
    for field, expected in _ZERO_ISSUE_FIELDS.items():
        matches = _field_values(text, field)
        if not matches or matches[-1].lower() != expected.lower():
            return False
    return True


def _contains_pass(text: str, *, key: str = "result") -> bool:
    m = _field_values(text, key)
    return bool(m) and m[-1].upper() == "PASS"


def validate(project: BookProject, produces: Status) -> GateCheck:
    """Return whether the artifacts proving ``produces`` exist and pass."""
    p = project

    if produces == Status.SOURCE_INGESTED:
        if not p.source_clean.exists():
            return _fail("source/source_text.txt missing — call ingest_source")
        if not p.source_manifest.exists():
            return _fail("source/source_manifest.json missing")
        return _ok()

    if produces == Status.SOURCE_SPLIT:
        if not p.toc_json.exists():
            return _fail("source/toc.json missing — call split_chapters")
        if not list(p.chapters_src.glob("*.md")):
            return _fail("chapters/src/ has no chapter files")
        return _ok()

    if produces == Status.GLOBAL_RESEARCH_DONE:
        ack = p.root / "qa/benchmark/global_research_ack.md"
        return _ok() if ack.exists() else _fail("qa/benchmark/global_research_ack.md missing")

    if produces == Status.BOOK_RESEARCH_DONE:
        if not p.book_research.exists():
            return _fail("metadata/book_specific_translation_research.md missing")
        if not p.style_profile.exists():
            return _fail("metadata/style_profile.md missing")
        return _ok()

    if produces == Status.PRETRANSLATION_PASS:
        if not p.pretranslation_report.exists():
            return _fail("qa/pretranslation/pretranslation_report.md missing")
        if not _contains_pass(p.pretranslation_report.read_text(encoding="utf-8")):
            return _fail("pretranslation_report.md does not conclude 'result: PASS'")
        return _ok()

    if produces == Status.GLOSSARY_STYLE_DONE:
        if not p.terms_csv.exists():
            return _fail("glossary/terms.csv missing")
        rows = [r for r in p.terms_csv.read_text(encoding="utf-8").splitlines() if r.strip()]
        if len(rows) < 2:
            return _fail("glossary/terms.csv has no term rows (header + >=1 row required)")
        if not p.style_guide.exists():
            return _fail("glossary/style_guide.md missing")
        return _ok()

    if produces == Status.TRANSLATED:
        src = {q.stem for q in p.chapters_src.glob("*.md")}
        done = {q.stem for q in p.chapters_translated.glob("*.md")}
        missing = sorted(src - done)
        if missing:
            return _fail(f"{len(missing)} chapters not translated: {missing[:5]}")
        return _ok() if src else _fail("no source chapters found")

    if produces == Status.CHAPTER_POST_CONTROL_PASS:
        translated = sorted(q.stem for q in p.chapters_translated.glob("*.md"))
        if not translated:
            return _fail("no translated chapters to control")
        for slug in translated:
            ctrl = p.chapter_control(slug)
            if not ctrl.exists():
                return _fail(f"missing control file for {slug}")
            if not _has_zero_issue_pass(ctrl.read_text(encoding="utf-8")):
                return _fail(f"{slug}.control.md is not a zero-issue FULL_CHAPTER PASS")
        return _ok()

    if produces == Status.CHAPTER_GATES_PASS:
        translated = sorted(q.stem for q in p.chapters_translated.glob("*.md"))
        for slug in translated:
            gate = p.chapter_gate(slug)
            if not gate.exists() or not _contains_pass(gate.read_text(encoding="utf-8")):
                return _fail(f"{slug}.gate.md missing or not PASS")
            if not (p.chapters_final / f"{slug}.md").exists():
                return _fail(f"chapters/final/{slug}.md missing")
        return _ok()

    if produces == Status.PREPRODUCTION_SPEC_DONE:
        return _ok() if p.production_spec.exists() else _fail("production_spec.md missing")

    if produces == Status.PREPRODUCTION_SAMPLE_PASS:
        if not p.sample_review.exists():
            return _fail("sample_review.md missing")
        txt = p.sample_review.read_text(encoding="utf-8")
        if not _contains_pass(txt, key="sample_review_status"):
            return _fail("sample_review_status is not PASS")
        if not p.sample_epub.exists():
            return _fail("sample_book.epub missing")
        return _ok()

    if produces == Status.EPUB_BUILT:
        if not p.book_epub.exists():
            return _fail("output/book.epub missing")
        if not _report_ok(p.publication_lint_report):
            return _fail("publication_lint.json missing or has hard errors")
        if not _report_ok(p.asset_manifest_report):
            return _fail("asset_manifest_check.json missing or has hard errors")
        return _ok()

    if produces == Status.RANDOM_SPOTCHECK_PASS:
        return _validate_spotcheck(p)

    if produces == Status.INDEPENDENT_REVIEW_PASS:
        a = p.root / "reviews/agent_a/review.md"
        b = p.root / "reviews/agent_b/review.md"
        for f, label in ((a, "agent_a"), (b, "agent_b")):
            if not f.exists():
                return _fail(f"reviews/{label}/review.md missing")
            if not _contains_pass(f.read_text(encoding="utf-8"), key="result") and \
               "PASS" not in f.read_text(encoding="utf-8").upper():
                return _fail(f"reviews/{label}/review.md not PASS")
        return _ok()

    if produces == Status.RELEASE_PASS:
        return _validate_release(p)

    if produces == Status.FINAL_OUTPUT_PASS:
        return _ok() if p.final_manifest.exists() else _fail("output/final_manifest.md missing")

    if produces == Status.RETROSPECTIVE_DONE:
        if not p.retrospective.exists():
            return _fail("retrospective/book_retrospective.md missing")
        if not (p.root / "retrospective/template_update_suggestions.md").exists():
            return _fail("retrospective/template_update_suggestions.md missing")
        return _ok()

    return _ok()


def _report_ok(path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(data.get("ok")) and int(data.get("hard_errors", data.get("errors", 0)) or 0) == 0


def _validate_spotcheck(p: BookProject) -> GateCheck:
    rounds = sorted(p.random_spotcheck_dir.glob("round_*"))
    if not rounds:
        return _fail("no reviews/random_spotcheck/round_* found")
    report = rounds[-1] / "validation_report.json"
    if not report.exists():
        return _fail("latest round has no validation_report.json")
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except Exception:
        return _fail("validation_report.json not valid JSON")
    if str(data.get("status", "")).upper() != "PASS":
        return _fail("validation_report.json status != PASS")
    if float(data.get("release_confidence", 0) or 0) < 0.80:
        return _fail("release_confidence < 0.80")
    return _ok()


def _validate_release(p: BookProject) -> GateCheck:
    for state_file in (
        p.release_dir / "release_state.json",
        p.private_artifacts_dir / "private_artifact_state.json",
    ):
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                return _fail(f"{state_file.name} not valid JSON")
            if str(data.get("latest_status", "")).upper() == "PASS":
                return _ok()
            return _fail(f"{state_file.name} latest_status != PASS")
    return _fail("no release_state.json / private_artifact_state.json with PASS")
