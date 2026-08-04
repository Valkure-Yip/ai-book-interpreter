"""Crash-reconcilable promotion from attempt staging to canonical artifacts."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from contextlib import suppress
from hashlib import sha256
from pathlib import Path, PurePath
from uuid import uuid4

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

TestHook = Callable[[str, PromotionIntent | None], None]


class ArtifactConflictError(RuntimeError):
    """Raised when a promotion would overwrite a different canonical artifact."""


class ArtifactReconciliationError(ArtifactConflictError):
    """Raised after reconciliation has recorded and continued through conflicts."""

    def __init__(self, intent_ids: tuple[str, ...]) -> None:
        self.intent_ids = intent_ids
        noun = "conflict" if len(intent_ids) == 1 else "conflicts"
        super().__init__(
            f"{len(intent_ids)} promotion {noun} require review or repair; "
            "inspect and choose the canonical artifact"
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
    """Promote immutable copies without claiming filesystem/SQLite atomicity."""

    def __init__(
        self, project: BookProject, ledger: RunLedger | None, *, test_hook: TestHook | None = None
    ) -> None:
        self._project = project
        self._ledger = ledger
        self._test_hook = test_hook

    def staging_dir(self, action_id: str, attempt: int) -> Path:
        """Create one attempt directory using no-follow directory descriptors."""
        _require_secure_dirfd_support()
        self._invoke_test_hook("before_staging_mkdir", None)
        fd = self._open_staging_attempt_dir(action_id, attempt, create=True)
        os.close(fd)
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
        """Persist a normalized, checksum-bearing intent before canonical mutation."""
        _require_secure_dirfd_support()
        staged_parts = self._staged_file_parts(action_id, attempt, staged_relpath)
        canonical = self._canonical_path(canonical_relpath)
        checksum = self._staged_checksum(action_id, attempt, staged_parts)
        if checksum is None:
            raise FileNotFoundError(f"staged artifact {staged_relpath} does not exist")
        return await self._require_ledger().create_promotion_intent(
            action_id=action_id,
            attempt=attempt,
            staged_relpath=self._normalized_staged_relpath(action_id, attempt, staged_parts),
            canonical_relpath=self._project_relative(canonical),
            checksum=checksum,
            media_type=media_type,
        )

    async def promote(
        self, intent: PromotionIntent, *, crash_after: str | None = None
    ) -> PromotionIntent:
        """Complete an already-durable promotion intent."""
        durable_intent = await self._require_ledger().get_promotion_intent(intent.intent_id)
        if crash_after == "after_intent":
            raise InjectedCrash("injected crash after promotion intent")
        return await self._complete(durable_intent, crash_after=crash_after)

    async def reconcile_intent(self, intent: PromotionIntent | str) -> PromotionIntent:
        """Finish one intent based only on durable state and validated artifact paths."""
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
        _require_secure_dirfd_support()
        try:
            staged_parts = self._staged_file_parts(intent.action_id, intent.attempt, intent.staged_relpath)
            canonical = self._canonical_path(intent.canonical_relpath)
        except ValueError as exc:
            await self._record_invalid_intent(intent, str(exc))
            raise ArtifactConflictError(f"invalid promotion intent requires ledger repair: {exc}") from exc

        staged_checksum = await self._validated_staged_checksum(intent, staged_parts)
        if staged_checksum is None:
            if canonical.is_file():
                return await self._commit_existing(intent, staged_parts, canonical)
            await self._require_ledger().record_promotion_incident(
                intent.intent_id,
                error_code="artifact_promotion_missing",
                message=(
                    f"neither staged nor canonical artifact exists for {intent.canonical_relpath}; "
                    "restore the artifact or rerun the action"
                ),
            )
            return intent

        if staged_checksum != intent.checksum:
            await self._raise_checksum_conflict(intent, "staged artifact no longer matches its promotion intent")

        self._invoke_test_hook("after_staged_verification", intent)
        try:
            temp = self._materialize_immutable_copy(intent, staged_parts, canonical)
        except ValueError as exc:
            await self._record_invalid_intent(intent, str(exc))
            raise ArtifactConflictError(f"invalid promotion intent requires ledger repair: {exc}") from exc
        try:
            if sha256_file(temp) != intent.checksum:
                await self._raise_checksum_conflict(intent, "immutable promotion copy has a different checksum")
            canonical.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(temp, canonical)
            except FileExistsError:
                return await self._commit_existing(intent, staged_parts, canonical)
            if crash_after == "after_rename":
                raise InjectedCrash("injected crash after artifact install")
            if sha256_file(canonical) != intent.checksum:
                await self._raise_checksum_conflict(intent, "installed canonical artifact has a different checksum")
            committed = await self._require_ledger().commit_promotion_intent(intent.intent_id)
            if sha256_file(canonical) != intent.checksum:
                await self._raise_checksum_conflict(intent, "committed canonical artifact has a different checksum")
            await self._clean_staged_file(committed, staged_parts, canonical)
            return committed
        finally:
            if temp.exists():
                temp.unlink()

    async def _commit_existing(
        self, intent: PromotionIntent, staged_parts: tuple[str, ...], canonical: Path
    ) -> PromotionIntent:
        """Commit a pre-existing canonical file only when all observed bytes agree."""
        if not canonical.is_file() or sha256_file(canonical) != intent.checksum:
            await self._raise_checksum_conflict(intent, "canonical artifact has a different checksum")
        staged_checksum = await self._validated_staged_checksum(intent, staged_parts)
        if staged_checksum is not None and staged_checksum != intent.checksum:
            await self._raise_checksum_conflict(intent, "staged artifact has a different checksum")
        committed = await self._require_ledger().commit_promotion_intent(intent.intent_id)
        if sha256_file(canonical) != intent.checksum:
            await self._raise_checksum_conflict(intent, "committed canonical artifact has a different checksum")
        await self._clean_staged_file(committed, staged_parts, canonical)
        return committed

    def _materialize_immutable_copy(
        self, intent: PromotionIntent, staged_parts: tuple[str, ...], canonical: Path
    ) -> Path:
        """Copy through a no-follow staging descriptor into a private same-filesystem file."""
        source_fd = self._open_staged_file(intent.action_id, intent.attempt, staged_parts)
        if source_fd is None:
            raise FileNotFoundError(f"staged artifact {intent.staged_relpath} disappeared during promotion")
        canonical.parent.mkdir(parents=True, exist_ok=True)
        temp = canonical.parent / f".abi-promotion-{intent.intent_id}-{uuid4().hex}.tmp"
        try:
            destination_fd = os.open(
                temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                0o600,
            )
            try:
                while chunk := os.read(source_fd, 1024 * 1024):
                    _write_all(destination_fd, chunk)
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
        except BaseException:
            if temp.exists():
                temp.unlink()
            raise
        finally:
            os.close(source_fd)
        return temp

    async def _clean_staged_file(
        self, intent: PromotionIntent, staged_parts: tuple[str, ...], canonical: Path
    ) -> None:
        """Safely remove only a matching staged file after durable commit."""
        if intent.status != "COMMITTED" or not canonical.is_file():
            return
        if sha256_file(canonical) != intent.checksum:
            await self._raise_checksum_conflict(intent, "committed canonical artifact has a different checksum")
        try:
            staged_fd = self._open_staged_file(intent.action_id, intent.attempt, staged_parts)
        except ValueError as exc:
            await self._record_invalid_intent(intent, str(exc))
            raise ArtifactConflictError(f"invalid promotion intent requires ledger repair: {exc}") from exc
        if staged_fd is None:
            return
        try:
            if _sha256_fd(staged_fd) != intent.checksum:
                await self._raise_checksum_conflict(intent, "staged artifact has a different checksum")
        finally:
            os.close(staged_fd)
        parent_fd = self._open_staged_parent(intent.action_id, intent.attempt, staged_parts)
        try:
            os.unlink(staged_parts[-1], dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

    async def _raise_checksum_conflict(self, intent: PromotionIntent, detail: str) -> None:
        await self._require_ledger().record_promotion_incident(
            intent.intent_id,
            error_code="artifact_checksum_conflict",
            message=(
                f"{detail} for {intent.canonical_relpath}; "
                "inspect and choose the canonical artifact"
            ),
        )
        raise ArtifactConflictError(
            f"{detail} for {intent.canonical_relpath}; inspect and choose the canonical artifact"
        )

    async def _record_invalid_intent(self, intent: PromotionIntent, detail: str) -> None:
        await self._require_ledger().record_promotion_incident(
            intent.intent_id,
            error_code="artifact_intent_invalid",
            message=f"promotion intent {intent.intent_id} is unsafe: {detail}; repair the ledger",
        )

    async def _validated_staged_checksum(
        self, intent: PromotionIntent, parts: tuple[str, ...]
    ) -> str | None:
        try:
            return self._staged_checksum(intent.action_id, intent.attempt, parts)
        except ValueError as exc:
            await self._record_invalid_intent(intent, str(exc))
            raise ArtifactConflictError(f"invalid promotion intent requires ledger repair: {exc}") from exc

    def _staged_file_parts(
        self, action_id: str, attempt: int, staged_relpath: str
    ) -> tuple[str, ...]:
        if not _is_safe_component(action_id):
            raise ValueError("action_id must be a safe path component")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        parts = _safe_relative_parts(staged_relpath)
        prefix = ("state", "staging", action_id, str(attempt))
        if len(parts) <= len(prefix) or parts[: len(prefix)] != prefix:
            raise ValueError("staged artifact must be inside its attempt staging directory")
        return parts[len(prefix) :]

    def _normalized_staged_relpath(
        self, action_id: str, attempt: int, parts: tuple[str, ...]
    ) -> str:
        return Path("state", "staging", action_id, str(attempt), *parts).as_posix()

    def _canonical_path(self, canonical_relpath: str) -> Path:
        parts = _safe_relative_parts(canonical_relpath)
        if parts[:2] == ("state", "staging"):
            raise ValueError("canonical artifact must be outside the staging root")
        canonical = (self._project.root.resolve() / Path(*parts)).resolve()
        try:
            canonical.relative_to(self._project.root.resolve())
        except ValueError as exc:
            raise ValueError("canonical artifact must stay inside the project") from exc
        return canonical

    def _staged_checksum(self, action_id: str, attempt: int, parts: tuple[str, ...]) -> str | None:
        fd = self._open_staged_file(action_id, attempt, parts)
        if fd is None:
            return None
        try:
            return _sha256_fd(fd)
        finally:
            os.close(fd)

    def _open_staged_file(self, action_id: str, attempt: int, parts: tuple[str, ...]) -> int | None:
        try:
            parent_fd = self._open_staged_parent(action_id, attempt, parts)
        except FileNotFoundError:
            return None
        try:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | _O_NOFOLLOW, dir_fd=parent_fd)
            except FileNotFoundError:
                return None
            _require_regular_file(fd)
            return fd
        finally:
            os.close(parent_fd)

    def _open_staged_parent(self, action_id: str, attempt: int, parts: tuple[str, ...]) -> int:
        fd = self._open_staging_attempt_dir(action_id, attempt, create=False)
        try:
            for component in parts[:-1]:
                next_fd = _open_directory_at(fd, component, create=False)
                os.close(fd)
                fd = next_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_staging_attempt_dir(self, action_id: str, attempt: int, *, create: bool) -> int:
        if not _is_safe_component(action_id):
            raise ValueError("action_id must be a safe path component")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        fd = os.open(self._project.root.resolve(), os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
        try:
            for component in ("state", "staging", action_id, str(attempt)):
                next_fd = _open_directory_at(fd, component, create=create)
                os.close(fd)
                fd = next_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _invoke_test_hook(self, point: str, intent: PromotionIntent | None) -> None:
        if self._test_hook is not None:
            self._test_hook(point, intent)

    def _project_relative(self, path: Path) -> str:
        return path.relative_to(self._project.root.resolve()).as_posix()

    def _require_ledger(self) -> RunLedger:
        if self._ledger is None:
            raise RuntimeError("a RunLedger is required to prepare or promote artifacts")
        return self._ledger


_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _safe_relative_parts(value: str) -> tuple[str, ...]:
    path = PurePath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("artifact path must be project-relative and may not escape the project")
    if any(part in {"", "."} for part in path.parts):
        raise ValueError("artifact path contains an unsafe component")
    return tuple(path.parts)


def _is_safe_component(value: str) -> bool:
    return bool(value) and Path(value).name == value and value not in {".", ".."}


def _open_directory_at(parent_fd: int, component: str, *, create: bool) -> int:
    try:
        return os.open(component, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        with suppress(FileExistsError):
            os.mkdir(component, dir_fd=parent_fd)
        return os.open(component, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("attempt staging directory may not traverse a symlink") from exc
        raise


def _require_regular_file(fd: int) -> None:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise ValueError("staged artifact must be a regular file")


def _sha256_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = sha256()
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _require_secure_dirfd_support() -> None:
    if os.name != "posix" or _O_DIRECTORY == 0 or _O_NOFOLLOW == 0:
        raise RuntimeError("artifact promotion requires POSIX dirfd and O_NOFOLLOW support")
