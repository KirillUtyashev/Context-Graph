from __future__ import annotations

from .diffing import diff_snapshots
from .deterministic_planner import (
    DeterministicMutationPlanner,
    DeterministicSnapshotPlan,
)
from ..metadata import MetadataStore
from ..models import GraphSnapshot, SnapshotApplyResult, SnapshotDiff
from ..storage.backend import BackendDriver, BackendSyncBridge, MutationPlan


class TemporalGraphWriter:
    """Plans and finalizes deterministic snapshot application."""

    def __init__(
        self,
        *,
        metadata_store: MetadataStore,
        backend_driver: BackendDriver | None,
        planner: DeterministicMutationPlanner | None = None,
    ) -> None:
        self.metadata_store = metadata_store
        self.backend_driver = backend_driver
        self.planner = planner or DeterministicMutationPlanner()
        self.sync_bridge = BackendSyncBridge(backend_driver) if backend_driver is not None else None
        self.metadata_store.bootstrap()

    def plan_snapshot(self, snapshot: GraphSnapshot) -> DeterministicSnapshotPlan:
        """Load the previous snapshot, diff it, and build a typed mutation plan."""

        previous = self.metadata_store.load_latest_snapshot(snapshot.repo_id)
        return self.plan_snapshot_against(previous_snapshot=previous, snapshot=snapshot)

    def plan_snapshot_against(
        self,
        *,
        previous_snapshot: GraphSnapshot | None,
        snapshot: GraphSnapshot,
    ) -> DeterministicSnapshotPlan:
        """Build a typed mutation plan against an explicit previous snapshot."""

        diff = diff_snapshots(previous_snapshot, snapshot)
        return self.planner.plan_snapshot_diff(
            previous_snapshot=previous_snapshot,
            snapshot=snapshot,
            diff=diff,
        )

    def finalize_snapshot(self, snapshot: GraphSnapshot, diff: SnapshotDiff) -> None:
        """Persist snapshot metadata after planning or applying a revision."""

        self.metadata_store.save_latest_snapshot(snapshot)
        self.metadata_store.record_index_run(
            repo_id=snapshot.repo_id,
            revision_id=snapshot.revision_id,
            status="applied",
            stats={
                "nodes": len(snapshot.nodes),
                "edges": len(snapshot.edges),
                "added_nodes": len(diff.added_nodes),
                "changed_nodes": len(diff.changed_nodes),
                "removed_nodes": len(diff.removed_nodes),
                "added_edges": len(diff.added_edges),
                "changed_edges": len(diff.changed_edges),
                "removed_edges": len(diff.removed_edges),
            },
        )

    def apply_planned_snapshot(
        self,
        snapshot: GraphSnapshot,
        diff: SnapshotDiff,
        operations: MutationPlan,
        *,
        execute: bool = False,
    ) -> SnapshotApplyResult:
        """Optionally apply typed mutations, then always finalize metadata state."""

        applied = 0
        if execute:
            if self.sync_bridge is None:
                raise RuntimeError("deterministic backend writes require a configured backend driver")
            self.sync_bridge.ensure_write_ready()
            applied = self.sync_bridge.apply(operations).operations_applied
        self.finalize_snapshot(snapshot, diff)
        return SnapshotApplyResult(
            snapshot=snapshot,
            diff=diff,
            operations_emitted=operations.operation_count(),
            operations_applied=applied,
        )
