"""Pipeline state machine — the 28 states from PDBT ``PIPELINE_SPEC.md`` §3.

This is the durable spine of the agentic pipeline. ``PipelineState`` is
persisted as ``state/pipeline_state.json`` inside a book project and is the
source of truth for "what stage are we on / did the last gate pass".

The states mirror ``template/epub_pipeline/common/PIPELINE_SPEC.md`` verbatim so
the ported stage prompts can refer to the same vocabulary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Status(StrEnum):
    """The 28 pipeline states (plus ``FAILED``)."""

    INIT = "INIT"
    SOURCE_INGESTED = "SOURCE_INGESTED"
    SOURCE_SPLIT = "SOURCE_SPLIT"
    GLOBAL_RESEARCH_DONE = "GLOBAL_RESEARCH_DONE"
    BOOK_RESEARCH_DONE = "BOOK_RESEARCH_DONE"
    PRETRANSLATION_FAILED = "PRETRANSLATION_FAILED"
    PRETRANSLATION_PASS = "PRETRANSLATION_PASS"
    GLOSSARY_STYLE_DONE = "GLOSSARY_STYLE_DONE"
    TRANSLATING = "TRANSLATING"
    TRANSLATED = "TRANSLATED"
    CHAPTER_POST_CONTROL_PASS = "CHAPTER_POST_CONTROL_PASS"
    REVIEWING = "REVIEWING"
    CHAPTER_GATES_PASS = "CHAPTER_GATES_PASS"
    PREPRODUCTION_SPEC_DONE = "PREPRODUCTION_SPEC_DONE"
    PREPRODUCTION_SAMPLE_FAILED = "PREPRODUCTION_SAMPLE_FAILED"
    PREPRODUCTION_SAMPLE_PASS = "PREPRODUCTION_SAMPLE_PASS"
    EPUB_BUILT = "EPUB_BUILT"
    RANDOM_SPOTCHECK_FAILED = "RANDOM_SPOTCHECK_FAILED"
    RANDOM_SPOTCHECK_PASS = "RANDOM_SPOTCHECK_PASS"
    INDEPENDENT_REVIEW_FAILED = "INDEPENDENT_REVIEW_FAILED"
    INDEPENDENT_REVIEW_PASS = "INDEPENDENT_REVIEW_PASS"
    REVISION_ROUTING_REQUIRED = "REVISION_ROUTING_REQUIRED"
    FINAL_OUTPUT_PASS = "FINAL_OUTPUT_PASS"
    RELEASE_DRAFT = "RELEASE_DRAFT"
    RELEASE_PASS = "RELEASE_PASS"
    RETROSPECTIVE_DONE = "RETROSPECTIVE_DONE"
    DONE = "DONE"
    FAILED = "FAILED"


# Linear "happy path" ordering of the macro pipeline. The orchestrator walks
# this list; gate failures route backwards (handled by the revision-routing
# stage), they do not advance the index.
HAPPY_PATH: list[Status] = [
    Status.INIT,
    Status.SOURCE_INGESTED,
    Status.SOURCE_SPLIT,
    Status.GLOBAL_RESEARCH_DONE,
    Status.BOOK_RESEARCH_DONE,
    Status.PRETRANSLATION_PASS,
    Status.GLOSSARY_STYLE_DONE,
    Status.TRANSLATED,
    Status.CHAPTER_POST_CONTROL_PASS,
    Status.CHAPTER_GATES_PASS,
    Status.PREPRODUCTION_SPEC_DONE,
    Status.PREPRODUCTION_SAMPLE_PASS,
    Status.EPUB_BUILT,
    Status.RANDOM_SPOTCHECK_PASS,
    Status.INDEPENDENT_REVIEW_PASS,
    Status.RELEASE_PASS,
    Status.FINAL_OUTPUT_PASS,
    Status.RETROSPECTIVE_DONE,
    Status.DONE,
]


def happy_index(status: Status) -> int:
    """Position of ``status`` on the happy path, or -1 if off-path."""
    try:
        return HAPPY_PATH.index(status)
    except ValueError:
        return -1

PublicationMode = str  # "public_domain" | "licensed" | "private_use"


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StateEvent(BaseModel):
    """One transition record kept in ``history`` for auditability."""

    model_config = ConfigDict(frozen=True)

    ts: str = Field(default_factory=_utcnow)
    status: Status
    step: str
    note: str = ""


class PipelineState(BaseModel):
    """Durable book-project state. Persisted to ``state/pipeline_state.json``.

    Unlike the IR/config models this one is *mutable* — it is the single piece
    of evolving run state. We still serialise it explicitly via
    :func:`save_state` so writes stay atomic.
    """

    model_config = ConfigDict(use_enum_values=False)

    book_slug: str
    source_lang: str
    target_lang: str
    source_target: str
    publication_mode: PublicationMode = "public_domain"
    profile: str | None = None

    status: Status = Status.INIT
    current_step: str = "00_orchestrator"
    last_error: str | None = None

    # Map of logical artifact name -> project-relative path produced so far.
    artifacts: dict[str, str] = Field(default_factory=dict)
    # Free-form per-stage gate results, e.g. {"pretranslation": "PASS"}.
    gates: dict[str, str] = Field(default_factory=dict)

    created_at: str = Field(default_factory=_utcnow)
    updated_at: str = Field(default_factory=_utcnow)
    history: list[StateEvent] = Field(default_factory=list)

    def advance(self, status: Status, *, step: str, note: str = "") -> None:
        """Record a forward (or routed) transition and clear any error."""
        self.status = status
        self.current_step = step
        self.last_error = None
        self.updated_at = _utcnow()
        self.history.append(StateEvent(status=status, step=step, note=note))

    def fail(self, *, step: str, error: str) -> None:
        self.status = Status.FAILED
        self.current_step = step
        self.last_error = error
        self.updated_at = _utcnow()
        self.history.append(StateEvent(status=Status.FAILED, step=step, note=error))

    def record_gate(self, name: str, result: str) -> None:
        self.gates[name] = result
        self.updated_at = _utcnow()

    def record_artifact(self, name: str, rel_path: str) -> None:
        self.artifacts[name] = rel_path
        self.updated_at = _utcnow()

    def is_done(self) -> bool:
        return self.status == Status.DONE
