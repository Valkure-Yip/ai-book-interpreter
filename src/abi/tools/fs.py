"""Sandboxed filesystem tools for the agent (read/write/edit/ls/glob/grep)."""

from __future__ import annotations

import re

from langchain_core.tools import BaseTool, StructuredTool

from abi.tools.context import ToolContext

_MAX_READ_CHARS = 60_000


def make_fs_tools(ctx: ToolContext) -> list[BaseTool]:
    def read_file(path: str) -> str:
        """Read a UTF-8 text file inside the project. Path is project-relative."""
        p = ctx.resolve(path)
        if not p.exists():
            return f"ERROR: file not found: {path}"
        if p.is_dir():
            return f"ERROR: {path} is a directory; use list_dir."
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > _MAX_READ_CHARS:
            return text[:_MAX_READ_CHARS] + f"\n\n[...truncated {len(text) - _MAX_READ_CHARS} chars]"
        return text

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a project-relative text file (creates parent dirs)."""
        p = ctx.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        ctx.project.append_log(f"write_file: {path} ({len(content)} chars)")
        return f"wrote {len(content)} chars to {path}"

    def append_file(path: str, content: str) -> str:
        """Append text to a project-relative file (creates it if missing)."""
        p = ctx.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        return f"appended {len(content)} chars to {path}"

    def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace the first occurrence of old_string with new_string in a file."""
        p = ctx.resolve(path)
        if not p.exists():
            return f"ERROR: file not found: {path}"
        text = p.read_text(encoding="utf-8")
        if old_string not in text:
            return f"ERROR: old_string not found in {path}"
        p.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
        return f"edited {path}"

    def list_dir(path: str = ".") -> str:
        """List entries of a project-relative directory."""
        p = ctx.resolve(path)
        if not p.exists():
            return f"ERROR: not found: {path}"
        if p.is_file():
            return path
        entries = []
        for item in sorted(p.iterdir()):
            suffix = "/" if item.is_dir() else ""
            entries.append(item.name + suffix)
        return "\n".join(entries) if entries else "(empty)"

    def glob(pattern: str) -> str:
        """Glob project files, e.g. 'chapters/final/*.md'. Returns relative paths."""
        matches = sorted(
            ctx.project.rel(p) for p in ctx.project.root.glob(pattern) if p.is_file()
        )
        return "\n".join(matches) if matches else "(no matches)"

    def grep(pattern: str, path_glob: str = "**/*.md") -> str:
        """Regex-search project files matching path_glob. Returns 'file:line: text'."""
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"ERROR: bad regex: {exc}"
        out: list[str] = []
        for p in sorted(ctx.project.root.glob(path_glob)):
            if not p.is_file():
                continue
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        out.append(f"{ctx.project.rel(p)}:{i}: {line.strip()[:200]}")
                        if len(out) >= 200:
                            out.append("[...truncated at 200 matches]")
                            return "\n".join(out)
            except Exception:
                continue
        return "\n".join(out) if out else "(no matches)"

    return [
        StructuredTool.from_function(read_file),
        StructuredTool.from_function(write_file),
        StructuredTool.from_function(append_file),
        StructuredTool.from_function(edit_file),
        StructuredTool.from_function(list_dir),
        StructuredTool.from_function(glob),
        StructuredTool.from_function(grep),
    ]
