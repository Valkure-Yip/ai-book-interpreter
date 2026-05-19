"""Common types and spec parsing for evaluation datasets.

A "dataset spec" is a colon-separated string identifying which corpus to load:

    <name>:<language_pair>:<register>[:<key=value>...]

Examples:
    wmt24pp:en-zh_CN:literary
    wmt24pp:en-zh_CN:literary:limit_docs=1

Each spec maps to an adapter that returns an :class:`EvalDataset` — a
paragraph-keyed bundle of source / human-reference / context information that
the eval pipeline consumes as a drop-in replacement for the local book file.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DatasetParagraph:
    """One unit of evaluation — a single source paragraph + human reference.

    ``document_id`` lets us reconstruct same-document neighbours for coherence
    judging; ``position`` is a global position across the whole dataset (used
    by alignment / sampler); ``segment_id`` is the upstream stable id (e.g.
    wmt24pp's ``segment_id``) used to form a deterministic paragraph_id.
    """

    paragraph_id: str
    source_text: str
    reference_text: str
    document_id: str
    segment_id: str
    position: int


@dataclass(frozen=True)
class EvalDataset:
    """Adapter output: an ordered list of paragraphs grouped by document."""

    book_id: str
    title: str
    source_language: str
    target_language: str
    register: str
    paragraphs: list[DatasetParagraph]
    doc_to_paragraphs: dict[str, list[DatasetParagraph]] = field(default_factory=dict)


@dataclass(frozen=True)
class DatasetSpec:
    """Parsed dataset-spec string."""

    name: str
    language_pair: str
    register: str
    options: dict[str, str] = field(default_factory=dict)

    def as_canonical(self) -> str:
        opts = "".join(f":{k}={v}" for k, v in sorted(self.options.items()))
        return f"{self.name}:{self.language_pair}:{self.register}{opts}"


def parse_spec(raw: str) -> DatasetSpec:
    """Parse ``name:lp:register[:k=v[:k=v]...]`` into a :class:`DatasetSpec`.

    Raises :class:`ValueError` on malformed input.
    """
    if not raw or not isinstance(raw, str):
        raise ValueError("dataset spec must be a non-empty string")
    parts = [p.strip() for p in raw.split(":")]
    if len(parts) < 3:
        raise ValueError(
            f"dataset spec must be 'name:language_pair:register[:k=v...]' "
            f"(got {raw!r})"
        )
    name, language_pair, register = parts[0], parts[1], parts[2]
    options: dict[str, str] = {}
    for kv in parts[3:]:
        if not kv:
            continue
        if "=" not in kv:
            raise ValueError(f"option must be key=value (got {kv!r})")
        k, v = kv.split("=", 1)
        options[k.strip()] = v.strip()
    return DatasetSpec(
        name=name, language_pair=language_pair, register=register, options=options
    )


def compute_book_id(spec: DatasetSpec) -> str:
    """Stable book_id derived from the spec (without dynamic options).

    ``limit_docs`` is intentionally excluded so smoke runs and full runs share
    the same ABI run directory layout under ``runs/<book_id>/``.
    """
    key = f"{spec.name}:{spec.language_pair}:{spec.register}"
    return hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


DatasetLoader = Callable[[DatasetSpec], EvalDataset]

# Adapter registry. Each adapter has signature ``(spec) -> EvalDataset``.
# Adapters are registered via :func:`register_adapter` from their own module.
_ADAPTERS: dict[str, DatasetLoader] = {}


def register_adapter(name: str, loader: DatasetLoader) -> None:
    _ADAPTERS[name] = loader


def load_eval_dataset(spec: str | DatasetSpec) -> EvalDataset:
    """Resolve and run the adapter for ``spec``."""
    parsed = parse_spec(spec) if isinstance(spec, str) else spec
    loader = _ADAPTERS.get(parsed.name)
    if loader is None:
        raise ValueError(
            f"unknown dataset {parsed.name!r}; known: {sorted(_ADAPTERS)}"
        )
    return loader(parsed)


# Lines that might accidentally trigger ABI's TXT heading detector. We
# break them with a zero-width space so the paragraph stays intact yet
# still reads naturally to the human / the LLM.
#
# This MUST stay in sync with ``abi.ir.txt`` — anything that file's
# ``_is_heading_line`` recognizes as a heading would silently steal a
# paragraph from us and break 1:1 alignment with the dataset references.
_HEADING_TRIGGER = re.compile(
    r"^(\s*PART\s+[IVXLC0-9]"
    r"|\s*第[一二三四五六七八九十百0-9]"
    r"|\s*Chapter\s+[0-9]"
    r"|\s*([0-9]+\.){1,3}\s"
    r"|\s*[0-9]+\s+[A-Z])",
    re.IGNORECASE,
)
# Matches abi.ir.txt.ALL_CAPS_SHORT verbatim. The TXT parser elevates any such
# single line surrounded by blanks to a level-2 heading.
_ALL_CAPS_SHORT = re.compile(r"^[A-Z][A-Z0-9 \-:,'`]{2,60}$")


def _sanitize_paragraph(text: str) -> str:
    """Collapse newlines and defuse accidental heading patterns."""
    one_line = " ".join(part.strip() for part in text.splitlines() if part.strip())
    if _HEADING_TRIGGER.match(one_line) or _ALL_CAPS_SHORT.match(one_line):
        one_line = "\u200b" + one_line
    return one_line


def materialize_to_book_file(ds: EvalDataset, out_dir: Path) -> Path:
    """Write ``ds`` to a deterministic .txt file so ABI can ingest it.

    Each document becomes a ``Chapter N`` section (matching ABI's heuristic
    chapter detector) and each paragraph is one blank-line-separated block
    of single-line prose. The file path is keyed on ``ds.book_id`` so
    re-running the same spec is a no-op (and ABI's book_id, derived from
    the file's bytes, is also stable across runs).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{ds.book_id}.txt"
    if path.exists():
        return path
    # NOTE: we deliberately do NOT emit a title line — the TXT parser does
    # not recognize ``#``-prefixed lines as headings, so it would become an
    # extra "prose" paragraph and silently break positional alignment with
    # the dataset references. The dataset title lives in ``ds.title`` and in
    # the EvalDataset metadata, not in the materialized file body.
    lines: list[str] = []
    for chapter_n, (doc_id, paras) in enumerate(ds.doc_to_paragraphs.items(), start=1):
        lines.append(f"Chapter {chapter_n}: {doc_id}")
        lines.append("")
        for p in paras:
            lines.append(_sanitize_paragraph(p.source_text))
            lines.append("")
    body = "\n".join(lines).rstrip() + "\n"
    path.write_text(body, encoding="utf-8")
    return path
