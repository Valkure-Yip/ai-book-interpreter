"""Publication-text lint over the final chapters + frontmatter + metadata.

Deterministic hard checks that block the full EPUB build. Mirrors PDBT's
publication lint: no local absolute paths, no mojibake/replacement chars, no BOM,
balanced fences, and target-language typography sanity (for CJK targets).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from abi.epub.result import GateResult
from abi.project.layout import BookProject
from abi.types.tools import GateRuntimeMetadata

_ABS_PATH_RE = re.compile(r"(file://|[A-Za-z]:\\\\|[A-Za-z]:/|/Users/|/home/|/mnt/)")
_REPLACEMENT_CHARS = ("\ufffd", "\x00")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# A CJK char directly followed by an ASCII letter/space then CJK is suspicious
_CJK_SPACE_RE = re.compile(r"[\u4e00-\u9fff] [\u4e00-\u9fff]")


def _check_text(rel: str, text: str, *, cjk_target: bool, errors: list[str], warnings: list[str]) -> None:
    if text.startswith("\ufeff"):
        errors.append(f"{rel}: contains a BOM")
    for ch in _REPLACEMENT_CHARS:
        if ch in text:
            errors.append(f"{rel}: contains replacement/null char (mojibake)")
            break
    for m in _ABS_PATH_RE.finditer(text):
        errors.append(f"{rel}: local absolute path / file:// reference near {m.group(0)!r}")
        break
    if text.count("```") % 2 != 0:
        errors.append(f"{rel}: unbalanced ``` code fences")
    if cjk_target and _CJK_RE.search(text):
        if _CJK_SPACE_RE.search(text):
            warnings.append(f"{rel}: stray space between CJK characters")
        if text.count(";") > max(5, len(text) // 400):
            warnings.append(f"{rel}: heavy semicolon use for a CJK target")


def publication_lint(
    project: BookProject,
    *,
    runtime_metadata: GateRuntimeMetadata | None = None,
) -> GateResult:
    errors: list[str] = []
    warnings: list[str] = []

    meta: dict[str, object] = {}
    if project.book_yaml.exists():
        try:
            loaded = yaml.safe_load(project.book_yaml.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise TypeError("top-level metadata must be a mapping")
            meta = {str(key): value for key, value in loaded.items()}
        except Exception as exc:
            errors.append(f"metadata/book.yaml: invalid YAML ({exc})")
    configured_language = runtime_metadata.target_language if runtime_metadata else ""
    lang = str((meta or {}).get("language") or configured_language)
    cjk_target = lang.startswith(("zh", "ja", "ko"))

    for required in ("title", "language"):
        if not (meta or {}).get(required):
            errors.append(f"metadata/book.yaml: missing required field '{required}'")

    final_files = sorted(project.chapters_final.glob("*.md"))
    if not final_files:
        errors.append("chapters/final/ is empty")
    targets: list[Path] = list(final_files)
    fm = project.root / "frontmatter"
    if fm.exists():
        targets += [p for p in fm.rglob("*") if p.is_file() and p.suffix in {".md", ".xhtml", ".html"}]

    for f in targets:
        rel = project.rel(f)
        text = f.read_text(encoding="utf-8", errors="replace")
        _check_text(rel, text, cjk_target=cjk_target, errors=errors, warnings=warnings)

    ok = not errors
    res = GateResult(
        ok,
        f"publication lint over {len(targets)} files: "
        f"{len(errors)} errors, {len(warnings)} warnings",
        hard_errors=errors,
        warnings=warnings,
        details={"files": len(targets), "language": lang},
    )
    res.write_json(project.publication_lint_report)
    return res
