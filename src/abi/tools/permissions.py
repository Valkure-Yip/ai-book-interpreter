"""Fail-closed filesystem permissions applied at Action tool handlers."""

from __future__ import annotations

from abi.project.artifact_paths import canonical_artifact_key
from abi.types._base import FrozenModel


class ActionPathPermissions(FrozenModel):
    """Exact canonical files and directory roots visible to one Action."""

    read_files: tuple[str, ...] = ()
    read_dirs: tuple[str, ...] = ()
    write_files: tuple[str, ...] = ()
    write_dirs: tuple[str, ...] = ()

    def can_read(self, relpath: str) -> bool:
        return self._allows(
            relpath,
            files=(*self.read_files, *self.write_files),
            directories=(*self.read_dirs, *self.write_dirs),
        )

    def can_write(self, relpath: str) -> bool:
        return self._allows(relpath, files=self.write_files, directories=self.write_dirs)

    @staticmethod
    def _allows(
        relpath: str,
        *,
        files: tuple[str, ...],
        directories: tuple[str, ...],
    ) -> bool:
        try:
            candidate = canonical_artifact_key(relpath)
        except ValueError:
            return False
        if candidate in files:
            return True
        return any(
            candidate == directory or candidate.startswith(f"{directory}/")
            for directory in directories
        )
