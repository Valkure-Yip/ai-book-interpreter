"""Deterministic, fail-closed authorization for planner proposals."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from abi.actions.contracts import ActionAccess, ResolvedAction
from abi.actions.registry import ActionRegistry, RegistryConfigurationError
from abi.planning.scheduler import access_sets_conflict
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


@dataclass(frozen=True, slots=True)
class _ResolvedCandidate:
    action: ResolvedAction
    access: ActionAccess


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
        reasons, resolved = self._collect_rejections(snapshot, patch)
        if reasons:
            return AuthorizationDecision(
                authorized=False, reason_codes=tuple(sorted(reasons))
            )
        actions = self._resolve_actions(
            snapshot, patch, next_plan_version, resolved=resolved
        )
        return AuthorizationDecision(authorized=True, actions=actions)

    def _collect_rejections(
        self, snapshot: RunSnapshot, patch: PlanPatch
    ) -> tuple[set[str], dict[str, _ResolvedCandidate]]:
        proposals = patch.proposed_actions
        reasons: set[str] = set()
        if not 1 <= len(proposals) <= 5:
            reasons.add("invalid_horizon")
        if len({proposal.proposal_id for proposal in proposals}) != len(proposals):
            reasons.add("duplicate_proposal_id")

        eligible = {action.capability for action in snapshot.eligible_actions}
        semantic_repair_capabilities = {
            self._registry.semantic_repair_capability(incident.reason_code)
            for incident in snapshot.incidents
            if incident.repair_class == "semantic"
            and incident.reason_code is not None
            and self._registry.has_semantic_repair(incident.reason_code)
        }
        resolved = self._resolve_known_actions(proposals, reasons)
        for proposal in proposals:
            if not self._registry.contains(proposal.capability):
                reasons.add("unknown_capability")
                continue
            if proposal.capability not in eligible:
                reasons.add("ineligible_capability")
            definition = self._registry.get(proposal.capability)
            if (
                proposal.capability not in semantic_repair_capabilities
                and not self._registry.prerequisites_pass(definition, snapshot)
            ):
                reasons.add("hard_prerequisite_failed")

        self._check_dependencies(snapshot, proposals, reasons)
        resolved_actions = tuple(candidate.action for candidate in resolved.values())
        self._check_budget(snapshot, resolved_actions, reasons)
        self._check_conflicts(resolved.values(), reasons)
        self._check_failure_signatures(snapshot, resolved_actions, reasons)
        self._check_completed_capabilities(snapshot, proposals, reasons)
        self._check_repair_routes(snapshot, proposals, reasons)
        return reasons, resolved

    def _check_completed_capabilities(
        self,
        snapshot: RunSnapshot,
        proposals: tuple[ProposedAction, ...],
        reasons: set[str],
    ) -> None:
        """Do not let a free-form planner replay already-current business work."""
        completed = {
            action.capability
            for action in snapshot.actions
            if action.status.value == "SUCCEEDED" and action.outputs_current
        }
        repair_capabilities = {
            self._registry.semantic_repair_capability(incident.reason_code)
            for incident in snapshot.incidents
            if incident.repair_class == "semantic"
            and incident.reason_code is not None
            and self._registry.has_semantic_repair(incident.reason_code)
        }
        if any(
            proposal.capability in completed
            and proposal.capability not in repair_capabilities
            for proposal in proposals
        ):
            reasons.add("capability_already_succeeded")

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
    ) -> dict[str, _ResolvedCandidate]:
        resolved: dict[str, _ResolvedCandidate] = {}
        for proposal in proposals:
            if not self._registry.contains(proposal.capability):
                continue
            try:
                action = self._registry.resolve(
                    proposal.capability, proposal.arguments
                )
                resolved[proposal.proposal_id] = _ResolvedCandidate(
                    action=action,
                    access=self._registry.access_for(action),
                )
            except (RegistryConfigurationError, TypeError, ValueError):
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
        self, resolved: Iterable[_ResolvedCandidate], reasons: set[str]
    ) -> None:
        actions = tuple(resolved)
        for index, left in enumerate(actions):
            for right in actions[index + 1 :]:
                if access_sets_conflict(
                    left.access.read_set,
                    left.access.write_set,
                    right.access.read_set,
                    right.access.write_set,
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

    def _resolve_actions(
        self,
        snapshot: RunSnapshot,
        patch: PlanPatch,
        next_plan_version: int,
        *,
        resolved: Mapping[str, _ResolvedCandidate],
    ) -> tuple[AuthorizedAction, ...]:
        action_ids = {
            proposal.proposal_id: f"{snapshot.run_id}:{next_plan_version}:{proposal.proposal_id}"
            for proposal in patch.proposed_actions
        }
        repair_incidents_by_capability: dict[str, list[str]] = {}
        for incident in snapshot.incidents:
            reason_code = incident.reason_code
            if (
                incident.repair_class == "semantic"
                and reason_code is not None
                and self._registry.has_semantic_repair(reason_code)
            ):
                capability = self._registry.semantic_repair_capability(reason_code)
                repair_incidents_by_capability.setdefault(capability, []).append(
                    incident.incident_id
                )
        authorized: list[AuthorizedAction] = []
        for proposal in patch.proposed_actions:
            candidate = resolved[proposal.proposal_id]
            action = candidate.action
            action_id = action_ids[proposal.proposal_id]
            dependencies = tuple(
                action_ids.get(dependency, dependency) for dependency in proposal.dependencies
            )
            manifest = action.definition.effect_expander(
                proposal.capability, action_id, action.parameters
            )
            access = candidate.access
            retry_policy = action.definition.spec.retry_policy
            authorized.append(
                AuthorizedAction(
                    action_id=action_id,
                    proposal_id=proposal.proposal_id,
                    plan_version=next_plan_version,
                    capability=proposal.capability,
                    parameters_json=action.parameters_json,
                    dependencies=dependencies,
                    priority=proposal.priority,
                    read_set=access.read_set,
                    write_set=access.write_set,
                    idempotency_key=action_id,
                    expected_artifact_manifest=manifest,
                    expected_artifact_manifest_digest=sha256_canonical_json(
                        canonical_manifest_json(manifest)
                    ),
                    expected_evidence_refs=tuple(
                        item.name
                        for item in action.definition.spec.expected_evidence
                        if item.required
                    ),
                    repairs_incident_ids=tuple(
                        sorted(repair_incidents_by_capability.get(proposal.capability, ()))
                    ),
                    retry_policy=retry_policy,
                    retry_policy_fingerprint=sha256_canonical_json(
                        canonical_model_json(retry_policy)
                    ),
                )
            )
        return tuple(authorized)
