"""Crash-reconcilable promotion from attempt staging to canonical artifacts."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from abi.project.layout import BookProject
from abi.project.run_ledger import PromotionIntent, RunLedger

__all__ = [
    "ArtifactConflictError",
    "ArtifactStore",
    "InjectedCrash",
    "PromotionIntent",
    "sha256_file",
]


class ArtifactConflictError(RuntimeError):
    """Raised when a promotion would overwrite a different canonical artifact."""


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
        return path

    def _staging_dir_path(self, action_id: str, attempt: int) -> Path:
        """Validate and calculate an attempt staging path without creating it."""
        if not _is_safe_component(action_id):
            raise ValueError("action_id must be a safe path component")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        return self._project.staging_root / action_id / str(attempt)

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
        staging_dir = self._staging_dir_path(action_id, attempt).resolve()
        try:
            staged_path.relative_to(staging_dir)
        except ValueError as exc:
            raise ValueError("staged artifact must be inside its attempt staging directory") from exc
        self._path_for(canonical_relpath)
        if not staged_path.is_file():
            raise FileNotFoundError(f"staged artifact {staged_relpath} does not exist")
        return await self._require_ledger().create_promotion_intent(
            action_id=action_id,
            attempt=attempt,
            staged_relpath=staged_relpath,
            canonical_relpath=canonical_relpath,
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
        """Reconcile every durable intent, including cleanup after committed recovery."""
        return tuple(
            [await self.reconcile_intent(intent) for intent in await self._require_ledger().promotion_intents()]
        )

    async def _complete(
        self, intent: PromotionIntent, *, crash_after: str | None = None
    ) -> PromotionIntent:
        staged = self._path_for(intent.staged_relpath)
        canonical = self._path_for(intent.canonical_relpath)
        staged_exists = staged.is_file()
        canonical_exists = canonical.is_file()

        if canonical_exists:
            if sha256_file(canonical) != intent.checksum:
                await self._record_checksum_conflict(intent)
                raise ArtifactConflictError(
                    f"canonical artifact {intent.canonical_relpath} has a different checksum; "
                    "inspect and choose the canonical artifact"
                )
            if staged_exists and sha256_file(staged) != intent.checksum:
                await self._record_checksum_conflict(intent)
                raise ArtifactConflictError(
                    f"staged artifact {intent.staged_relpath} has a different checksum; "
                    "inspect and choose the canonical artifact"
                )
            committed = await self._require_ledger().commit_promotion_intent(intent.intent_id)
            self._clean_staged_file(committed)
            return committed

        if not staged_exists:
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
            await self._record_checksum_conflict(intent)
            raise ArtifactConflictError(
                f"staged artifact {intent.staged_relpath} no longer matches its promotion intent; "
                "inspect and choose the canonical artifact"
            )

        canonical.parent.mkdir(parents=True, exist_ok=True)
        staged.replace(canonical)
        if crash_after == "after_rename":
            raise InjectedCrash("injected crash after artifact rename")
        committed = await self._require_ledger().commit_promotion_intent(intent.intent_id)
        self._clean_staged_file(committed)
        return committed

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

    def _require_ledger(self) -> RunLedger:
        if self._ledger is None:
            raise RuntimeError("a RunLedger is required to prepare or promote artifacts")
        return self._ledger


def _is_safe_component(value: str) -> bool:
    return bool(value) and Path(value).name == value and value not in {".", ".."}
