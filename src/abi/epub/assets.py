"""Asset / figure / table reference check.

Verifies every image referenced from the final chapters or frontmatter resolves
to a file under ``assets/`` (so the builder can put it in the OPF manifest), and
that no reference uses an external hot-link or a local absolute path.
"""

from __future__ import annotations

import re
from pathlib import Path

from abi.epub.result import GateResult
from abi.project.layout import BookProject

_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_HTML_IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']')


def _refs(text: str) -> list[str]:
    return _MD_IMG_RE.findall(text) + _HTML_IMG_RE.findall(text)


def asset_manifest_check(project: BookProject, *, write_report: bool = True) -> GateResult:
    errors: list[str] = []
    warnings: list[str] = []
    referenced: set[str] = set()

    targets: list[Path] = list(project.chapters_final.glob("*.md"))
    fm = project.root / "frontmatter"
    if fm.exists():
        targets += [p for p in fm.rglob("*") if p.is_file() and p.suffix in {".md", ".xhtml", ".html"}]

    for f in targets:
        rel = project.rel(f)
        for ref in _refs(f.read_text(encoding="utf-8", errors="replace")):
            ref = ref.strip().split()[0].strip("<>")  # drop title part / angle brackets
            if ref.startswith(("http://", "https://")):
                errors.append(f"{rel}: external hot-linked image {ref!r} (must be local)")
                continue
            if ref.startswith(("/", "file://")) or re.match(r"^[A-Za-z]:", ref):
                errors.append(f"{rel}: absolute/file image path {ref!r}")
                continue
            referenced.add(ref)
            candidate = (f.parent / ref).resolve()
            alt = (project.root / ref).resolve()
            if not candidate.exists() and not alt.exists():
                errors.append(f"{rel}: image not found: {ref!r}")

    # Warn about assets present but never referenced (dead weight).
    present = {
        project.rel(p)
        for sub in ("assets/figures", "assets/images")
        for p in (project.root / sub).rglob("*")
        if p.is_file()
    }
    if present and not referenced:
        warnings.append("assets present but no chapter references any image")

    ok = not errors
    res = GateResult(
        ok,
        f"asset check: {len(referenced)} references, {len(errors)} errors",
        hard_errors=errors,
        warnings=warnings,
        details={"referenced": sorted(referenced), "present": len(present)},
    )
    if write_report:
        res.write_json(project.asset_manifest_report)
    return res
