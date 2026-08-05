"""Deterministic, fail-closed authorization for planner proposals."""

from __future__ import annotations

from collections.abc import Iterable

from abi.actions.contracts import ResolvedAction
from abi.actions.registry import ActionRegistry, RegistryConfigurationError
from abi.types.orchestration import (
    AuthorizationDecision,
    AuthorizedAction,
    PlanPatch,
    ProposedAction,
    RunSnapshot,
    canonical_manifest_json,
    canonical_model_json,
    sha256_canonical_json,
)


class PolicyEngine:
    """Authorize a complete patch only when every deterministic guard passes."""

    def __init__(self, registry: ActionRegistry) -> None:
        self._registry = registry

    def authorize(
        self, snapshot: RunSnapshot, patch: PlanPatch, *, next_plan_version: int
    ) -> AuthorizationDecision:
        if next_plan_version != snapshot.plan_version + 1:
            return AuthorizationDecision(
                authorized=False, reason_codes=("invalid_plan_version",)
            )
        reasons = self._collect_rejections(snapshot, patch)
        if reasons:
            return AuthorizationDecision(
                authorized=False, reason_codes=tuple(sorted(reasons))
            )
        actions = self._resolve_actions(snapshot, patch, next_plan_version)
        return AuthorizationDecision(authorized=True, actions=actions)

    def _collect_rejections(self, snapshot: RunSnapshot, patch: PlanPatch) -> set[str]:
        proposals = patch.proposed_actions
        reasons: set[str] = set()
        if not 1 <= len(proposals) <= 5:
            reasons.add("invalid_horizon")
        if len({proposal.proposal_id for proposal in proposals}) != len(proposals):
            reasons.add("duplicate_proposal_id")

        eligible = {action.capability for action in snapshot.eligible_actions}
        resolved = self._resolve_known_actions(proposals, reasons)
        for proposal in proposals:
            if not self._registry.contains(proposal.capability):
                reasons.add("unknown_capability")
                continue
            if proposal.capability not in eligible:
                reasons.add("ineligible_capability")
            definition = self._registry.get(proposal.capability)
            if not self._registry.prerequisites_pass(definition, snapshot):
                reasons.add("hard_prerequisite_failed")

        self._check_dependencies(snapshot, proposals, reasons)
        self._check_budget(snapshot, resolved.values(), reasons)
        self._check_conflicts(proposals, reasons)
        self._check_failure_signatures(snapshot, resolved.values(), reasons)
        self._check_terminal_release_policy(snapshot, resolved.values(), reasons)
        self._check_repair_routes(snapshot, proposals, reasons)
        return reasons

    def _check_repair_routes(
        self,
        snapshot: RunSnapshot,
        proposals: tuple[ProposedAction, ...],
        reasons: set[str],
    ) -> None:
        repair_incidents = tuple(
            incident
            for incident in snapshot.incidents
            if incident.repair_class is not None
        )
        if any(incident.repair_class == "integrity" for incident in repair_incidents):
            reasons.add("integrity_incident_open")
            return
        semantic_reasons = tuple(
            incident.reason_code
            for incident in repair_incidents
            if incident.repair_class == "semantic" and incident.reason_code is not None
        )
        if not semantic_reasons:
            return
        mapped: set[str] = set()
        for reason_code in semantic_reasons:
            if not self._registry.has_semantic_repair(reason_code):
                reasons.add("unmapped_semantic_repair")
                continue
            mapped.add(self._registry.semantic_repair_capability(reason_code))
        if any(proposal.capability not in mapped for proposal in proposals):
            reasons.add("semantic_repair_capability_mismatch")

    def _resolve_known_actions(
        self, proposals: tuple[ProposedAction, ...], reasons: set[str]
    ) -> dict[str, ResolvedAction]:
        resolved: dict[str, ResolvedAction] = {}
        for proposal in proposals:
            if not self._registry.contains(proposal.capability):
                continue
            try:
                resolved[proposal.proposal_id] = self._registry.resolve(
                    proposal.capability, proposal.arguments
                )
            except RegistryConfigurationError:
                reasons.add("invalid_arguments")
        return resolved

    @staticmethod
    def _check_dependencies(
        snapshot: RunSnapshot,
        proposals: tuple[ProposedAction, ...],
        reasons: set[str],
    ) -> None:
        proposal_ids = {proposal.proposal_id for proposal in proposals}
        existing_ids = {action.action_id for action in snapshot.actions}
        for proposal in proposals:
            if any(
                dependency not in proposal_ids and dependency not in existing_ids
                for dependency in proposal.dependencies
            ):
                reasons.add("unknown_dependency")
        graph = {
            proposal.proposal_id: tuple(
                dependency
                for dependency in proposal.dependencies
                if dependency in proposal_ids
            )
            for proposal in proposals
        }
        if PolicyEngine._has_cycle(graph):
            reasons.add("dependency_cycle")

    @staticmethod
    def _has_cycle(graph: dict[str, tuple[str, ...]]) -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(visit(dependency) for dependency in graph[node]):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        return any(visit(node) for node in graph)

    @staticmethod
    def _check_budget(
        snapshot: RunSnapshot,
        resolved: Iterable[ResolvedAction],
        reasons: set[str],
    ) -> None:
        if snapshot.remaining_budget_usd is None:
            return
        cost = sum(action.definition.spec.estimated_cost_usd for action in resolved)
        if cost > snapshot.remaining_budget_usd:
            reasons.add("budget_exceeded")

    def _check_conflicts(
        self, proposals: tuple[ProposedAction, ...], reasons: set[str]
    ) -> None:
        definitions = [
            self._registry.get(proposal.capability)
            for proposal in proposals
            if self._registry.contains(proposal.capability)
        ]
        for index, left in enumerate(definitions):
            left_reads = set(left.spec.read_set)
            left_writes = set(left.spec.write_set)
            for right in definitions[index + 1 :]:
                right_reads = set(right.spec.read_set)
                right_writes = set(right.spec.write_set)
                if (
                    left_writes & right_writes
                    or left_writes & right_reads
                    or left_reads & right_writes
                ):
                    reasons.add("write_conflict")

    @staticmethod
    def _check_failure_signatures(
        snapshot: RunSnapshot,
        resolved: Iterable[ResolvedAction],
        reasons: set[str],
    ) -> None:
        prior = set(snapshot.failure_signatures)
        for action in resolved:
            signature = f"{action.definition.spec.capability}:{action.parameters_json}"
            if signature in prior:
                reasons.add("repeated_failure_signature")

    @staticmethod
    def _check_terminal_release_policy(
        snapshot: RunSnapshot,
        resolved: Iterable[ResolvedAction],
        reasons: set[str],
    ) -> None:
        for action in resolved:
            if not action.definition.spec.capability.startswith("release."):
                continue
            required = {
                evidence.name
                for evidence in action.definition.spec.expected_evidence
                if evidence.required
            }
            passed = {
                evidence.gate
                for evidence in snapshot.gate_evidence
                if evidence.passed
            }
            if snapshot.status.value != "RUNNING" or not required <= passed:
                reasons.add("terminal_release_policy")

    def _resolve_actions(
        self, snapshot: RunSnapshot, patch: PlanPatch, next_plan_version: int
    ) -> tuple[AuthorizedAction, ...]:
        action_ids = {
            proposal.proposal_id: f"{snapshot.run_id}:{next_plan_version}:{proposal.proposal_id}"
            for proposal in patch.proposed_actions
        }
        authorized: list[AuthorizedAction] = []
        for proposal in patch.proposed_actions:
            resolved = self._registry.resolve(proposal.capability, proposal.arguments)
            action_id = action_ids[proposal.proposal_id]
            dependencies = tuple(
                action_ids.get(dependency, dependency) for dependency in proposal.dependencies
            )
            manifest = resolved.definition.effect_expander(
                proposal.capability, action_id, resolved.parameters
            )
            retry_policy = resolved.definition.spec.retry_policy
            authorized.append(
                AuthorizedAction(
                    action_id=action_id,
                    proposal_id=proposal.proposal_id,
                    plan_version=next_plan_version,
                    capability=proposal.capability,
                    parameters_json=resolved.parameters_json,
                    dependencies=dependencies,
                    priority=proposal.priority,
                    read_set=resolved.definition.spec.read_set,
                    write_set=resolved.definition.spec.write_set,
                    idempotency_key=action_id,
                    expected_artifact_manifest=manifest,
                    expected_artifact_manifest_digest=sha256_canonical_json(
                        canonical_manifest_json(manifest)
                    ),
                    expected_evidence_refs=tuple(
                        item.name
                        for item in resolved.definition.spec.expected_evidence
                        if item.required
                    ),
                    retry_policy=retry_policy,
                    retry_policy_fingerprint=sha256_canonical_json(
                        canonical_model_json(retry_policy)
                    ),
                )
            )
        return tuple(authorized)
