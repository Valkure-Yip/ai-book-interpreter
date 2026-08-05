"""Sandboxed filesystem tools for the agent (read/write/edit/ls/glob/grep)."""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Mapping

from pydantic import Field

from abi.project.artifacts import AttemptStagingWriter, BufferedAttemptWriter
from abi.tools.context import ToolContext
from abi.tools.permissions import ActionPathPermissions
from abi.types._base import FrozenModel
from abi.types.orchestration import ExpectedArtifact
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


def make_fs_tools(
    ctx: ToolContext,
    *,
    permissions: ActionPathPermissions | None = None,
    writer: AttemptStagingWriter | BufferedAttemptWriter | None = None,
    expected_artifacts: Mapping[str, ExpectedArtifact] | None = None,
) -> list[ToolBinding]:
    def require_read(path: str) -> None:
        if permissions is not None and not permissions.can_read(path):
            raise PermissionError(
                f"this Action is not allowed to read {path!r}; use a declared read_set path"
            )

    def require_write(path: str) -> None:
        if permissions is not None and not permissions.can_write(path):
            raise PermissionError(
                f"this Action is not allowed to write {path!r}; use a declared write_set path"
            )

    def safe_glob(pattern: str) -> bool:
        parts = pattern.replace("\\", "/").split("/")
        return bool(pattern) and not pattern.startswith("/") and ".." not in parts

    def read_file(path: str) -> str:
        """Read a UTF-8 text file inside the project. Path is project-relative."""
        require_read(path)
        if writer is not None and path in {entry.canonical_relpath for entry in writer.entries}:
            return writer.read_bytes(path).decode("utf-8", errors="replace")
        try:
            data = ctx.read_authorized_bytes(path, permissions)
        except FileNotFoundError:
            return f"ERROR: file not found: {path}"
        except IsADirectoryError:
            return f"ERROR: {path} is a directory; use list_dir."
        text = data.decode("utf-8", errors="replace")
        if len(text) > _MAX_READ_CHARS:
            return (
                text[:_MAX_READ_CHARS] + f"\n\n[...truncated {len(text) - _MAX_READ_CHARS} chars]"
            )
        return text

    def write_file(path: str, content: str) -> str:
        """Create one current-attempt staged output using its logical canonical path."""
        require_write(path)
        if writer is None:
            raise PermissionError(
                "write_file requires an AttemptStagingWriter; dispatch this tool inside an authorized attempt"
            )
        expected = None if expected_artifacts is None else expected_artifacts.get(path)
        writer.write_text(
            path,
            content,
            media_type=(
                expected.media_type
                if expected is not None
                else (mimetypes.guess_type(path)[0] or "text/plain")
            ),
            evidence_role=expected.evidence_role if expected is not None else "action_output",
            metadata=expected.metadata if expected is not None else (),
        )
        return f"wrote {len(content)} chars to {path}"

    def append_file(path: str, content: str) -> str:
        """Reject non-create-only writes; submit complete content once."""
        require_write(path)
        raise PermissionError(
            "append_file is unavailable for attempt outputs; write the complete artifact once"
        )

    def edit_file(path: str, old_string: str, new_string: str) -> str:
        """Replace the first occurrence of old_string with new_string in a file."""
        require_read(path)
        require_write(path)
        raise PermissionError(
            "edit_file cannot mutate committed or staged output in place; write one replacement artifact in a new attempt"
        )

    def list_dir(path: str = ".") -> str:
        """List entries of a project-relative directory."""
        if permissions is not None:
            normalized = path.rstrip("/")
            visible_roots = (*permissions.read_dirs, *permissions.write_dirs)
            if normalized not in visible_roots:
                raise PermissionError(
                    f"this Action is not allowed to list {path!r}; use a declared read_set path"
                )
        try:
            entries = ctx.list_authorized_directory(path, permissions)
        except FileNotFoundError:
            return f"ERROR: not found: {path}"
        except NotADirectoryError:
            return f"ERROR: {path} is not a directory"
        return "\n".join(entries) if entries else "(empty)"

    def glob(pattern: str) -> str:
        """Glob project files, e.g. 'chapters/final/*.md'. Returns relative paths."""
        if not safe_glob(pattern):
            raise PermissionError(
                f"this Action is not allowed to glob {pattern!r}; use a project-relative pattern"
            )
        matches = []
        for p in sorted(ctx.project.root.glob(pattern)):
            relpath = ctx.project.rel(p)
            if permissions is not None and not permissions.can_read(relpath):
                continue
            authorized = ctx.authorize_read_path(relpath, permissions)
            if authorized.is_file():
                matches.append(relpath)
        return "\n".join(matches) if matches else "(no matches)"

    def grep(pattern: str, path_glob: str = "**/*.md") -> str:
        """Regex-search project files matching path_glob. Returns 'file:line: text'."""
        if not safe_glob(path_glob):
            raise PermissionError(
                f"this Action is not allowed to grep {path_glob!r}; use a project-relative pattern"
            )
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"ERROR: bad regex: {exc}"
        out: list[str] = []
        for p in sorted(ctx.project.root.glob(path_glob)):
            if not p.is_file():
                continue
            relpath = ctx.project.rel(p)
            if permissions is not None and not permissions.can_read(relpath):
                continue
            try:
                text = ctx.read_authorized_bytes(relpath, permissions).decode(
                    "utf-8", errors="replace"
                )
                for i, line in enumerate(text.splitlines(), 1):
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
