"""Crash-reconcilable promotion from attempt staging to canonical artifacts."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Callable
from contextlib import suppress
from hashlib import sha256
from pathlib import Path, PurePath
from typing import NoReturn
from weakref import finalize

from abi.project.artifact_paths import canonical_artifact_key
from abi.project.layout import BookProject
from abi.project.run_ledger import (
    LedgerError,
    LedgerTransitionError,
    PromotionIntent,
    RunLedger,
)
from abi.types.orchestration import (
    ActionOutcomeEnvelope,
    ArtifactBundle,
    ArtifactBundleEntry,
    ArtifactMetadata,
    AttemptOutcomeReceiptPayload,
    ExpectedArtifactManifest,
    Succeeded,
    canonical_bundle_json,
    canonical_model_json,
    sha256_canonical_json,
)

__all__ = [
    "ArtifactConflictError",
    "ArtifactReconciliationError",
    "ArtifactStore",
    "AttemptStagingWriter",
    "InjectedCrash",
    "PromotionIntent",
    "sha256_file",
]

TestHook = Callable[[str, PromotionIntent | None], None]
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


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


class AttemptStagingWriter:
    """Create-only logical canonical writes bound to one attempt namespace."""

    def __init__(self, store: ArtifactStore, action_id: str, attempt: int) -> None:
        _validate_attempt(action_id, attempt)
        self._store = store
        self.action_id = action_id
        self.attempt = attempt
        self._entries: list[ArtifactBundleEntry] = []

    def write_bytes(
        self,
        canonical_relpath: str,
        content: bytes,
        *,
        media_type: str,
        evidence_role: str,
        metadata: tuple[ArtifactMetadata, ...] = (),
    ) -> ArtifactBundleEntry:
        key = canonical_artifact_key(canonical_relpath)
        if self._entries and key <= self._entries[-1].canonical_relpath:
            raise ValueError(
                "attempt effects must be emitted once in strict canonical order; fix the writer"
            )
        self._store.write_staged_bytes(
            action_id=self.action_id,
            attempt=self.attempt,
            relative_path=key,
            content=content,
        )
        entry = ArtifactBundleEntry(
            staged_relpath=f"state/staging/{self.action_id}/{self.attempt}/{key}",
            canonical_relpath=key,
            media_type=media_type,
            evidence_role=evidence_role,
            metadata=metadata,
        )
        self._entries.append(entry)
        return entry

    def write_text(
        self,
        canonical_relpath: str,
        content: str,
        *,
        media_type: str = "text/plain",
        evidence_role: str,
        metadata: tuple[ArtifactMetadata, ...] = (),
    ) -> ArtifactBundleEntry:
        return self.write_bytes(
            canonical_relpath,
            content.encode("utf-8"),
            media_type=media_type,
            evidence_role=evidence_role,
            metadata=metadata,
        )

    @property
    def entries(self) -> tuple[ArtifactBundleEntry, ...]:
        return tuple(self._entries)

    def read_bytes(self, canonical_relpath: str) -> bytes:
        """Read one output already emitted by this writer through no-follow dirfds."""
        key = canonical_artifact_key(canonical_relpath)
        if key not in {entry.canonical_relpath for entry in self._entries}:
            raise KeyError(f"{key} has not been emitted by this attempt writer")
        return self._store.read_staged_bytes(self.action_id, self.attempt, key)

    def staged_path(self, canonical_relpath: str) -> Path:
        """Return the fixed display path of an output already emitted by this writer."""
        key = canonical_artifact_key(canonical_relpath)
        entry = next(
            (item for item in self._entries if item.canonical_relpath == key), None
        )
        if entry is None:
            raise KeyError(f"{key} has not been emitted by this attempt writer")
        return self._store._project.root / entry.staged_relpath

    def artifact_bundle(self) -> ArtifactBundle:
        return ArtifactBundle(
            action_id=self.action_id,
            attempt=self.attempt,
            entries=self.entries,
        )

class BufferedAttemptWriter:
    """Collect composite outputs in memory, then flush once in canonical order."""

    def __init__(self, action_id: str, attempt: int) -> None:
        _validate_attempt(action_id, attempt)
        self.action_id = action_id
        self.attempt = attempt
        self._items: dict[str, tuple[bytes, str, str, tuple[ArtifactMetadata, ...]]] = {}

    def write_bytes(
        self,
        canonical_relpath: str,
        content: bytes,
        *,
        media_type: str,
        evidence_role: str,
        metadata: tuple[ArtifactMetadata, ...] = (),
    ) -> ArtifactBundleEntry:
        key = canonical_artifact_key(canonical_relpath)
        if key in self._items:
            raise FileExistsError(f"composite output {key} was already emitted")
        self._items[key] = (content, media_type, evidence_role, metadata)
        return self._entry(key)

    def write_text(
        self,
        canonical_relpath: str,
        content: str,
        *,
        media_type: str = "text/plain",
        evidence_role: str,
        metadata: tuple[ArtifactMetadata, ...] = (),
    ) -> ArtifactBundleEntry:
        return self.write_bytes(
            canonical_relpath,
            content.encode(),
            media_type=media_type,
            evidence_role=evidence_role,
            metadata=metadata,
        )

    @property
    def entries(self) -> tuple[ArtifactBundleEntry, ...]:
        return tuple(self._entry(key) for key in sorted(self._items))

    def read_bytes(self, canonical_relpath: str) -> bytes:
        key = canonical_artifact_key(canonical_relpath)
        try:
            return self._items[key][0]
        except KeyError as exc:
            raise KeyError(f"{key} has not been emitted by this buffered writer") from exc

    def artifact_bundle(self) -> ArtifactBundle:
        return ArtifactBundle(
            action_id=self.action_id,
            attempt=self.attempt,
            entries=self.entries,
        )

    def staged_path(self, canonical_relpath: str) -> Path:
        canonical_artifact_key(canonical_relpath)
        raise RuntimeError("buffered outputs have no filesystem path before canonical-order flush")

    def flush_to(self, writer: AttemptStagingWriter) -> ArtifactBundle:
        if writer.action_id != self.action_id or writer.attempt != self.attempt:
            raise ValueError("buffer and attempt writer identities must match")
        for key in sorted(self._items):
            content, media_type, evidence_role, metadata = self._items[key]
            writer.write_bytes(
                key,
                content,
                media_type=media_type,
                evidence_role=evidence_role,
                metadata=metadata,
            )
        return writer.artifact_bundle()

    def _entry(self, key: str) -> ArtifactBundleEntry:
        _, media_type, evidence_role, metadata = self._items[key]
        return ArtifactBundleEntry(
            staged_relpath=f"state/staging/{self.action_id}/{self.attempt}/{key}",
            canonical_relpath=key,
            media_type=media_type,
            evidence_role=evidence_role,
            metadata=metadata,
        )


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of a regular artifact file."""
    if os.name != "posix" or _O_NOFOLLOW == 0 or _O_NONBLOCK == 0:
        raise RuntimeError("artifact checksums require POSIX O_NOFOLLOW and O_NONBLOCK support")
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("artifact checksum path may not be a symlink") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("artifact checksum target must be a regular file")
        return _sha256_fd(fd)
    finally:
        os.close(fd)


class ArtifactStore:
    """Promote PENDING staging input, then reconcile COMMITTED canonical facts only."""

    def __init__(
        self, project: BookProject, ledger: RunLedger | None, *, test_hook: TestHook | None = None
    ) -> None:
        _require_secure_dirfd_support()
        self._project = project
        self._ledger = ledger
        self._test_hook = test_hook
        self._durable_root = Path(os.path.abspath(project.root))
        self._root_fd = _open_trusted_root(self._durable_root)
        self._root_stat = os.fstat(self._root_fd)
        self._root_finalizer = finalize(self, os.close, self._root_fd)

    def close(self) -> None:
        """Release the pinned project-root descriptor."""
        self._root_finalizer()

    def staging_dir(self, action_id: str, attempt: int) -> Path:
        """Return an unsafe display-only path; use :meth:`write_staged_bytes` for writes."""
        _validate_attempt(action_id, attempt)
        return self._project.staging_root / action_id / str(attempt)

    def writer(self, action_id: str, attempt: int) -> AttemptStagingWriter:
        """Return the only output-writing capability exposed to an Action attempt."""
        return AttemptStagingWriter(self, action_id, attempt)

    def read_staged_bytes(self, action_id: str, attempt: int, canonical_relpath: str) -> bytes:
        """Read a current-attempt staged regular file without following links."""
        key = canonical_artifact_key(canonical_relpath)
        fd = self._open_staged_file(action_id, attempt, _safe_relative_parts(key))
        if fd is None:
            raise FileNotFoundError(key)
        try:
            chunks: list[bytes] = []
            while chunk := os.read(fd, 1024 * 1024):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def write_staged_bytes(
        self, *, action_id: str, attempt: int, relative_path: str, content: bytes
    ) -> Path:
        """Safely create one new staged regular file through no-follow dirfds."""
        _require_secure_dirfd_support()
        parts = _safe_relative_parts(relative_path)
        self._assert_root_anchor()
        self._invoke_test_hook("before_staging_mkdir", None)
        parent_fd = self._open_staged_parent(action_id, attempt, parts, create=True)
        try:
            try:
                fd = os.open(
                    parts[-1],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_NONBLOCK,
                    0o600,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                _raise_unsafe_path_error(exc)
            try:
                _require_regular_file(fd)
                _write_all(fd, content)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return self.staging_dir(action_id, attempt) / Path(*parts)

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

    async def verify_committed_bundle(
        self, action_id: str, attempt: int
    ) -> tuple[PromotionIntent, ...]:
        """Reopen and verify every canonical intent as one success prerequisite."""
        try:
            intents = await self._require_ledger().verify_committed_bundle(action_id, attempt)
        except LedgerError as exc:
            await self._require_ledger().mark_bundle_conflict(
                action_id,
                attempt,
                reason_code="partial_intent_set",
                message="Every bundle intent must be COMMITTED before unified postcheck.",
            )
            raise ArtifactConflictError("bundle has pending or conflicting intents") from exc
        for intent in intents:
            try:
                canonical = self._durable_root / canonical_artifact_key(intent.canonical_relpath)
                if sha256_file(canonical) != intent.checksum:
                    raise ValueError("checksum drift")
            except (OSError, ValueError) as exc:
                await self._require_ledger().mark_bundle_conflict(
                    action_id,
                    attempt,
                    reason_code="post_success_drift",
                    message=f"Canonical bundle postcheck failed for {intent.canonical_relpath}: {exc}",
                )
                raise ArtifactConflictError("canonical bundle postcheck failed") from exc
        return intents

    async def rebuild_outcome_receipt(
        self, action_id: str, attempt: int
    ) -> AttemptOutcomeReceiptPayload:
        """Rebuild success only from the exact durable manifest and safe staged leaf set."""
        ledger = self._require_ledger()
        attempt_record = await ledger.get_attempt(action_id, attempt)
        manifest: ExpectedArtifactManifest = attempt_record.expected_artifact_manifest
        if not manifest.entries:
            await ledger.mark_bundle_conflict(
                action_id,
                attempt,
                reason_code="artifact_bundle_conflict",
                message="An empty expected manifest cannot prove an executor outcome.",
            )
            raise ArtifactConflictError("empty manifest outcome is not reconstructable")
        await self.require_exact_staging(action_id, attempt)
        entries = tuple(
            ArtifactBundleEntry(
                staged_relpath=f"state/staging/{action_id}/{attempt}/{item.canonical_relpath}",
                canonical_relpath=item.canonical_relpath,
                media_type=item.media_type,
                evidence_role=item.evidence_role,
                metadata=item.metadata,
            )
            for item in manifest.entries
        )
        bundle = ArtifactBundle(action_id=action_id, attempt=attempt, entries=entries)
        outcome = Succeeded(
            artifact_bundle=bundle,
            evidence_refs=attempt_record.expected_evidence_refs,
        )
        envelope = ActionOutcomeEnvelope(
            action_id=action_id, attempt=attempt, outcome=outcome
        )
        outcome_json = canonical_model_json(envelope)
        bundle_json = canonical_bundle_json(bundle)
        return await ledger.record_attempt_outcome(
            AttemptOutcomeReceiptPayload(
                action_id=action_id,
                attempt=attempt,
                canonical_outcome_json=outcome_json,
                outcome_digest=sha256_canonical_json(outcome_json),
                canonical_bundle_json=bundle_json,
                bundle_digest=sha256_canonical_json(bundle_json),
                evidence_refs=outcome.evidence_refs,
            )
        )

    async def require_exact_staging(self, action_id: str, attempt: int) -> None:
        """Reject missing, extra, linked, or non-regular attempt staging evidence."""
        ledger = self._require_ledger()
        attempt_record = await ledger.get_attempt(action_id, attempt)
        manifest = attempt_record.expected_artifact_manifest
        expected_paths = {item.canonical_relpath for item in manifest.entries}
        expected_dirs: set[str] = set()
        for expected in expected_paths:
            parts = PurePath(expected).parts
            expected_dirs.update(
                PurePath(*parts[:index]).as_posix()
                for index in range(1, len(parts))
            )
        try:
            observed, observed_dirs = self._staging_inventory(
                action_id, attempt
            )
        except (OSError, ValueError):
            await ledger.mark_bundle_conflict(
                action_id,
                attempt,
                reason_code="artifact_bundle_conflict",
                message="Unsafe staged entry prevents exact staging validation.",
            )
            raise ArtifactConflictError("unsafe staging evidence") from None
        if observed != expected_paths or observed_dirs != expected_dirs:
            await ledger.mark_bundle_conflict(
                action_id,
                attempt,
                reason_code="artifact_bundle_conflict",
                message="Staged leaf/directory set differs from the durable expected manifest.",
            )
            raise ArtifactConflictError("staging evidence is incomplete or contains extras")

    def _staging_inventory(
        self, action_id: str, attempt: int
    ) -> tuple[set[str], set[str]]:
        """Enumerate one pinned attempt dir recursively without following aliases."""
        root_fd = self._open_staging_attempt_dir(action_id, attempt, create=False)
        files: set[str] = set()
        directories: set[str] = set()

        def visit(directory_fd: int, prefix: tuple[str, ...]) -> None:
            for name in sorted(os.listdir(directory_fd)):
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                relparts = (*prefix, name)
                relpath = PurePath(*relparts).as_posix()
                if stat.S_ISREG(info.st_mode):
                    files.add(relpath)
                    continue
                if stat.S_ISDIR(info.st_mode):
                    directories.add(relpath)
                    child_fd = _open_directory_at(
                        directory_fd, name, create=False
                    )
                    try:
                        visit(child_fd, relparts)
                    finally:
                        os.close(child_fd)
                    continue
                raise ValueError("staging contains a link or non-regular leaf")

        try:
            visit(root_fd, ())
        finally:
            os.close(root_fd)
        return files, directories

    async def _complete(
        self, intent: PromotionIntent, *, crash_after: str | None = None
    ) -> PromotionIntent:
        _require_secure_dirfd_support()
        if intent.status == "CONFLICT":
            raise ArtifactConflictError(
                f"promotion intent {intent.intent_id} is CONFLICT; inspect its incident and repair the artifact"
            )
        try:
            canonical_parts = self._canonical_parts(intent.canonical_relpath)
            self._assert_root_anchor()
            # Staging is authoritative only until the durable COMMITTED transition.
            staged_parts = (
                self._staged_file_parts(intent.action_id, intent.attempt, intent.staged_relpath)
                if intent.status == "PENDING"
                else None
            )
            canonical_parent_fd = self._open_project_parent(
                canonical_parts, create=intent.status == "PENDING"
            )
        except FileNotFoundError as exc:
            if intent.status == "COMMITTED":
                await self._raise_missing_committed(intent)
            await self._record_invalid_intent(intent, str(exc))
            raise ArtifactConflictError(f"invalid promotion intent requires ledger repair: {exc}") from exc
        except (OSError, ValueError) as exc:
            await self._record_invalid_intent(intent, str(exc))
            raise ArtifactConflictError(f"invalid promotion intent requires ledger repair: {exc}") from exc
        try:
            if intent.status == "COMMITTED":
                if _sha256_regular_at(canonical_parent_fd, canonical_parts[-1]) is None:
                    await self._raise_missing_committed(intent)
                return await self._commit_existing(
                    intent, staged_parts, canonical_parent_fd, canonical_parts[-1]
                )
            assert staged_parts is not None
            staged_checksum = await self._validated_staged_checksum(intent, staged_parts)
            if staged_checksum is None:
                if _sha256_regular_at(canonical_parent_fd, canonical_parts[-1]) is None:
                    await self._require_ledger().record_promotion_incident(
                        intent.intent_id,
                        error_code="artifact_promotion_missing",
                        message=(
                            f"neither staged nor canonical artifact exists for {intent.canonical_relpath}; "
                            "restore the artifact or rerun the action"
                        ),
                    )
                    return intent
                return await self._commit_existing(
                    intent, staged_parts, canonical_parent_fd, canonical_parts[-1]
                )
            if staged_checksum != intent.checksum:
                await self._raise_checksum_conflict(intent, "staged artifact no longer matches its promotion intent")

            self._invoke_test_hook("after_staged_verification", intent)
            self._assert_directory_binding(canonical_parent_fd, canonical_parts[:-1])
            try:
                canonical_fd = os.open(
                    canonical_parts[-1],
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_NONBLOCK,
                    0o600,
                    dir_fd=canonical_parent_fd,
                )
            except FileExistsError:
                return await self._commit_existing(
                    intent, staged_parts, canonical_parent_fd, canonical_parts[-1]
                )
            except OSError as exc:
                _raise_unsafe_path_error(exc)
            try:
                _require_regular_file(canonical_fd)
                try:
                    canonical_stat = os.fstat(canonical_fd)
                    os.fsync(canonical_parent_fd)
                    if crash_after == "after_canonical_create":
                        raise InjectedCrash("injected crash after canonical file creation")
                    source_fd = self._open_staged_file(intent.action_id, intent.attempt, staged_parts)
                    if source_fd is None:
                        raise FileNotFoundError(
                            f"staged artifact {intent.staged_relpath} disappeared during promotion"
                        )
                    try:
                        while chunk := os.read(source_fd, 1024 * 1024):
                            _write_all(canonical_fd, chunk)
                    finally:
                        os.close(source_fd)
                    os.fsync(canonical_fd)
                except OSError as exc:
                    await self._raise_write_incomplete(intent, exc)
                if _sha256_fd(canonical_fd) != intent.checksum:
                    await self._raise_checksum_conflict(
                        intent, "new canonical artifact has a different checksum"
                    )
                self._invoke_test_hook("after_canonical_written", intent)
                self._assert_directory_binding(canonical_parent_fd, canonical_parts[:-1])
                if not _named_inode_matches(canonical_parent_fd, canonical_parts[-1], canonical_stat):
                    await self._raise_checksum_conflict(intent, "canonical artifact name was replaced")
                try:
                    os.fsync(canonical_parent_fd)
                except OSError as exc:
                    await self._raise_write_incomplete(intent, exc)
                if crash_after == "after_canonical_write":
                    raise InjectedCrash("injected crash after canonical artifact write")
                return await self._commit_existing(
                    intent,
                    staged_parts,
                    canonical_parent_fd,
                    canonical_parts[-1],
                    expected_inode=canonical_stat,
                )
            finally:
                os.close(canonical_fd)
        except (OSError, ValueError) as exc:
            await self._record_invalid_intent(intent, str(exc))
            raise ArtifactConflictError(f"invalid promotion intent requires ledger repair: {exc}") from exc
        finally:
            os.close(canonical_parent_fd)

    async def _commit_existing(
        self,
        intent: PromotionIntent,
        staged_parts: tuple[str, ...] | None,
        canonical_parent_fd: int,
        canonical_name: str,
        *,
        expected_inode: os.stat_result | None = None,
    ) -> PromotionIntent:
        """Commit a pre-existing canonical file only when all observed bytes agree."""
        canonical_fd = _open_regular_at(canonical_parent_fd, canonical_name)
        if canonical_fd is None:
            await self._raise_checksum_conflict(intent, "canonical artifact is missing")
        try:
            canonical_stat = os.fstat(canonical_fd)
            if expected_inode is not None and not _same_inode(canonical_stat, expected_inode):
                await self._raise_checksum_conflict(intent, "canonical artifact name was replaced")
            if _sha256_fd(canonical_fd) != intent.checksum:
                await self._raise_checksum_conflict(intent, "canonical artifact has a different checksum")
            self._assert_directory_binding(canonical_parent_fd, _parent_parts(intent.canonical_relpath))
            if not _named_inode_matches(canonical_parent_fd, canonical_name, canonical_stat):
                await self._raise_checksum_conflict(intent, "canonical artifact name was replaced")
            if intent.status == "PENDING":
                assert staged_parts is not None
                staged_checksum = await self._validated_staged_checksum(intent, staged_parts)
                if staged_checksum is not None and staged_checksum != intent.checksum:
                    await self._raise_checksum_conflict(
                        intent, "staged artifact has a different checksum"
                    )
            try:
                os.fsync(canonical_parent_fd)
            except OSError as exc:
                await self._raise_write_incomplete(intent, exc)
            try:
                committed = await self._require_ledger().commit_promotion_intent(intent.intent_id)
            except LedgerTransitionError as exc:
                durable_intent = await self._require_ledger().get_promotion_intent(intent.intent_id)
                if durable_intent.status != "CONFLICT":
                    raise
                raise ArtifactConflictError(
                    f"promotion intent {intent.intent_id} became CONFLICT during commit; "
                    "inspect its incident and continue reconciliation"
                ) from exc
            if (
                _sha256_fd(canonical_fd) != intent.checksum
                or not _named_inode_matches(canonical_parent_fd, canonical_name, canonical_stat)
            ):
                await self._raise_checksum_conflict(
                    intent, "committed canonical artifact has a different inode or checksum"
                )
            self._assert_directory_binding(canonical_parent_fd, _parent_parts(intent.canonical_relpath))
            return committed
        finally:
            os.close(canonical_fd)

    async def _raise_checksum_conflict(self, intent: PromotionIntent, detail: str) -> NoReturn:
        await self._require_ledger().conflict_promotion_intent(
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

    async def _raise_missing_committed(self, intent: PromotionIntent) -> NoReturn:
        await self._require_ledger().conflict_promotion_intent(
            intent.intent_id,
            error_code="artifact_promotion_missing",
            message=(
                f"committed canonical artifact {intent.canonical_relpath} is missing; "
                "inspect storage, retain staged evidence, and create a repair action"
            ),
        )
        raise ArtifactConflictError(
            f"committed canonical artifact {intent.canonical_relpath} is missing; "
            "inspect storage and repair the promotion conflict"
        )

    async def _raise_write_incomplete(self, intent: PromotionIntent, error: OSError) -> NoReturn:
        await self._require_ledger().conflict_promotion_intent(
            intent.intent_id,
            error_code="canonical_write_incomplete",
            message=(
                f"canonical write for {intent.canonical_relpath} was incomplete after storage error "
                f"{error}; inspect storage and retain the partial canonical and staged artifact"
            ),
        )
        raise ArtifactConflictError(
            f"canonical artifact for {intent.canonical_relpath} was retained after storage failure; "
            "it may be partial, so inspect storage and retain both artifacts"
        ) from error

    async def _record_invalid_intent(self, intent: PromotionIntent, detail: str) -> None:
        await self._require_ledger().conflict_promotion_intent(
            intent.intent_id,
            error_code="artifact_intent_invalid",
            message=f"promotion intent {intent.intent_id} is unsafe: {detail}; repair the ledger",
        )

    async def _validated_staged_checksum(
        self, intent: PromotionIntent, parts: tuple[str, ...]
    ) -> str | None:
        try:
            return self._staged_checksum(intent.action_id, intent.attempt, parts)
        except (OSError, ValueError) as exc:
            await self._record_invalid_intent(intent, str(exc))
            raise ArtifactConflictError(f"invalid promotion intent requires ledger repair: {exc}") from exc

    def _staged_file_parts(
        self, action_id: str, attempt: int, staged_relpath: str
    ) -> tuple[str, ...]:
        _validate_attempt(action_id, attempt)
        parts = _safe_relative_parts(staged_relpath)
        prefix = ("state", "staging", action_id, str(attempt))
        if len(parts) <= len(prefix) or parts[: len(prefix)] != prefix:
            raise ValueError("staged artifact must be inside its attempt staging directory")
        return parts[len(prefix) :]

    def _normalized_staged_relpath(
        self, action_id: str, attempt: int, parts: tuple[str, ...]
    ) -> str:
        return Path("state", "staging", action_id, str(attempt), *parts).as_posix()

    def _canonical_parts(self, canonical_relpath: str) -> tuple[str, ...]:
        return tuple(canonical_artifact_key(canonical_relpath).split("/"))

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
            parent_fd = self._open_staged_parent(action_id, attempt, parts, create=False)
        except FileNotFoundError:
            return None
        try:
            try:
                fd = os.open(parts[-1], os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=parent_fd)
            except FileNotFoundError:
                return None
            except OSError as exc:
                _raise_unsafe_path_error(exc)
            try:
                _require_regular_file(fd)
            except BaseException:
                os.close(fd)
                raise
            return fd
        finally:
            os.close(parent_fd)

    def _open_staged_parent(
        self, action_id: str, attempt: int, parts: tuple[str, ...], *, create: bool
    ) -> int:
        fd = self._open_staging_attempt_dir(action_id, attempt, create=create)
        try:
            for component in parts[:-1]:
                next_fd = _open_directory_at(fd, component, create=create)
                os.close(fd)
                fd = next_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_staging_attempt_dir(self, action_id: str, attempt: int, *, create: bool) -> int:
        _validate_attempt(action_id, attempt)
        fd = self._duplicate_root_fd()
        try:
            for component in ("state", "staging", action_id, str(attempt)):
                next_fd = _open_directory_at(fd, component, create=create)
                os.close(fd)
                fd = next_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_project_parent(self, parts: tuple[str, ...], *, create: bool) -> int:
        fd = self._duplicate_root_fd()
        try:
            for component in parts[:-1]:
                next_fd = _open_directory_at(fd, component, create=create)
                os.close(fd)
                fd = next_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _assert_directory_binding(self, expected_fd: int, parts: tuple[str, ...]) -> None:
        """Require a held directory to remain reachable at its durable project-relative path."""
        self._assert_root_anchor()
        actual_fd = self._duplicate_root_fd()
        try:
            for component in parts:
                next_fd = _open_directory_at(actual_fd, component, create=False)
                os.close(actual_fd)
                actual_fd = next_fd
            if not _same_inode(os.fstat(actual_fd), os.fstat(expected_fd)):
                raise ValueError(
                    "project directory no longer matches its durable path; repair the project tree"
                )
        finally:
            os.close(actual_fd)

    def _assert_root_anchor(self) -> None:
        """Require the durable root pathname to still identify this store's pinned root inode."""
        current_fd = _open_trusted_root(self._durable_root)
        try:
            if not _same_inode(os.fstat(current_fd), self._root_stat):
                raise ValueError(
                    "trusted project root no longer identifies the original directory; repair the project path"
                )
        finally:
            os.close(current_fd)

    def _duplicate_root_fd(self) -> int:
        if not self._root_finalizer.alive:
            raise RuntimeError("ArtifactStore is closed; create a new store for the trusted project root")
        return os.dup(self._root_fd)

    def _invoke_test_hook(self, point: str, intent: PromotionIntent | None) -> None:
        if self._test_hook is not None:
            self._test_hook(point, intent)

    def _require_ledger(self) -> RunLedger:
        if self._ledger is None:
            raise RuntimeError("a RunLedger is required to prepare or promote artifacts")
        return self._ledger


def _validate_attempt(action_id: str, attempt: int) -> None:
    if not _is_safe_component(action_id):
        raise ValueError("action_id must be a safe path component")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise ValueError("attempt must be a positive integer")


def _open_trusted_root(path: Path) -> int:
    """Open an absolute directory path without following any symlink component."""
    if not path.is_absolute():
        raise ValueError("trusted project root must be absolute; repair the project path")
    fd = os.open("/", os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW,
                    dir_fd=fd,
                )
            except OSError as exc:
                raise ValueError(
                    "trusted project root may not contain a symlink or missing directory; "
                    "repair the project path"
                ) from exc
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _safe_relative_parts(value: str) -> tuple[str, ...]:
    path = PurePath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("artifact path must be project-relative and may not escape the project")
    if any(part in {"", "."} for part in path.parts):
        raise ValueError("artifact path contains an unsafe component")
    return tuple(path.parts)


def _is_safe_component(value: str) -> bool:
    return bool(value) and Path(value).name == value and value not in {".", ".."}


def _parent_parts(value: str) -> tuple[str, ...]:
    return tuple(canonical_artifact_key(value).split("/"))[:-1]


def _open_directory_at(parent_fd: int, component: str, *, create: bool) -> int:
    try:
        return os.open(component, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        with suppress(FileExistsError):
            os.mkdir(component, dir_fd=parent_fd)
        os.fsync(parent_fd)
        try:
            return os.open(component, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as exc:
            _raise_unsafe_path_error(exc)
    except OSError as exc:
        _raise_unsafe_path_error(exc)


def _raise_unsafe_path_error(exc: OSError) -> NoReturn:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.ENXIO}:
        raise ValueError("attempt staging directory may not traverse a symlink") from exc
    raise exc


def _require_regular_file(fd: int) -> None:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise ValueError("staged artifact must be a regular file")


def _same_inode(actual: os.stat_result, expected: os.stat_result) -> bool:
    return actual.st_dev == expected.st_dev and actual.st_ino == expected.st_ino


def _open_regular_at(parent_fd: int, name: str) -> int | None:
    try:
        fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _raise_unsafe_path_error(exc)
    try:
        _require_regular_file(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _sha256_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = sha256()
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _sha256_regular_at(parent_fd: int, name: str) -> str | None:
    fd = _open_regular_at(parent_fd, name)
    if fd is None:
        return None
    try:
        return _sha256_fd(fd)
    finally:
        os.close(fd)


def _named_inode_matches(parent_fd: int, name: str, expected: os.stat_result) -> bool:
    fd = _open_regular_at(parent_fd, name)
    if fd is None:
        return False
    try:
        actual = os.fstat(fd)
        return _same_inode(actual, expected)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _require_secure_dirfd_support() -> None:
    if (
        os.name != "posix"
        or _O_DIRECTORY == 0
        or _O_NOFOLLOW == 0
        or _O_NONBLOCK == 0
        or os.open not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
    ):
        raise RuntimeError(
            "artifact promotion requires POSIX dirfd, O_NOFOLLOW, and O_NONBLOCK support"
        )
