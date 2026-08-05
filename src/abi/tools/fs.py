"""Sandboxed filesystem tools for the agent (read/write/edit/ls/glob/grep)."""

from __future__ import annotations

import re

from pydantic import Field

from abi.tools.context import ToolContext
from abi.types._base import FrozenModel
from abi.types.tools import ToolBinding

_MAX_READ_CHARS = 60_000


class ReadFileInput(FrozenModel):
    path: str = Field(description="Project-relative UTF-8 text file path.")


class WriteFileInput(FrozenModel):
    path: str = Field(description="Project-relative output file path.")
    content: str = Field(description="Complete UTF-8 file content.")


class AppendFileInput(FrozenModel):
    path: str = Field(description="Project-relative output file path.")
    content: str = Field(description="UTF-8 text to append.")


class EditFileInput(FrozenModel):
    path: str = Field(description="Project-relative UTF-8 text file path.")
    old_string: str = Field(description="Exact first occurrence to replace.")
    new_string: str = Field(description="Replacement text.")


class ListDirInput(FrozenModel):
    path: str = Field(default=".", description="Project-relative directory path.")


class GlobInput(FrozenModel):
    pattern: str = Field(description="Project-relative glob pattern.")


class GrepInput(FrozenModel):
    pattern: str = Field(description="Regular expression to search for.")
    path_glob: str = Field(default="**/*.md", description="Files to search.")


def make_fs_tools(ctx: ToolContext) -> list[ToolBinding]:
    def read_file(path: str) -> str:
        """Read a UTF-8 text file inside the project. Path is project-relative."""
        p = ctx.resolve(path)
        if not p.exists():
            return f"ERROR: file not found: {path}"
        if p.is_dir():
            return f"ERROR: {path} is a directory; use list_dir."
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > _MAX_READ_CHARS:
            return (
                text[:_MAX_READ_CHARS] + f"\n\n[...truncated {len(text) - _MAX_READ_CHARS} chars]"
            )
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
        matches = sorted(ctx.project.rel(p) for p in ctx.project.root.glob(pattern) if p.is_file())
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
                for i, line in enumerate(
                    p.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if rx.search(line):
                        out.append(f"{ctx.project.rel(p)}:{i}: {line.strip()[:200]}")
                        if len(out) >= 200:
                            out.append("[...truncated at 200 matches]")
                            return "\n".join(out)
            except Exception:
                continue
        return "\n".join(out) if out else "(no matches)"

    return [
        ToolBinding("read_file", read_file.__doc__ or "Read a file.", ReadFileInput, read_file),
        ToolBinding(
            "write_file", write_file.__doc__ or "Write a file.", WriteFileInput, write_file
        ),
        ToolBinding(
            "append_file", append_file.__doc__ or "Append a file.", AppendFileInput, append_file
        ),
        ToolBinding("edit_file", edit_file.__doc__ or "Edit a file.", EditFileInput, edit_file),
        ToolBinding("list_dir", list_dir.__doc__ or "List a directory.", ListDirInput, list_dir),
        ToolBinding("glob", glob.__doc__ or "Glob files.", GlobInput, glob),
        ToolBinding("grep", grep.__doc__ or "Search files.", GrepInput, grep),
    ]
