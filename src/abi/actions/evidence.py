"""Read-only overlay of one attempt bundle over committed artifact facts."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from abi.project.layout import BookProject
from abi.types.artifact_paths import canonical_artifact_key
from abi.types.orchestration import (
    ArtifactBundle,
    ArtifactRef,
    canonical_bundle_json,
    sha256_canonical_json,
)


class StagingEvidenceView:
    """Expose current attempt outputs plus only ledger-committed canonical dependencies."""

    def __init__(
        self,
        project: BookProject,
        committed_artifacts: tuple[ArtifactRef, ...],
        bundle: ArtifactBundle,
    ) -> None:
        self._project = project
        root_stat = os.stat(project.root, follow_symlinks=False)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError("evidence root must be a real directory")
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        self._staged = {entry.canonical_relpath: entry.staged_relpath for entry in bundle.entries}
        self._committed = {item.relpath: item for item in committed_artifacts}
        self.bundle = bundle
        self.bundle_digest = sha256_canonical_json(canonical_bundle_json(bundle))
        self.artifact_checksums = tuple(
            hashlib.sha256(self._read_project_file(entry.staged_relpath)).hexdigest()
            for entry in bundle.entries
        )

    @classmethod
    def for_bundle(
        cls,
        project: BookProject,
        committed_artifacts: tuple[ArtifactRef, ...],
        bundle: ArtifactBundle,
    ) -> StagingEvidenceView:
        return cls(project, committed_artifacts, bundle)

    def exists(self, canonical_relpath: str) -> bool:
        key = canonical_artifact_key(canonical_relpath)
        relpath = self._staged.get(key, key if key in self._committed else None)
        if relpath is None:
            return False
        try:
            self._read_project_file(relpath)
        except (OSError, ValueError):
            return False
        return True

    def read_bytes(self, canonical_relpath: str) -> bytes:
        key = canonical_artifact_key(canonical_relpath)
        staged = self._staged.get(key)
        if staged is not None:
            content = self._read_project_file(staged)
        elif key in self._committed:
            content = self._read_project_file(key)
            if hashlib.sha256(content).hexdigest() != self._committed[key].sha256:
                raise ValueError(
                    f"committed artifact {key} checksum drifted; block and reconcile the ledger"
                )
        else:
            raise PermissionError(
                f"{key} is neither a current staged output nor a committed artifact dependency"
            )
        return content

    def read_text(self, canonical_relpath: str) -> str:
        return self.read_bytes(canonical_relpath).decode("utf-8")

    def paths(self) -> tuple[str, ...]:
        return tuple((*self._staged, *sorted(set(self._committed) - set(self._staged))))

    def _read_project_file(self, relpath: str) -> bytes:
        parts = _safe_parts(relpath)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(self._project.root, flags | getattr(os, "O_DIRECTORY", 0))
        try:
            root_stat = os.fstat(root_fd)
            if (root_stat.st_dev, root_stat.st_ino) != self._root_identity:
                raise ValueError("evidence root identity changed")
            parent_fd = root_fd
            for part in parts[:-1]:
                next_fd = os.open(
                    part,
                    flags | getattr(os, "O_DIRECTORY", 0),
                    dir_fd=parent_fd,
                )
                if parent_fd != root_fd:
                    os.close(parent_fd)
                parent_fd = next_fd
            try:
                fd = os.open(
                    parts[-1],
                    flags | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=parent_fd,
                )
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError(f"evidence {relpath} must be a regular no-follow file")
                    chunks: list[bytes] = []
                    while chunk := os.read(fd, 1024 * 1024):
                        chunks.append(chunk)
                    return b"".join(chunks)
                finally:
                    os.close(fd)
            finally:
                if parent_fd != root_fd:
                    os.close(parent_fd)
        except OSError as exc:
            raise ValueError(f"evidence {relpath} has an unsafe path component") from exc
        finally:
            os.close(root_fd)


def _safe_parts(relpath: str) -> tuple[str, ...]:
    path = Path(relpath)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("evidence path must stay beneath the pinned project root")
    return path.parts


__all__ = ["StagingEvidenceView"]
