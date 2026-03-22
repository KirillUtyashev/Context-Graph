from __future__ import annotations

from typing import Iterable

from .diffing import diff_snapshots
from .graph_sinks import BaseCypherGraphSink, CypherStatement
from .metadata import MetadataStore
from .models import GraphSnapshot, SnapshotApplyResult, SnapshotDiff


class TemporalGraphWriter:
    def __init__(self, *, metadata_store: MetadataStore, graph_sink: BaseCypherGraphSink) -> None:
        self.metadata_store = metadata_store
        self.graph_sink = graph_sink
        self.metadata_store.bootstrap()

    def plan_snapshot(self, snapshot: GraphSnapshot) -> tuple[SnapshotDiff, list[CypherStatement]]:
        previous = self.metadata_store.load_latest_snapshot(snapshot.repo_id)
        diff = diff_snapshots(previous, snapshot)
        statements = self.graph_sink.plan_apply_diff(snapshot, diff)
        return diff, statements

    def finalize_snapshot(self, snapshot: GraphSnapshot, diff: SnapshotDiff) -> None:
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
        statements: Iterable[CypherStatement],
        *,
        execute: bool = False,
    ) -> SnapshotApplyResult:
        materialized = list(statements)
        executed = self.graph_sink.execute(materialized) if execute else 0
        self.finalize_snapshot(snapshot, diff)
        return SnapshotApplyResult(
            snapshot=snapshot,
            diff=diff,
            statements_emitted=len(materialized),
            statements_executed=executed,
        )

    def apply_snapshot(
        self,
        snapshot: GraphSnapshot,
        *,
        execute: bool = False,
        extra_statements: Iterable[CypherStatement] = (),
    ) -> SnapshotApplyResult:
        diff, planned = self.plan_snapshot(snapshot)
        return self.apply_planned_snapshot(
            snapshot,
            diff,
            [*planned, *list(extra_statements)],
            execute=execute,
        )
