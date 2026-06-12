"""Extract reader-visible audit units from final chapters for stratified sampling.

Strata mirror ``references/stratified_random_spotcheck.md``:
paragraph / table / figure / formula / caption_note.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from abi.project.layout import BookProject

Stratum = str  # "paragraph" | "table" | "figure" | "formula" | "caption_note"

_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_FORMULA_BLOCK_RE = re.compile(r"\$\$.+?\$\$", re.DOTALL)


@dataclass(frozen=True)
class AuditUnit:
    unit_id: str
    chapter: str
    stratum: Stratum
    text: str


def _blocks(md: str) -> list[str]:
    return [b.strip() for b in re.split(r"\n\s*\n", md) if b.strip()]


def _classify(block: str) -> Stratum:
    lines = block.splitlines()
    if block.startswith("$$") or _FORMULA_BLOCK_RE.search(block):
        return "formula"
    if len(lines) >= 2 and lines[0].lstrip().startswith("|") and set(lines[1].replace("|", "").strip()) <= set("-: "):
        return "table"
    if _IMG_RE.search(block):
        return "figure"
    stripped = block.lstrip()
    if re.match(r"^(图|表|Figure|Table|Fig\.)\s*[0-9]", stripped) or stripped.startswith("[^"):
        return "caption_note"
    return "paragraph"


def extract_units(project: BookProject) -> list[AuditUnit]:
    units: list[AuditUnit] = []
    for f in sorted(project.chapters_final.glob("*.md")):
        chapter = f.stem
        for i, block in enumerate(_blocks(f.read_text(encoding="utf-8"))):
            if block.startswith("#"):
                continue  # headings are checked under title policy, not sampled
            stratum = _classify(block)
            units.append(
                AuditUnit(
                    unit_id=f"{chapter}#{i:04d}",
                    chapter=chapter,
                    stratum=stratum,
                    text=block,
                )
            )
    return units


def stratify(units: list[AuditUnit]) -> dict[Stratum, list[AuditUnit]]:
    out: dict[Stratum, list[AuditUnit]] = {
        "paragraph": [], "table": [], "figure": [], "formula": [], "caption_note": []
    }
    for u in units:
        out[u.stratum].append(u)
    return out
