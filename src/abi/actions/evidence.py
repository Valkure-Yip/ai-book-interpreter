"""Read-only overlay of one attempt bundle over committed artifact facts."""

from __future__ import annotations

from abi.project.artifacts import sha256_file
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
        self._staged = {entry.canonical_relpath: entry.staged_relpath for entry in bundle.entries}
        self._committed = {item.relpath: item for item in committed_artifacts}
        self.bundle = bundle
        self.bundle_digest = sha256_canonical_json(canonical_bundle_json(bundle))
        self.artifact_checksums = tuple(
            sha256_file(project.root / entry.staged_relpath) for entry in bundle.entries
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
        if key in self._staged:
            return (self._project.root / self._staged[key]).is_file()
        if key in self._committed:
            return (self._project.root / key).is_file()
        return False

    def read_bytes(self, canonical_relpath: str) -> bytes:
        key = canonical_artifact_key(canonical_relpath)
        staged = self._staged.get(key)
        if staged is not None:
            path = self._project.root / staged
        elif key in self._committed:
            path = self._project.root / key
            expected = self._committed[key].sha256
            if sha256_file(path) != expected:
                raise ValueError(
                    f"committed artifact {key} checksum drifted; block and reconcile the ledger"
                )
        else:
            raise PermissionError(
                f"{key} is neither a current staged output nor a committed artifact dependency"
            )
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"evidence {key} must be a regular no-follow file")
        return path.read_bytes()

    def read_text(self, canonical_relpath: str) -> str:
        return self.read_bytes(canonical_relpath).decode("utf-8")

    def paths(self) -> tuple[str, ...]:
        return tuple((*self._staged, *sorted(set(self._committed) - set(self._staged))))


__all__ = ["StagingEvidenceView"]
