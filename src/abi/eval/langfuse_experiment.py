"""Langfuse Experiments integration for the eval pipeline.

This module is responsible for:

1. **Datasets**: idempotently upserting eval-dataset items to Langfuse so that
   re-running the same spec doesn't pile up duplicates.
2. **Runs**: opening a Dataset Run record per ``abi eval`` invocation so
   per-sample traces + scores roll up to an experiment row in the UI.
3. **Scoring**: pushing both per-sample (judge-likert means, per-pair
   verdicts) and run-level (aggregate winrates) scores.

Failures are swallowed with a warning — the eval pipeline must keep
running even if Langfuse is misconfigured or down.

We use the **low-level v2 SDK** (``Langfuse.create_dataset`` /
``create_dataset_item`` / ``score`` / ``DatasetItem.link``) rather than
the v3 ``run_experiment`` runner because:

- ABI translations are pre-computed (not a per-item task), so the v3 task
  abstraction doesn't fit naturally.
- We already have our own concurrency / budget / retry plumbing on
  ``LLMRouter``; the v3 runner would duplicate it.

The cost: dataset-run aggregate scores have to be attached at the trace
level for now (one synthetic "summary" trace) since v2 doesn't expose
run-level scores directly. Per-item scores work as expected.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from abi.eval.datasets import EvalDataset
from abi.types.run import LangfuseConfig

_log = logging.getLogger(__name__)


@dataclass
class ExperimentContext:
    """Per-eval-run handle carried by the pipeline.

    ``items_by_paragraph_id`` is a lookup from ABI's paragraph_id (the
    stable hash returned by the dataset adapter) to the corresponding
    Langfuse dataset-item handle so we can ``link`` traces at score time.
    """

    client: Any
    dataset_name: str
    run_name: str
    run_metadata: dict[str, Any]
    items_by_paragraph_id: dict[str, Any] = field(default_factory=dict)
    run_url: str | None = None

    @property
    def enabled(self) -> bool:
        return self.client is not None


DISABLED = ExperimentContext(
    client=None, dataset_name="", run_name="", run_metadata={}
)


def derive_dataset_name(dataset_spec: str | None) -> str:
    """Stable Langfuse dataset name derived from a dataset spec.

    Drops ``limit_docs`` and similar dynamic options so smoke runs and full
    runs share the same dataset entry. Format: ``<name>-<lp>-<register>-v1``.
    """
    if not dataset_spec:
        return ""
    parts = dataset_spec.split(":")
    name = parts[0] if parts else ""
    lp = parts[1] if len(parts) > 1 else ""
    register = parts[2] if len(parts) > 2 else ""
    pieces = [p for p in (name, lp, register) if p]
    return "-".join(pieces) + "-v1"


def ensure_dataset(
    client: Any,
    dataset_name: str,
    dataset: EvalDataset,
    *,
    paragraph_id_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Idempotently upsert dataset items to Langfuse.

    ``paragraph_id_map`` (optional) is ``segment_id -> abi_paragraph_id``.
    When present, we use the ABI paragraph_id as the Langfuse item id so
    later trace-linking by ABI's paragraph_id is a direct lookup.

    Returns a dict from paragraph_id to the live DatasetItem handle.
    """
    if not client:
        return {}
    try:
        client.create_dataset(
            name=dataset_name,
            description=f"ABI eval — {dataset.title}",
            metadata={
                "source_language": dataset.source_language,
                "target_language": dataset.target_language,
                "register": dataset.register,
                "title": dataset.title,
                "n_paragraphs": len(dataset.paragraphs),
            },
        )
    except Exception as exc:  # pragma: no cover — best-effort upsert
        _log.debug("create_dataset noop or failed (likely exists): %s", exc)

    items_by_pid: dict[str, Any] = {}
    for p in dataset.paragraphs:
        item_id = (
            paragraph_id_map.get(p.segment_id, p.paragraph_id)
            if paragraph_id_map else p.paragraph_id
        )
        # Same-window context (prev/next within the same document) helps the
        # judge — we embed it in ``input`` so the dataset is self-contained.
        doc_paras = dataset.doc_to_paragraphs.get(p.document_id, [])
        idx = next(
            (i for i, x in enumerate(doc_paras) if x.segment_id == p.segment_id), -1
        )
        prev_window = (
            [x.source_text for x in doc_paras[max(0, idx - 2): idx]] if idx > 0 else []
        )
        next_window = (
            [x.source_text for x in doc_paras[idx + 1: idx + 3]] if idx >= 0 else []
        )
        try:
            item = client.create_dataset_item(
                dataset_name=dataset_name,
                input={
                    "source": p.source_text,
                    "document_id": p.document_id,
                    "segment_id": p.segment_id,
                    "prev_window": prev_window,
                    "next_window": next_window,
                },
                expected_output={
                    "text": p.reference_text,
                    "register": dataset.register,
                },
                metadata={
                    "lp": f"{dataset.source_language}-{dataset.target_language}",
                    "domain": dataset.register,
                    "position": p.position,
                },
                id=item_id,
            )
            items_by_pid[item_id] = item
        except Exception as exc:  # pragma: no cover
            _log.warning(
                "create_dataset_item failed for %s: %s", p.segment_id, exc
            )
    return items_by_pid


def start_dataset_run(
    config: LangfuseConfig,
    client: Any,
    *,
    dataset: EvalDataset,
    run_name: str,
    run_metadata: dict[str, Any],
    paragraph_id_map: dict[str, str] | None = None,
) -> ExperimentContext:
    """Open a Dataset Run and prepare the item lookup for score attachment."""
    if not client:
        return DISABLED
    dataset_name = derive_dataset_name(
        run_metadata.get("dataset_spec") if isinstance(run_metadata, dict) else None
    )
    if not dataset_name:
        return DISABLED
    items_by_pid = ensure_dataset(
        client, dataset_name, dataset, paragraph_id_map=paragraph_id_map
    )
    run_url = _build_run_url(config.host, dataset_name, run_name)
    return ExperimentContext(
        client=client,
        dataset_name=dataset_name,
        run_name=run_name,
        run_metadata=run_metadata,
        items_by_paragraph_id=items_by_pid,
        run_url=run_url,
    )


def link_sample(
    ctx: ExperimentContext,
    *,
    paragraph_id: str,
    trace_id: str,
) -> None:
    """Link an existing trace to the dataset item for this sample.

    No-ops when the experiment is disabled or the item id can't be found.
    """
    if not ctx.enabled:
        return
    item = ctx.items_by_paragraph_id.get(paragraph_id)
    if item is None:
        _log.debug("link_sample: no dataset item for paragraph_id=%s", paragraph_id)
        return
    try:
        item.link(
            None,
            run_name=ctx.run_name,
            run_metadata=ctx.run_metadata,
            trace_id=trace_id,
        )
    except Exception as exc:  # pragma: no cover
        _log.warning("dataset_item.link failed for %s: %s", paragraph_id, exc)


def attach_scores(
    ctx: ExperimentContext,
    *,
    trace_id: str,
    scores: list[dict[str, Any]],
) -> None:
    """Attach Numeric / Boolean / Categorical scores to a trace.

    Each ``scores`` entry is ``{name, value, data_type?, comment?}``.
    ``data_type`` defaults to NUMERIC when ``value`` is a number,
    CATEGORICAL otherwise.
    """
    if not ctx.enabled or not scores:
        return
    for s in scores:
        try:
            ctx.client.score(
                trace_id=trace_id,
                name=s["name"],
                value=s["value"],
                data_type=s.get("data_type") or _infer_data_type(s["value"]),
                comment=s.get("comment", ""),
            )
        except Exception as exc:  # pragma: no cover
            _log.warning("score push failed (%s): %s", s.get("name"), exc)


def finalize_run(
    ctx: ExperimentContext,
    *,
    aggregate_scores: list[dict[str, Any]],
) -> None:
    """Push run-level aggregate scores via a synthetic summary trace.

    v2 doesn't expose direct run-level scores, so we record a one-off
    trace named ``"abi.eval.run_summary"`` and attach the aggregates to it.
    The trace is linked to every dataset item that was scored in this run,
    so the Langfuse UI shows aggregates next to per-sample data.
    """
    if not ctx.enabled or not aggregate_scores:
        return
    try:
        summary_trace = ctx.client.trace(
            name="abi.eval.run_summary",
            metadata=ctx.run_metadata,
            tags=["abi.eval", "summary"],
        )
        summary_id = summary_trace.id
    except Exception as exc:  # pragma: no cover
        _log.warning("summary trace failed: %s", exc)
        return
    attach_scores(ctx, trace_id=summary_id, scores=aggregate_scores)
    # Link the summary trace to *one* representative item so it shows up
    # inside the dataset run. Picking the first item is enough — the run
    # is keyed on ``run_name``, not on the linked item.
    items = list(ctx.items_by_paragraph_id.values())
    if items:
        try:
            items[0].link(
                None,
                run_name=ctx.run_name,
                run_metadata=ctx.run_metadata,
                trace_id=summary_id,
            )
        except Exception as exc:  # pragma: no cover
            _log.debug("summary link failed: %s", exc)


def fresh_trace_id() -> str:
    """Generate a trace id that can be passed into both Langfuse and our logs."""
    return uuid.uuid4().hex


def _infer_data_type(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, (int, float)):
        return "NUMERIC"
    return "CATEGORICAL"


def _build_run_url(host: str, dataset_name: str, run_name: str) -> str:
    """Best-effort Langfuse UI URL for the dataset run.

    Uses the project-aware route when a project id is available via the
    ``LANGFUSE_PROJECT_ID`` env var; falls back to the dataset-name route.
    """
    host = host.rstrip("/")
    project = os.environ.get("LANGFUSE_PROJECT_ID", "")
    if project:
        return f"{host}/project/{project}/datasets/{dataset_name}/runs/{run_name}"
    return f"{host}/datasets/{dataset_name}/runs/{run_name}"
