"""Tests for the Langfuse experiment integration with mocked client."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from abi.eval.datasets import load_eval_dataset
from abi.eval.langfuse_experiment import (
    DISABLED,
    attach_scores,
    derive_dataset_name,
    ensure_dataset,
    finalize_run,
    fresh_trace_id,
    link_sample,
    start_dataset_run,
)
from abi.types.run import LangfuseConfig


@dataclass
class FakeItem:
    item_id: str
    links: list[dict] = field(default_factory=list)

    def link(
        self,
        trace_or_observation: Any,
        run_name: str,
        run_metadata: Any = None,
        run_description: str | None = None,
        trace_id: str | None = None,
        observation_id: str | None = None,
    ) -> None:
        self.links.append(
            {"trace_id": trace_id, "run_name": run_name, "run_metadata": run_metadata}
        )


@dataclass
class FakeTrace:
    id: str
    metadata: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    name: str = ""


@dataclass
class FakeLangfuseClient:
    datasets_created: list[dict] = field(default_factory=list)
    items_created: list[dict] = field(default_factory=list)
    scores: list[dict] = field(default_factory=list)
    traces: list[FakeTrace] = field(default_factory=list)
    # Map item_id -> FakeItem; reused across re-creations (idempotent).
    _items: dict[str, FakeItem] = field(default_factory=dict)

    def create_dataset(self, name: str, description: str = "", metadata: Any = None) -> None:
        # Simulate "raise if exists" so production code's try/except path is exercised.
        for d in self.datasets_created:
            if d["name"] == name:
                raise Exception("Dataset already exists")
        self.datasets_created.append(
            {"name": name, "description": description, "metadata": metadata}
        )

    def create_dataset_item(
        self,
        *,
        dataset_name: str,
        input: Any,
        expected_output: Any,
        metadata: Any,
        id: str | None = None,
    ) -> FakeItem:
        self.items_created.append(
            {"dataset_name": dataset_name, "id": id, "input": input}
        )
        item = self._items.setdefault(id or f"auto-{len(self._items)}", FakeItem(item_id=id or ""))
        return item

    def score(
        self,
        *,
        trace_id: str,
        name: str,
        value: Any,
        data_type: str = "NUMERIC",
        comment: str = "",
    ) -> None:
        self.scores.append(
            {"trace_id": trace_id, "name": name, "value": value, "data_type": data_type}
        )

    def trace(self, *, name: str = "", metadata: Any = None, tags: Any = None) -> FakeTrace:
        t = FakeTrace(id=fresh_trace_id(), name=name, metadata=metadata or {}, tags=tags or [])
        self.traces.append(t)
        return t

    def flush(self) -> None:
        pass


class TestDeriveDatasetName:
    def test_strips_dynamic_options(self) -> None:
        assert (
            derive_dataset_name("wmt24pp:en-zh_CN:literary:limit_docs=1")
            == "wmt24pp-en-zh_CN-literary-v1"
        )

    def test_empty_returns_empty(self) -> None:
        assert derive_dataset_name(None) == ""
        assert derive_dataset_name("") == ""


class TestEnsureDataset:
    def test_idempotent_pushes_all_items(self) -> None:
        ds = load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true")
        client = FakeLangfuseClient()
        items_first = ensure_dataset(client, "wmt24pp-en-zh_CN-literary-v1", ds)
        items_second = ensure_dataset(client, "wmt24pp-en-zh_CN-literary-v1", ds)
        assert len(items_first) == len(ds.paragraphs)
        # Second call uses the same item ids → no duplicate FakeItem objects.
        assert set(items_first.keys()) == set(items_second.keys())
        # FakeLangfuseClient.create_dataset raised on the second invocation; the
        # code swallowed it (best-effort upsert).
        assert len(client.datasets_created) == 1

    def test_item_input_has_context_window(self) -> None:
        ds = load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true")
        client = FakeLangfuseClient()
        ensure_dataset(client, "ds-v1", ds)
        a_items = [
            i for i in client.items_created
            if i["input"]["document_id"] == "doc-A"
        ]
        # doc-A has two paragraphs: second one's prev_window contains the first.
        second = next(i for i in a_items if i["input"]["segment_id"] == "2")
        assert any("April" in t for t in second["input"]["prev_window"])


class TestStartDatasetRun:
    def test_returns_disabled_when_no_client(self) -> None:
        config = LangfuseConfig()
        ctx = start_dataset_run(
            config, None,
            dataset=load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true"),
            run_name="r1", run_metadata={"dataset_spec": "wmt24pp:en-zh_CN:literary"},
        )
        assert ctx is DISABLED
        assert ctx.enabled is False

    def test_returns_context_with_url(self) -> None:
        config = LangfuseConfig(host="https://langfuse.local")
        client = FakeLangfuseClient()
        ds = load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true")
        ctx = start_dataset_run(
            config, client,
            dataset=ds,
            run_name="r1",
            run_metadata={"dataset_spec": "wmt24pp:en-zh_CN:literary"},
        )
        assert ctx.enabled
        assert ctx.dataset_name == "wmt24pp-en-zh_CN-literary-v1"
        assert ctx.run_url is not None
        assert "/datasets/wmt24pp-en-zh_CN-literary-v1/runs/r1" in ctx.run_url


class TestLinkAndScore:
    def _ctx(self) -> Any:
        config = LangfuseConfig(host="https://langfuse.local")
        client = FakeLangfuseClient()
        ds = load_eval_dataset("wmt24pp:en-zh_CN:literary:stub=true")
        ctx = start_dataset_run(
            config, client,
            dataset=ds, run_name="r1",
            run_metadata={"dataset_spec": "wmt24pp:en-zh_CN:literary"},
        )
        return client, ctx, ds

    def test_link_then_attach_scores(self) -> None:
        client, ctx, ds = self._ctx()
        pid = ds.paragraphs[0].paragraph_id
        trace_id = fresh_trace_id()
        link_sample(ctx, paragraph_id=pid, trace_id=trace_id)
        assert client._items[pid].links[0]["trace_id"] == trace_id
        attach_scores(
            ctx,
            trace_id=trace_id,
            scores=[
                {"name": "likert.abi.mean", "value": 4.5},
                {"name": "pairwise.abi_vs_baseline", "value": 1.0},
            ],
        )
        assert len(client.scores) == 2
        assert client.scores[0]["name"] == "likert.abi.mean"
        assert client.scores[0]["data_type"] == "NUMERIC"

    def test_link_unknown_paragraph_id_is_noop(self) -> None:
        client, ctx, _ = self._ctx()
        link_sample(ctx, paragraph_id="not-in-dataset", trace_id="abc")
        # No item got a link.
        assert all(not i.links for i in client._items.values())

    def test_attach_scores_disabled_is_noop(self) -> None:
        attach_scores(DISABLED, trace_id="x", scores=[{"name": "n", "value": 1.0}])

    def test_finalize_run_pushes_aggregate_scores(self) -> None:
        client, ctx, _ = self._ctx()
        finalize_run(
            ctx,
            aggregate_scores=[
                {"name": "abi.winrate_vs_baseline", "value": 0.83},
                {"name": "abi.likert_mean", "value": 4.2},
            ],
        )
        # Summary trace and two scores against its trace_id.
        assert len(client.traces) == 1
        summary_id = client.traces[0].id
        names = [s["name"] for s in client.scores if s["trace_id"] == summary_id]
        assert "abi.winrate_vs_baseline" in names
        assert "abi.likert_mean" in names
