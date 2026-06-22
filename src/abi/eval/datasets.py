"""Dataset adapters. WMT24++ (``google/wmt24pp``) is the primary calibration source.

Spec grammar (``--dataset``)::

    wmt24pp:<config>:<domain>[:stub=true][:limit_docs=N][:limit=N]

e.g. ``wmt24pp:en-zh_CN:literary`` or ``wmt24pp:en-ja_JP:literary:limit=200``.

``stub=true`` uses a tiny built-in offline fixture (for CI / no-network runs).
Each row becomes an :class:`EvalTriple` with ``reference`` = the post-edit
(``target``), which WMT24++ recommends as the default reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from abi.eval.types import EvalTriple
from abi.types.ids import paragraph_id


@dataclass(frozen=True)
class DatasetSpec:
    adapter: str
    config: str
    domain: str | None = None
    stub: bool = False
    limit_docs: int | None = None
    limit: int | None = None
    options: dict[str, str] = field(default_factory=dict)

    @property
    def source_lang(self) -> str:
        return self.config.split("-", 1)[0]

    @property
    def target_lang(self) -> str:
        # "en-zh_CN" -> target "zh_CN" -> tag "zh-CN" (band lookup uses base "zh").
        tgt = self.config.split("-", 1)[1] if "-" in self.config else self.config
        return tgt.replace("_", "-")


def parse_dataset_spec(spec: str) -> DatasetSpec:
    """Parse the ``wmt24pp:<config>:<domain>:opt=val...`` mini-grammar."""
    parts = [p for p in spec.split(":") if p != ""]
    if len(parts) < 2:
        raise ValueError(
            f"invalid dataset spec {spec!r}; expected "
            "'wmt24pp:<config>:<domain>[:stub=true][:limit_docs=N][:limit=N]', "
            "e.g. 'wmt24pp:en-zh_CN:literary'."
        )
    adapter, config = parts[0], parts[1]
    domain: str | None = None
    options: dict[str, str] = {}
    for raw in parts[2:]:
        if "=" in raw:
            k, v = raw.split("=", 1)
            options[k] = v
        elif domain is None:
            domain = raw
        else:
            options[raw] = "true"
    return DatasetSpec(
        adapter=adapter,
        config=config,
        domain=domain,
        stub=options.get("stub", "").lower() in {"1", "true", "yes"},
        limit_docs=int(options["limit_docs"]) if "limit_docs" in options else None,
        limit=int(options["limit"]) if "limit" in options else None,
        options=options,
    )


# --- offline stub fixture (en-zh_CN literary), for CI / no-network calibration ---
_STUB_ROWS: list[dict[str, str]] = [
    {
        "document_id": "stub-001",
        "source": "It was the best of times, it was the worst of times.",
        "target": "那是最好的时代，那是最坏的时代。",
    },
    {
        "document_id": "stub-001",
        "source": "The sky above the port was the color of television, tuned to a dead channel.",
        "target": "港口上空的天色，是电视调到空频道时的那种颜色。",
    },
    {
        "document_id": "stub-002",
        "source": "All happy families are alike; each unhappy family is unhappy in its own way.",
        "target": "幸福的家庭都是相似的，不幸的家庭各有各的不幸。",
    },
    {
        "document_id": "stub-002",
        "source": "In 1925 he published a slim volume of 200 pages that sold 4,000 copies.",
        "target": "1925 年，他出版了一本仅 200 页的小书，售出 4,000 册。",
    },
    {
        "document_id": "stub-003",
        "source": "Call me Ishmael. Some years ago, never mind how long precisely, I went to sea.",
        "target": "叫我以实玛利吧。几年前——具体多久就别管了——我出海去了。",
    },
]


def _load_stub(spec: DatasetSpec) -> list[EvalTriple]:
    rows = _STUB_ROWS
    if spec.limit is not None:
        rows = rows[: spec.limit]
    out: list[EvalTriple] = []
    for i, r in enumerate(rows):
        out.append(
            EvalTriple(
                paragraph_id=paragraph_id(r["source"], i),
                source=r["source"],
                reference=r["target"],
                source_lang=spec.source_lang,
                target_lang=spec.target_lang,
                domain=spec.domain or "literary",
                document_id=r["document_id"],
            )
        )
    return out


def load_triples(spec: DatasetSpec) -> list[EvalTriple]:
    """Materialise a dataset spec into reference-bearing :class:`EvalTriple`s."""
    if spec.adapter != "wmt24pp":
        raise ValueError(
            f"unknown dataset adapter {spec.adapter!r}; only 'wmt24pp' is supported. "
            "Use a spec like 'wmt24pp:en-zh_CN:literary'."
        )
    if spec.stub:
        return _load_stub(spec)
    return _load_wmt24pp(spec)


def _load_wmt24pp(spec: DatasetSpec) -> list[EvalTriple]:
    # Imported lazily so stub/offline paths never require the heavy dependency.
    from datasets import load_dataset

    ds = load_dataset("google/wmt24pp", spec.config, split="train")
    triples: list[EvalTriple] = []
    seen_docs: set[str] = set()
    for i, row in enumerate(ds):
        if spec.domain and str(row.get("domain")) != spec.domain:
            continue
        if row.get("is_bad_source"):
            continue
        doc = str(row.get("document_id", ""))
        if spec.limit_docs is not None:
            if doc not in seen_docs and len(seen_docs) >= spec.limit_docs:
                continue
            seen_docs.add(doc)
        source = str(row.get("source", ""))
        reference = str(row.get("target", "") or row.get("original_target", ""))
        if not source.strip() or not reference.strip():
            continue
        triples.append(
            EvalTriple(
                paragraph_id=paragraph_id(source, i),
                source=source,
                reference=reference,
                source_lang=spec.source_lang,
                target_lang=spec.target_lang,
                domain=str(row.get("domain")) if row.get("domain") else None,
                document_id=doc or None,
            )
        )
        if spec.limit is not None and len(triples) >= spec.limit:
            break
    return triples
