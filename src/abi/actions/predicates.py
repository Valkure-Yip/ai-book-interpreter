"""Fail-closed predicates used to compute action eligibility."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from abi.types.orchestration import ActionArgument, PredicateSpec, RunSnapshot

Predicate = Callable[[RunSnapshot, tuple[ActionArgument, ...]], bool]


class PredicateConfigurationError(ValueError):
    """A declared predicate cannot be evaluated safely."""


class PredicateCatalog:
    """Named, deterministic predicates available to registered action specs."""

    def __init__(self, predicates: Mapping[str, Predicate] | None = None) -> None:
        self._predicates = dict(predicates or {})

    def register(self, name: str, predicate: Predicate) -> None:
        if name in self._predicates:
            raise PredicateConfigurationError(
                f"duplicate predicate {name}; remove one registration"
            )
        self._predicates[name] = predicate

    def contains(self, name: str) -> bool:
        return name in self._predicates

    def evaluate(self, snapshot: RunSnapshot, spec: PredicateSpec) -> bool:
        try:
            predicate = self._predicates[spec.name]
        except KeyError as exc:
            raise PredicateConfigurationError(
                f"unknown predicate {spec.name}; register the predicate before startup"
            ) from exc
        try:
            return bool(predicate(snapshot, spec.arguments))
        except Exception as exc:
            raise PredicateConfigurationError(
                f"predicate {spec.name} failed; correct the predicate before authorizing actions"
            ) from exc
