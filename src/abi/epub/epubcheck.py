"""EPUBCheck gate.

Resolution order (first available wins):
1. ``ABI_EPUBCHECK_JAR`` env var -> ``java -jar <jar>``
2. ``epubcheck`` Python package (the ``epub`` extra) -> bundled jar
3. ``epubcheck`` executable on PATH

All require a JRE (``java``) except where the package ships its own launcher.
If none is available the gate returns a FAIL whose message explains how to
install it — it never silently passes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from abi.epub.result import GateResult
from abi.project.layout import BookProject


def _find_java() -> str | None:
    """Locate a real JRE. Mirrors PDBT's discovery so an existing bundled JRE
    (e.g. ``public-domain-books-translation/books/tools/zulu17-jre``) is reused.

    Order: ABI_JAVA / LIFEBOOK_JAVA env -> JAVA_HOME/bin/java -> ``java`` on PATH.
    """
    for env_var in ("ABI_JAVA", "LIFEBOOK_JAVA"):
        cand = os.environ.get(env_var)
        if cand and Path(cand).exists():
            return cand
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        for name in ("java", "java.exe"):
            cand_path = Path(java_home) / "bin" / name
            if cand_path.exists():
                return str(cand_path)
    return shutil.which("java")


def _jar_from_package() -> str | None:
    try:
        from epubcheck.const import EPUBCHECK  # type: ignore

        if EPUBCHECK and Path(EPUBCHECK).exists():
            return str(EPUBCHECK)
    except Exception:
        return None
    return None


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        return proc.returncode, (proc.stdout + "\n" + proc.stderr)
    except FileNotFoundError as exc:
        return 127, f"command not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, "epubcheck timed out"


def run_epubcheck(project: BookProject, epub_path: Path) -> GateResult:
    if not epub_path.exists():
        return _fail(project, f"EPUB not found: {project.rel(epub_path)}")

    java = _find_java()
    jar = os.environ.get("ABI_EPUBCHECK_JAR") or _jar_from_package()
    cli = shutil.which("epubcheck")

    json_out = project.epubcheck_log
    json_out.parent.mkdir(parents=True, exist_ok=True)

    if jar and java:
        code, output = _run([java, "-jar", jar, "--json", str(json_out), str(epub_path)])
    elif cli:
        code, output = _run([cli, "--json", str(json_out), str(epub_path)])
    else:
        return _fail(
            project,
            "EPUBCheck not available. Install a JRE plus the 'epub' extra "
            "(`pip install ai-book-interpreter[epub]`), or set ABI_EPUBCHECK_JAR "
            "to an epubcheck.jar, or put `epubcheck` on PATH. To reuse an existing "
            "JRE, set JAVA_HOME or ABI_JAVA to its java binary.",
        )

    fatal, errors, warnings = _parse_report(json_out, output)
    ok = code == 0 and fatal == 0 and errors == 0
    res = GateResult(
        ok,
        f"EPUBCheck: {fatal} fatal, {errors} errors, {warnings} warnings (exit {code})",
        hard_errors=([] if ok else _extract_messages(json_out, output)),
        details={"fatal": fatal, "errors": errors, "warnings": warnings, "exit_code": code},
    )
    res.write_json(project.root / "output/epubcheck_summary.json")
    return res


def _parse_report(json_out: Path, output: str) -> tuple[int, int, int]:
    if json_out.exists():
        try:
            data = json.loads(json_out.read_text(encoding="utf-8"))
            checker = data.get("checker", {})
            return (
                int(checker.get("nFatal", 0)),
                int(checker.get("nError", 0)),
                int(checker.get("nWarning", 0)),
            )
        except Exception:
            pass
    # Fallback: scrape text output.
    fatal = output.count("FATAL")
    errors = output.lower().count("error") - output.lower().count("0 error")
    return max(0, fatal), max(0, errors), output.lower().count("warning")


def _extract_messages(json_out: Path, output: str) -> list[str]:
    msgs: list[str] = []
    if json_out.exists():
        try:
            data = json.loads(json_out.read_text(encoding="utf-8"))
            for m in data.get("messages", []):
                if m.get("severity") in {"FATAL", "ERROR"}:
                    msgs.append(f"{m.get('ID')}: {m.get('message')}")
        except Exception:
            pass
    if not msgs:
        msgs = [ln for ln in output.splitlines() if "ERROR" in ln or "FATAL" in ln][:30]
    if not msgs:
        # No structured messages (e.g. the JVM itself failed to start). Surface
        # the raw tail so the agent/user can see why.
        tail = [ln for ln in output.splitlines() if ln.strip()][-8:]
        msgs = tail or ["epubcheck failed with a non-zero exit and no diagnostics"]
    return msgs[:30]


def _fail(project: BookProject, message: str) -> GateResult:
    res = GateResult(False, message)
    res.write_json(project.root / "output/epubcheck_summary.json")
    return res
