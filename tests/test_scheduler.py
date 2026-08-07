"""Behavioral tests for dependency-aware conflict-free Action scheduling."""

from __future__ import annotations

from abi.planning.scheduler import Scheduler
from abi.types.orchestration import (
    AuthorizedAction,
    ExpectedArtifactManifest,
    RetryPolicySpec,
    canonical_manifest_json,
    canonical_model_json,
    sha256_canonical_json,
)


def _action(
    action_id: str,
    *,
    priority: int = 0,
    dependencies: tuple[str, ...] = (),
    reads: tuple[str, ...] = (),
    writes: tuple[str, ...] = (),
) -> AuthorizedAction:
    manifest = ExpectedArtifactManifest(action_id=action_id)
    retry_policy = RetryPolicySpec(max_attempts=1)
    return AuthorizedAction(
        action_id=action_id,
        proposal_id=action_id,
        plan_version=1,
        capability=f"test.{action_id}",
        parameters_json="{}",
        dependencies=dependencies,
        priority=priority,
        read_set=reads,
        write_set=writes,
        idempotency_key=action_id,
        expected_artifact_manifest=manifest,
        expected_artifact_manifest_digest=sha256_canonical_json(
            canonical_manifest_json(manifest)
        ),
        retry_policy=retry_policy,
        retry_policy_fingerprint=sha256_canonical_json(
            canonical_model_json(retry_policy)
        ),
    )


def test_scheduler_parallelizes_only_non_conflicting_actions() -> None:
    """Catch a batch that admits two writers of the same canonical namespace."""
    batch = Scheduler(max_parallel=4).select_batch(
        (
            _action(
                "t1",
                reads=("chapters/src/001.md",),
                writes=("chapters/translated/001.md",),
            ),
            _action(
                "t2",
                reads=("chapters/src/002.md",),
                writes=("chapters/translated/002.md",),
            ),
            _action(
                "g",
                reads=("chapters/translated/*",),
                writes=("glossary/terms.csv",),
            ),
            _action("g2", writes=("glossary/terms.csv",)),
        )
    )

    assert {item.action_id for item in batch} == {"g"}


def test_scheduler_uses_priority_identity_and_committed_dependencies() -> None:
    """Catch catalog insertion order or merely-existing dependencies driving execution."""
    actions = (
        _action("z-low", priority=1),
        _action("b-high", priority=9),
        _action("a-high", priority=9),
        _action("dependent", priority=20, dependencies=("parent",)),
    )
    scheduler = Scheduler(max_parallel=2)

    blocked = scheduler.select_batch(actions)
    ready = scheduler.select_batch(actions, committed_action_ids=frozenset({"parent"}))

    assert tuple(item.action_id for item in blocked) == ("a-high", "b-high")
    assert tuple(item.action_id for item in ready) == ("dependent", "a-high")


def test_scheduler_rechecks_current_eligibility() -> None:
    """Catch stale authorization dispatching a capability that policy no longer deems eligible."""
    actions = (_action("eligible", priority=1), _action("stale", priority=10))

    batch = Scheduler(max_parallel=2).select_batch(
        actions,
        eligible_capabilities=frozenset({"test.eligible"}),
    )

    assert tuple(item.action_id for item in batch) == ("eligible",)


def test_scheduler_keeps_durable_unblock_replacement_dispatchable() -> None:
    """A controller-selected recovery must not be stranded after its incident closes."""
    recovery = _action("recovery", priority=100)

    batch = Scheduler(max_parallel=2).select_batch(
        (recovery,),
        eligible_capabilities=frozenset(),
        recovery_action_ids=frozenset({recovery.action_id}),
    )

    assert batch == (recovery,)


def test_scheduler_blocks_directory_and_descendant_resource_overlap() -> None:
    """Catch a directory lock and its child being treated as independent resources."""
    batch = Scheduler(max_parallel=2).select_batch(
        (
            _action("directory", writes=("chapters/translated",)),
            _action("file", writes=("chapters/translated/001.md",)),
        )
    )

    assert tuple(action.action_id for action in batch) == ("directory",)
