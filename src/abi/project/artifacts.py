"""Crash-reconcilable promotion from attempt staging to canonical artifacts."""

from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path

from abi.project.layout import BookProject
from abi.project.run_ledger import PromotionIntent, RunLedger

__all__ = [
    "ArtifactConflictError",
    "ArtifactReconciliationError",
    "ArtifactStore",
    "InjectedCrash",
    "PromotionIntent",
    "sha256_file",
]


class ArtifactConflictError(RuntimeError):
    """Raised when a promotion would overwrite a different canonical artifact."""


class ArtifactReconciliationError(ArtifactConflictError):
    """Raised after reconciliation has recorded and continued through conflicts."""

    def __init__(self, intent_ids: tuple[str, ...]) -> None:
        self.intent_ids = intent_ids
        noun = "conflict" if len(intent_ids) == 1 else "conflicts"
        super().__init__(
            f"{len(intent_ids)} promotion {noun} require review; inspect and choose the canonical artifact"
        )


class InjectedCrash(RuntimeError):
    """Test-only failure raised at a durable promotion crash boundary."""


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a regular artifact file."""
    digest = sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactStore:
    """Promote validated attempt artifacts without claiming cross-medium atomicity."""

    def __init__(self, project: BookProject, ledger: RunLedger | None) -> None:
        self._project = project
        self._ledger = ledger

    def staging_dir(self, action_id: str, attempt: int) -> Path:
        """Return and create the one safe staging directory for an action attempt."""
        path = self._staging_dir_path(action_id, attempt)
        path.mkdir(parents=True, exist_ok=True)
        return self._staging_dir_path(action_id, attempt)

    def _staging_dir_path(self, action_id: str, attempt: int) -> Path:
        """Validate and calculate an attempt staging path without creating it."""
        if not _is_safe_component(action_id):
            raise ValueError("action_id must be a safe path component")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        staging_root = self._resolved_staging_root()
        candidate = self._project.staging_root / action_id / str(attempt)
        self._reject_symlink_components(candidate)
        resolved_candidate = candidate.resolve()
        try:
            resolved_candidate.relative_to(staging_root)
        except ValueError as exc:
            raise ValueError("attempt staging directory escapes the resolved staging root") from exc
        return resolved_candidate

    async def prepare_promotion(
        self,
        *,
        action_id: str,
        attempt: int,
        staged_relpath: str,
        canonical_relpath: str,
        media_type: str,
    ) -> PromotionIntent:
        """Persist a checksum-bearing intent before any canonical path is changed."""
        staged_path = self._path_for(staged_relpath)
        staging_dir = self._staging_dir_path(action_id, attempt)
        try:
            staged_path.relative_to(staging_dir)
        except ValueError as exc:
            raise ValueError("staged artifact must be inside its attempt staging directory") from exc
        canonical_path = self._path_for(canonical_relpath)
        try:
            canonical_path.relative_to(self._resolved_staging_root())
        except ValueError:
            pass
        else:
            raise ValueError("canonical artifact must be outside the staging root")
        if canonical_path == staged_path:
            raise ValueError("canonical artifact may not alias its staged source")
        if not staged_path.is_file():
            raise FileNotFoundError(f"staged artifact {staged_relpath} does not exist")
        return await self._require_ledger().create_promotion_intent(
            action_id=action_id,
            attempt=attempt,
            staged_relpath=self._project_relative(staged_path),
            canonical_relpath=self._project_relative(canonical_path),
            checksum=sha256_file(staged_path),
            media_type=media_type,
        )

    async def promote(
        self, intent: PromotionIntent, *, crash_after: str | None = None
    ) -> PromotionIntent:
        """Complete a promotion, with test-only injection immediately after durable boundaries."""
        durable_intent = await self._require_ledger().create_promotion_intent(
            action_id=intent.action_id,
            attempt=intent.attempt,
            staged_relpath=intent.staged_relpath,
            canonical_relpath=intent.canonical_relpath,
            checksum=intent.checksum,
            media_type=intent.media_type,
        )
        if crash_after == "after_intent":
            raise InjectedCrash("injected crash after promotion intent")
        return await self._complete(durable_intent, crash_after=crash_after)

    async def reconcile_intent(self, intent: PromotionIntent | str) -> PromotionIntent:
        """Finish one intent based only on durable state and the two artifact paths."""
        durable_intent = (
            await self._require_ledger().get_promotion_intent(intent)
            if isinstance(intent, str)
            else await self._require_ledger().get_promotion_intent(intent.intent_id)
        )
        return await self._complete(durable_intent)

    async def reconcile_all(self) -> tuple[PromotionIntent, ...]:
        """Reconcile every intent, raising only after every conflict has been recorded."""
        reconciled: list[PromotionIntent] = []
        conflicts: list[str] = []
        for intent in await self._require_ledger().promotion_intents():
            try:
                reconciled.append(await self.reconcile_intent(intent))
            except ArtifactConflictError:
                conflicts.append(intent.intent_id)
        if conflicts:
            raise ArtifactReconciliationError(tuple(conflicts))
        return tuple(reconciled)

    async def _complete(
        self, intent: PromotionIntent, *, crash_after: str | None = None
    ) -> PromotionIntent:
        staged = self._path_for(intent.staged_relpath)
        canonical = self._path_for(intent.canonical_relpath)
        if not staged.is_file():
            if canonical.is_file():
                return await self._commit_existing(intent, staged, canonical)
            await self._require_ledger().record_promotion_incident(
                intent.intent_id,
                error_code="artifact_promotion_missing",
                message=(
                    f"neither staged nor canonical artifact exists for {intent.canonical_relpath}; "
                    "restore the artifact or rerun the action"
                ),
            )
            return intent

        if sha256_file(staged) != intent.checksum:
            await self._raise_checksum_conflict(intent, "staged artifact no longer matches its promotion intent")

        canonical.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(staged, canonical)
        except FileExistsError:
            return await self._commit_existing(intent, staged, canonical)
        if crash_after == "after_rename":
            raise InjectedCrash("injected crash after artifact rename")
        committed = await self._require_ledger().commit_promotion_intent(intent.intent_id)
        self._clean_staged_file(committed)
        return committed

    async def _commit_existing(
        self, intent: PromotionIntent, staged: Path, canonical: Path
    ) -> PromotionIntent:
        """Commit an already-created canonical file only when both checksums agree."""
        if not canonical.is_file() or sha256_file(canonical) != intent.checksum:
            await self._raise_checksum_conflict(intent, "canonical artifact has a different checksum")
        if staged.is_file() and sha256_file(staged) != intent.checksum:
            await self._raise_checksum_conflict(intent, "staged artifact has a different checksum")
        committed = await self._require_ledger().commit_promotion_intent(intent.intent_id)
        self._clean_staged_file(committed)
        return committed

    async def _raise_checksum_conflict(self, intent: PromotionIntent, detail: str) -> None:
        await self._record_checksum_conflict(intent)
        raise ArtifactConflictError(
            f"{detail} for {intent.canonical_relpath}; inspect and choose the canonical artifact"
        )

    async def _record_checksum_conflict(self, intent: PromotionIntent) -> None:
        await self._require_ledger().record_promotion_incident(
            intent.intent_id,
            error_code="artifact_checksum_conflict",
            message=(
                f"artifact checksums conflict for {intent.canonical_relpath}; "
                "inspect and choose the canonical artifact"
            ),
        )

    def _clean_staged_file(self, intent: PromotionIntent) -> None:
        """Remove only a matching staged duplicate after its durable commit."""
        if intent.status != "COMMITTED":
            return
        staged = self._path_for(intent.staged_relpath)
        canonical = self._path_for(intent.canonical_relpath)
        if not staged.is_file() or not canonical.is_file():
            return
        if sha256_file(staged) != intent.checksum or sha256_file(canonical) != intent.checksum:
            return
        staged.unlink()
        staging_dir = self.staging_dir(intent.action_id, intent.attempt)
        parent = staged.parent
        while parent != staging_dir.parent:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def _path_for(self, relpath: str) -> Path:
        candidate = Path(relpath)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("artifact path must be project-relative and may not escape the project")
        path = (self._project.root / candidate).resolve()
        if not self._project.within(path):
            raise ValueError("artifact path must stay inside the project")
        return path

    def _resolved_staging_root(self) -> Path:
        root = self._project.staging_root.resolve()
        try:
            root.relative_to(self._project.root.resolve())
        except ValueError as exc:
            raise ValueError("staging root must stay inside the project") from exc
        return root

    def _reject_symlink_components(self, candidate: Path) -> None:
        """Reject existing symlink components before creating or using staging paths."""
        relative = candidate.relative_to(self._project.root)
        current = self._project.root
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                raise ValueError("attempt staging directory may not traverse a symlink")

    def _project_relative(self, path: Path) -> str:
        return path.relative_to(self._project.root.resolve()).as_posix()

    def _require_ledger(self) -> RunLedger:
        if self._ledger is None:
            raise RuntimeError("a RunLedger is required to prepare or promote artifacts")
        return self._ledger


def _is_safe_component(value: str) -> bool:
    return bool(value) and Path(value).name == value and value not in {".", ".."}
