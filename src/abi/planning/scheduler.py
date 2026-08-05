"""Deterministic dependency-aware scheduling for authorized Actions."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Protocol, TypeVar


class SchedulableAction(Protocol):
    """The immutable action fields needed by the scheduler."""

    @property
    def action_id(self) -> str: ...

    @property
    def capability(self) -> str: ...

    @property
    def dependencies(self) -> tuple[str, ...]: ...

    @property
    def priority(self) -> int: ...

    @property
    def read_set(self) -> tuple[str, ...]: ...

    @property
    def write_set(self) -> tuple[str, ...]: ...


_ActionT = TypeVar("_ActionT", bound=SchedulableAction)


def _resource_root(path: str) -> str:
    return path[:-2] if path.endswith("/*") else path


def resource_paths_overlap(left: str, right: str) -> bool:
    """Return whether two exact/directory resource keys share one namespace."""
    left_root = _resource_root(left)
    right_root = _resource_root(right)
    return (
        left_root == right_root
        or left_root.startswith(f"{right_root}/")
        or right_root.startswith(f"{left_root}/")
    )


def _collections_overlap(left: Collection[str], right: Collection[str]) -> bool:
    return any(resource_paths_overlap(a, b) for a in left for b in right)


def access_sets_conflict(
    left_reads: Collection[str],
    left_writes: Collection[str],
    right_reads: Collection[str],
    right_writes: Collection[str],
) -> bool:
    """Apply one prefix-aware read/write conflict rule in policy and scheduler."""
    return (
        _collections_overlap(left_writes, right_writes)
        or _collections_overlap(left_writes, right_reads)
        or _collections_overlap(left_reads, right_writes)
    )


class Scheduler:
    """Select a stable batch whose admitted Actions cannot conflict."""

    def __init__(self, *, max_parallel: int) -> None:
        if max_parallel < 1:
            raise ValueError(
                "max_parallel must be at least 1; configure a positive Action concurrency limit"
            )
        self._max_parallel = max_parallel

    def select_batch(
        self,
        actions: Sequence[_ActionT],
        *,
        committed_action_ids: Collection[str] = (),
        eligible_capabilities: Collection[str] | None = None,
    ) -> tuple[_ActionT, ...]:
        """Admit ready Actions by descending priority and stable action identity."""
        committed = frozenset(committed_action_ids)
        eligible = (
            None if eligible_capabilities is None else frozenset(eligible_capabilities)
        )
        selected: list[_ActionT] = []
        batch_reads: set[str] = set()
        batch_writes: set[str] = set()
        ordered = sorted(actions, key=lambda action: (-action.priority, action.action_id))
        for action in ordered:
            if len(selected) >= self._max_parallel:
                break
            if eligible is not None and action.capability not in eligible:
                continue
            if any(dependency not in committed for dependency in action.dependencies):
                continue
            reads = set(action.read_set)
            writes = set(action.write_set)
            if access_sets_conflict(reads, writes, batch_reads, batch_writes):
                continue
            selected.append(action)
            batch_reads.update(reads)
            batch_writes.update(writes)
        return tuple(selected)
