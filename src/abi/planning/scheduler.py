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
            if writes & batch_writes or writes & batch_reads or reads & batch_writes:
                continue
            selected.append(action)
            batch_reads.update(reads)
            batch_writes.update(writes)
        return tuple(selected)
