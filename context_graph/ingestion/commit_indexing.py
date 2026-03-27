from __future__ import annotations

from pathlib import Path
import re
from typing import Sequence

from context_graph.ingestion.git.git_metadata import CommitInfo, GitMetadata
from context_graph.provenance import (
    ClaimBundleValidator,
    ClaimEvidenceExtractionService,
    EvidenceInvalidationPlanner,
    EvidenceExtractionService,
    EvidenceMutationPlanner,
    FactConflictResolutionService,
    ObservationMutationPlanner,
)
from context_graph.storage.backend import BackendDriver, merge_mutation_plans, MutationPlan

from .deterministic_planner import DeterministicMutationPlanner
from .models import CommitIndexResult
from .snapshot_builder import CodebaseMemorySnapshotProducer
from .temporal import TemporalGraphWriter
from ..metadata import MetadataStore, RepoIndexState
from ..models import CanonicalNode, GraphSnapshot, SnapshotApplyResult


def default_repo_id(value: str) -> str:
    """Derive a stable repo id from a path or owner/repo name."""

    raw_name = Path(value).name or value
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw_name).strip("-_.").lower()
    return slug or "repo"


def normalize_snapshot_for_commit(
    snapshot: GraphSnapshot,
    *,
    repo_path: str,
    commit: CommitInfo,
) -> GraphSnapshot:
    """Overlay commit metadata onto the exporter snapshot.

    The exporter knows about a repository tree, but not the commit semantics we
    index against. This normalization step makes the snapshot revision and its
    metadata explicitly commit-based before diffing or persistence.
    """

    metadata = dict(snapshot.metadata)
    metadata.update(
        {
            "commit_sha": commit.sha,
            "commit_branch": commit.branch,
            "commit_parent_shas": list(commit.parent_shas),
            "commit_changed_files": list(commit.changed_files),
            "source_revision_kind": "git-commit",
        }
    )
    normalized_nodes = tuple(
        _normalize_node_for_commit(node, repo_path=repo_path)
        for node in snapshot.nodes
    )
    return GraphSnapshot(
        repo_id=snapshot.repo_id,
        revision_id=commit.sha,
        root_path=repo_path,
        generated_at=snapshot.generated_at,
        nodes=normalized_nodes,
        edges=snapshot.edges,
        metadata=metadata,
    )


def _normalize_node_for_commit(node: CanonicalNode, *, repo_path: str) -> CanonicalNode:
    if node.kind != "Project":
        return node
    properties = dict(node.properties)
    properties["root_path"] = repo_path
    return CanonicalNode(
        node_id=node.node_id,
        repo_id=node.repo_id,
        kind=node.kind,
        name=node.name,
        qualified_name=node.qualified_name,
        file_path=node.file_path,
        start_line=node.start_line,
        end_line=node.end_line,
        properties=properties,
    )


class CommitIndexingService:
    """Indexes commits deterministically from a resolved git checkout."""

    def __init__(
        self,
        *,
        metadata_store: MetadataStore,
        backend_driver: BackendDriver | None,
        snapshot_producer: CodebaseMemorySnapshotProducer | None = None,
        git_metadata: GitMetadata | None = None,
        writer: TemporalGraphWriter | None = None,
        planner: DeterministicMutationPlanner | None = None,
        observation_planner: ObservationMutationPlanner | None = None,
        llm_client=None,
        claim_extraction_service: ClaimEvidenceExtractionService | None = None,
        evidence_service: EvidenceExtractionService | None = None,
        claim_validator: ClaimBundleValidator | None = None,
        evidence_planner: EvidenceMutationPlanner | None = None,
        fact_conflict_resolution_service: FactConflictResolutionService | None = None,
        evidence_invalidation_planner: EvidenceInvalidationPlanner | None = None,
    ) -> None:
        self.metadata_store = metadata_store
        self.backend_driver = backend_driver
        self.snapshot_producer = snapshot_producer or CodebaseMemorySnapshotProducer()
        self.git_metadata = git_metadata or GitMetadata()
        self.planner = planner or DeterministicMutationPlanner()
        self.observation_planner = observation_planner or ObservationMutationPlanner()
        self.claim_extraction_service = claim_extraction_service or evidence_service or (
            ClaimEvidenceExtractionService(llm_client=llm_client) if llm_client is not None else None
        )
        self.claim_validator = claim_validator or ClaimBundleValidator()
        self.evidence_planner = evidence_planner or EvidenceMutationPlanner()
        self.fact_conflict_resolution_service = fact_conflict_resolution_service or (
            FactConflictResolutionService(llm_client=llm_client) if llm_client is not None else None
        )
        self.evidence_invalidation_planner = evidence_invalidation_planner or EvidenceInvalidationPlanner(
            backend_driver=backend_driver,
            evidence_planner=self.evidence_planner,
            fact_conflict_resolution_service=self.fact_conflict_resolution_service,
        )
        self.writer = writer or TemporalGraphWriter(
            metadata_store=metadata_store,
            backend_driver=backend_driver,
            planner=self.planner,
        )

    def apply_latest_commit(
        self,
        repo_path: str,
        *,
        repo_id: str | None = None,
        branch_hint: str | None = None,
        limit: int = 1000000,
        execute: bool = False,
    ) -> CommitIndexResult:
        """Index HEAD or replay missing first-parent commits in order."""

        resolved_repo_path = self.git_metadata.resolve_repo_root(repo_path)
        resolved_repo_id = repo_id or default_repo_id(resolved_repo_path)
        current_state = self.metadata_store.load_repo_index_state(resolved_repo_id)
        previous_commit_sha = current_state.latest_commit_sha if current_state else None
        if not self.git_metadata.has_new_commit(
            resolved_repo_path,
            previous_commit_sha,
        ):
            head_commit = self.git_metadata.get_commit_info(
                resolved_repo_path,
                branch_hint=branch_hint,
            )
            return CommitIndexResult(
                repo_id=resolved_repo_id,
                repo_path=resolved_repo_path,
                commit=head_commit,
                previous_commit_sha=previous_commit_sha,
                branch=head_commit.branch,
                skipped=True,
                skip_reason="head commit already indexed",
                snapshot=None,
                apply_result=None,
                operations=MutationPlan(),
                applied_commit_shas=(),
            )

        head_commit = self.git_metadata.get_commit_info(
            resolved_repo_path,
            branch_hint=branch_hint,
        )
        self._ensure_supported_lineage(
            resolved_repo_path,
            previous_commit_sha=previous_commit_sha,
            previous_branch=current_state.latest_branch if current_state else None,
            target_commit=head_commit,
        )
        head_sha = head_commit.sha
        if previous_commit_sha is None:
            return self.apply_commit(
                resolved_repo_path,
                commit_sha=head_sha,
                repo_id=resolved_repo_id,
                branch_hint=branch_hint,
                limit=limit,
                execute=execute,
            )

        commits = self.git_metadata.list_first_parent_commits_between(
            resolved_repo_path,
            previous_commit_sha,
            head_sha,
        )
        if not commits:
            return self.apply_commit(
                resolved_repo_path,
                commit_sha=head_sha,
                repo_id=resolved_repo_id,
                branch_hint=branch_hint,
                limit=limit,
                execute=execute,
            )

        # We replay missing commits sequentially so metadata state always matches
        # the graph state that the next diff is computed against.
        return self.apply_commit_sequence(
            resolved_repo_path,
            commit_shas=commits,
            repo_id=resolved_repo_id,
            branch_hint=branch_hint,
            limit=limit,
            execute=execute,
        )

    def apply_commit_sequence(
        self,
        repo_path: str,
        *,
        commit_shas: Sequence[str],
        repo_id: str | None = None,
        branch_hint: str | None = None,
        limit: int = 1000000,
        execute: bool = False,
    ) -> CommitIndexResult:
        """Replay an explicit chronological sequence of commits.

        This helper is used by hosted indexing when a caller asks for a target
        commit plus a short ancestor window. Each commit is still indexed via
        ``apply_commit`` so metadata state and temporal diffs remain consistent
        between steps.
        """

        if not commit_shas:
            raise ValueError("commit_shas must not be empty")
        results = [
            self.apply_commit(
                repo_path,
                commit_sha=commit_sha,
                repo_id=repo_id,
                branch_hint=branch_hint,
                limit=limit,
                execute=execute,
            )
            for commit_sha in commit_shas
        ]
        if len(results) == 1:
            return results[0]
        return self._aggregate_commit_results(results)

    def apply_commit_with_history(
        self,
        repo_path: str,
        *,
        commit_sha: str,
        repo_id: str | None = None,
        branch_hint: str | None = None,
        limit: int = 1000000,
        execute: bool = False,
        history_window: int = 1,
    ) -> CommitIndexResult:
        """Index a target commit plus a bounded ancestor window when unseeded.

        Hosted indexing wants friendlier bootstrap behavior than bare
        ``apply_commit``: if a repository has never been indexed, we backfill a
        small first-parent window ending at the target commit. Once metadata is
        seeded, we go back to the normal deterministic replay rule and index
        only the missing first-parent commits from the last indexed commit.
        """

        resolved_repo_path = self.git_metadata.resolve_repo_root(repo_path)
        resolved_repo_id = repo_id or default_repo_id(resolved_repo_path)
        current_state = self.metadata_store.load_repo_index_state(resolved_repo_id)
        previous_commit_sha = current_state.latest_commit_sha if current_state else None

        if previous_commit_sha is not None:
            if previous_commit_sha == commit_sha:
                return self.apply_commit(
                    resolved_repo_path,
                    commit_sha=commit_sha,
                    repo_id=resolved_repo_id,
                    branch_hint=branch_hint,
                    limit=limit,
                    execute=execute,
                )
            if self.git_metadata.is_first_parent_ancestor(
                resolved_repo_path,
                previous_commit_sha,
                commit_sha,
            ):
                commits = self.git_metadata.list_first_parent_commits_between(
                    resolved_repo_path,
                    previous_commit_sha,
                    commit_sha,
                )
                return self.apply_commit_sequence(
                    resolved_repo_path,
                    commit_shas=commits or (commit_sha,),
                    repo_id=resolved_repo_id,
                    branch_hint=branch_hint,
                    limit=limit,
                    execute=execute,
                )
            return self.apply_commit(
                resolved_repo_path,
                commit_sha=commit_sha,
                repo_id=resolved_repo_id,
                branch_hint=branch_hint,
                limit=limit,
                execute=execute,
            )

        window = self.git_metadata.list_commit_window(
            resolved_repo_path,
            commit_sha,
            max_count=max(1, history_window),
        )
        return self.apply_commit_sequence(
            resolved_repo_path,
            commit_shas=window,
            repo_id=resolved_repo_id,
            branch_hint=branch_hint,
            limit=limit,
            execute=execute,
        )

    def apply_commit(
        self,
        repo_path: str,
        *,
        commit_sha: str,
        repo_id: str | None = None,
        branch_hint: str | None = None,
        limit: int = 1000000,
        execute: bool = False,
    ) -> CommitIndexResult:
        """Index one explicit commit from a resolved git checkout."""

        resolved_repo_path = self.git_metadata.resolve_repo_root(repo_path)
        resolved_repo_id = repo_id or default_repo_id(resolved_repo_path)
        current_state = self.metadata_store.load_repo_index_state(resolved_repo_id)
        commit = self.git_metadata.get_commit_info(
            resolved_repo_path,
            commit_sha,
            branch_hint=branch_hint,
        )
        previous_commit_sha = current_state.latest_commit_sha if current_state else None
        if previous_commit_sha == commit.sha:
            return CommitIndexResult(
                repo_id=resolved_repo_id,
                repo_path=resolved_repo_path,
                commit=commit,
                previous_commit_sha=previous_commit_sha,
                branch=commit.branch,
                skipped=True,
                skip_reason="commit already indexed",
                snapshot=None,
                apply_result=None,
                operations=MutationPlan(),
                applied_commit_shas=(),
            )

        self._ensure_supported_lineage(
            resolved_repo_path,
            previous_commit_sha=previous_commit_sha,
            previous_branch=current_state.latest_branch if current_state else None,
            target_commit=commit,
        )

        with self.git_metadata.temporary_worktree(resolved_repo_path, commit.sha) as worktree_path:
            snapshot = self.snapshot_producer.build_snapshot(
                worktree_path,
                repo_id=resolved_repo_id,
                revision_id=commit.sha,
                limit=limit,
            )

        normalized_snapshot = normalize_snapshot_for_commit(
            snapshot,
            repo_path=resolved_repo_path,
            commit=commit,
        )
        structural_plan = self.writer.plan_snapshot(normalized_snapshot)
        commit_plan = self.planner.plan_commit_provenance(
            snapshot=structural_plan.snapshot,
            diff=structural_plan.diff,
            commit=commit,
            planning_state=structural_plan.state,
        )
        planned_observation = self.observation_planner.build_commit_message_observation(
            repo_id=structural_plan.snapshot.repo_id,
            commit=commit,
        )
        observation_plan = (
            self.observation_planner.plan_observations(planned_observation)
            if planned_observation is not None
            else MutationPlan()
        )
        evidence_plan, claim_counts = self._plan_commit_evidence(
            planned_observation=planned_observation,
            snapshot=structural_plan.snapshot,
            commit=commit,
        )
        all_operations = self.planner.merge_plans(
            structural_plan.plan,
            commit_plan,
            observation_plan,
            evidence_plan,
        )
        apply_result = self.writer.apply_planned_snapshot(
            structural_plan.snapshot,
            structural_plan.diff,
            all_operations,
            execute=execute,
        )
        self.metadata_store.save_repo_index_state(
            RepoIndexState(
                repo_id=structural_plan.snapshot.repo_id,
                root_path=structural_plan.snapshot.root_path,
                latest_revision_id=structural_plan.snapshot.revision_id,
                latest_indexed_at=structural_plan.snapshot.generated_at,
                metadata=dict(structural_plan.snapshot.metadata),
                latest_commit_sha=commit.sha,
                latest_branch=commit.branch,
                last_commit_indexed_at=structural_plan.snapshot.generated_at,
            )
        )
        return CommitIndexResult(
            repo_id=resolved_repo_id,
            repo_path=resolved_repo_path,
            commit=commit,
            previous_commit_sha=previous_commit_sha,
            branch=commit.branch,
            skipped=False,
            skip_reason=None,
            snapshot=structural_plan.snapshot,
            apply_result=apply_result,
            operations=all_operations,
            applied_commit_shas=(commit.sha,),
            observation_operations=observation_plan,
            evidence_operations=evidence_plan,
            claim_candidates_extracted=claim_counts["extracted"],
            claim_candidates_validated=claim_counts["validated"],
            claim_candidates_rejected=claim_counts["rejected"],
            claim_rejection_reasons=dict(claim_counts["reasons"]),
            evidence_invalidated=claim_counts["invalidated"],
        )

    def _ensure_supported_lineage(
        self,
        repo_path: str,
        *,
        previous_commit_sha: str | None,
        previous_branch: str | None,
        target_commit: CommitInfo,
    ) -> None:
        """Guard against branch switches and off-chain first-parent rewrites."""

        if previous_commit_sha is None or previous_commit_sha == target_commit.sha:
            return

        if previous_branch and target_commit.branch and previous_branch != target_commit.branch:
            raise RuntimeError(
                "commit indexing does not support switching branches yet: "
                f"last indexed branch was {previous_branch!r}, "
                f"but the current checkout is on {target_commit.branch!r}"
            )

        if not self.git_metadata.is_first_parent_ancestor(
            repo_path,
            previous_commit_sha,
            target_commit.sha,
        ):
            raise RuntimeError(
                "commit indexing only supports first-parent commits on the tracked branch: "
                f"last indexed commit {previous_commit_sha} is not on the target commit {target_commit.sha} "
                "first-parent chain; reset or reseed this repo id before continuing"
            )

    def _aggregate_commit_results(
        self,
        results: Sequence[CommitIndexResult],
    ) -> CommitIndexResult:
        """Collapse a sequential replay into one summary result."""

        if not results:
            raise ValueError("results must not be empty")
        if len(results) == 1:
            return results[0]

        first = results[0]
        last = results[-1]
        total_emitted = sum(
            result.apply_result.operations_emitted for result in results if result.apply_result
        )
        total_executed = sum(
            result.apply_result.operations_applied for result in results if result.apply_result
        )
        aggregate_apply = None
        if last.apply_result is not None:
            aggregate_apply = SnapshotApplyResult(
                snapshot=last.apply_result.snapshot,
                diff=last.apply_result.diff,
                operations_emitted=total_emitted,
                operations_applied=total_executed,
            )
        return CommitIndexResult(
            repo_id=last.repo_id,
            repo_path=last.repo_path,
            commit=last.commit,
            previous_commit_sha=first.previous_commit_sha,
            branch=last.branch,
            skipped=all(result.skipped for result in results),
            skip_reason=last.skip_reason if all(result.skipped for result in results) else None,
            snapshot=last.snapshot,
            apply_result=aggregate_apply,
            operations=self.planner.merge_plans(*(result.operations for result in results)),
            applied_commit_shas=tuple(
                sha
                for result in results
                for sha in result.applied_commit_shas
            ),
            observation_operations=merge_mutation_plans(
                *(result.observation_operations for result in results)
            ),
            evidence_operations=merge_mutation_plans(
                *(result.evidence_operations for result in results)
            ),
            claim_candidates_extracted=sum(result.claim_candidates_extracted for result in results),
            claim_candidates_validated=sum(result.claim_candidates_validated for result in results),
            claim_candidates_rejected=sum(result.claim_candidates_rejected for result in results),
            claim_rejection_reasons=_merge_reason_counts(
                *(result.claim_rejection_reasons for result in results)
            ),
            evidence_invalidated=sum(result.evidence_invalidated for result in results),
        )

    def _plan_commit_evidence(
        self,
        *,
        planned_observation,
        snapshot: GraphSnapshot,
        commit: CommitInfo,
    ) -> tuple[MutationPlan, dict[str, object]]:
        zero_counts: dict[str, object] = {
            "extracted": 0,
            "validated": 0,
            "rejected": 0,
            "reasons": {},
            "invalidated": 0,
        }
        if planned_observation is None or self.claim_extraction_service is None:
            return MutationPlan(), zero_counts
        try:
            extraction = self.claim_extraction_service.extract(
                planned_observation.observation,
                source_context={
                    "source_kind": planned_observation.source.source_kind,
                    "source_record_type": planned_observation.source.source_record_type,
                    "source_id": planned_observation.source.source_id,
                    "source_display_name": planned_observation.source.display_name,
                    "commit_sha": commit.sha,
                    "branch": commit.branch,
                    "changed_files": list(commit.changed_files),
                    "parent_shas": list(commit.parent_shas),
                },
            )
        except Exception:
            return MutationPlan(), zero_counts
        validation = self.claim_validator.validate(
            observation=planned_observation.observation,
            extraction=extraction,
            snapshot=snapshot,
        )
        invalidation_result = self.evidence_invalidation_planner.plan_validated_claims(
            observation=planned_observation.observation,
            validation=validation,
            snapshot=snapshot,
        )
        return (
            invalidation_result.plan,
            {
                "extracted": validation.extracted_count,
                "validated": validation.validated_count,
                "rejected": validation.rejected_count,
                "reasons": validation.rejection_reason_counts,
                "invalidated": invalidation_result.invalidated_count,
            },
        )


def _merge_reason_counts(*reason_maps: dict[str, int]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reason_map in reason_maps:
        for key, value in reason_map.items():
            counts[key] = counts.get(key, 0) + value
    return counts
