"""Shared context handed to every tool factory.

Tools are closures over a :class:`ToolContext` so they can read/write inside one
book project and reach the run's shared services (LLM router, agent runtime,
budget, events). The filesystem tools are sandboxed to ``project.root``.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from abi.project.layout import BookProject
from abi.providers.services import RunServices
from abi.tools.permissions import ActionPathPermissions
from abi.types.orchestration import RunSnapshot


@dataclass
class ToolContext:
    project: BookProject
    services: RunServices
    run_id: str
    get_run_snapshot: Callable[[], RunSnapshot]

    def resolve(self, relpath: str) -> Path:
        """Resolve a project-relative path, enforcing the sandbox."""
        candidate = (self.project.root / relpath).resolve()
        if not self.project.within(candidate):
            raise ValueError(
                f"path escapes project sandbox: {relpath!r}. Tools may only read/write "
                f"inside the project root {self.project.root}."
            )
        return candidate

    def authorize_read_path(
        self, relpath: str, permissions: ActionPathPermissions | None
    ) -> Path:
        """Validate one lexical read and reject every existing symlink component."""
        if permissions is not None and not permissions.can_read(relpath):
            raise PermissionError(
                f"this Action is not allowed to read {relpath!r}; use a declared read_set path"
            )
        candidate = self.project.root
        for component in relpath.split("/"):
            candidate = candidate / component
            try:
                mode = candidate.lstat().st_mode
            except FileNotFoundError:
                break
            if stat.S_ISLNK(mode):
                raise PermissionError(
                    f"permissioned read rejects symlink component in {relpath!r}"
                )
        if not self.project.within(candidate):
            raise PermissionError(f"permissioned read escapes project root: {relpath!r}")
        return candidate

    def read_authorized_bytes(
        self, relpath: str, permissions: ActionPathPermissions | None
    ) -> bytes:
        """Read a regular file through pinned dirfds without following symlinks."""
        self.authorize_read_path(relpath, permissions)
        required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
        missing = tuple(name for name in required_flags if not hasattr(os, name))
        if missing:
            raise PermissionError(
                "platform lacks secure no-follow read capability: " + ", ".join(missing)
            )
        directory_flags = (
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        directory_fds: list[int] = []
        file_fd: int | None = None
        try:
            directory_fds.append(os.open(self.project.root, directory_flags))
            components = relpath.split("/")
            for component in components[:-1]:
                next_fd = os.open(
                    component,
                    directory_flags,
                    dir_fd=directory_fds[-1],
                )
                if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                    os.close(next_fd)
                    raise PermissionError(
                        f"permissioned read parent is not a directory: {relpath!r}"
                    )
                directory_fds.append(next_fd)
            file_fd = os.open(components[-1], file_flags, dir_fd=directory_fds[-1])
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise IsADirectoryError(relpath)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_fd, 64 * 1024)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise PermissionError(
                    f"permissioned read rejects symlink or changed path {relpath!r}"
                ) from exc
            raise
        finally:
            if file_fd is not None:
                os.close(file_fd)
            for directory_fd in reversed(directory_fds):
                os.close(directory_fd)

    def list_authorized_directory(
        self, relpath: str, permissions: ActionPathPermissions | None
    ) -> tuple[str, ...]:
        """List one pinned directory without following a swapped path component."""
        self.authorize_read_path(relpath, permissions)
        required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
        missing = tuple(name for name in required_flags if not hasattr(os, name))
        if missing:
            raise PermissionError(
                "platform lacks secure no-follow read capability: " + ", ".join(missing)
            )
        directory_flags = (
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        directory_fds: list[int] = []
        try:
            directory_fds.append(os.open(self.project.root, directory_flags))
            components = () if relpath in {"", "."} else tuple(relpath.split("/"))
            for component in components:
                directory_fds.append(
                    os.open(component, directory_flags, dir_fd=directory_fds[-1])
                )
            entries: list[str] = []
            for name in sorted(os.listdir(directory_fds[-1])):
                mode = os.stat(
                    name,
                    dir_fd=directory_fds[-1],
                    follow_symlinks=False,
                ).st_mode
                entries.append(name + ("/" if stat.S_ISDIR(mode) else ""))
            return tuple(entries)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise PermissionError(
                    f"permissioned read rejects symlink or changed path {relpath!r}"
                ) from exc
            raise
        finally:
            for directory_fd in reversed(directory_fds):
                os.close(directory_fd)
