"""Typed deterministic mutation planning for code graph + commit provenance.

This module is the replacement for the deterministic parts of `graph_sinks`.
It translates snapshot diffs and commit metadata into provider-neutral backend
records, while keeping the temporal logic explicit and inspectable.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from context_graph.ingestion.git.git_metadata import CommitInfo
from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot, SnapshotDiff
from context_graph.storage.backend import (
    EdgeRecord,
    merge_mutation_plans,
    MutationPlan,
    NodeRecord,
    RecordKind,
    SearchDocumentRecord,
)

_STATE_METADATA_KEY = "deterministic_mutation_state"
_SLUG_RE = re.compile(r"[^a-zA-Z0-9]+")


@dataclass(frozen=True, slots=True)
class DeterministicPlanningState:
    """Temporal state derived while planning one deterministic snapshot apply."""

    active_node_versions: Mapping[str, str]
    active_edge_versions: Mapping[str, str]
    closed_node_versions: Mapping[str, str]
    closed_edge_versions: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class DeterministicSnapshotPlan:
    """The structural mutation plan for one snapshot revision."""

    snapshot: GraphSnapshot
    diff: SnapshotDiff
    plan: MutationPlan
    state: DeterministicPlanningState


class DeterministicMutationPlanner:
    """Translate deterministic snapshot state into backend mutation records."""

    def plan_snapshot_diff(
        self,
        *,
        previous_snapshot: GraphSnapshot | None,
        snapshot: GraphSnapshot,
        diff: SnapshotDiff,
    ) -> DeterministicSnapshotPlan:
        """Plan structural writes for one snapshot diff.

        The planner keeps temporal validity out of provider-specific Cypher by
        carrying active version record ids forward in snapshot metadata. When a
        later revision changes or removes a node/edge, we can close the right
        version record with a normal upsert instead of a backend-specific query.
        """

        previous_state = _state_from_snapshot(previous_snapshot)
        active_node_versions = dict(previous_state.active_node_versions)
        active_edge_versions = dict(previous_state.active_edge_versions)
        closed_node_versions: dict[str, str] = {}
        closed_edge_versions: dict[str, str] = {}

        nodes: list[NodeRecord] = []
        edges: list[EdgeRecord] = []
        search_documents: list[SearchDocumentRecord] = []

        project_node = _require_project_node(snapshot)
        nodes.append(_project_record(snapshot=snapshot, project_node=project_node))
        search_documents.append(_project_search_document(snapshot=snapshot, project_node=project_node))

        removed_node_ids = sorted(diff.removed_nodes)
        changed_node_ids = sorted(diff.changed_nodes)
        for node_id in removed_node_ids + changed_node_ids:
            active_version_id = active_node_versions.pop(node_id, None)
            if active_version_id is None:
                continue
            closed_node_versions[node_id] = active_version_id
            edges.append(
                _has_version_record(
                    repo_id=snapshot.repo_id,
                    node_id=node_id,
                    version_id=active_version_id,
                    valid_to_revision=snapshot.revision_id,
                )
            )

        for node in _sorted_nodes(diff.removed_nodes.values()):
            nodes.append(_removed_code_node_record(snapshot=snapshot, node=node))
            search_documents.append(_code_node_search_document(snapshot=snapshot, node=node, active=False))

        for node in _sorted_nodes(diff.added_nodes.values()):
            if node.kind == "Project":
                continue
            version_id = _version_record_id(node.node_id, snapshot.revision_id)
            active_node_versions[node.node_id] = version_id
            nodes.extend(
                (
                    _code_node_record(snapshot=snapshot, node=node),
                    _code_node_version_record(snapshot=snapshot, node=node, version_id=version_id),
                )
            )
            edges.append(
                _has_version_record(
                    repo_id=snapshot.repo_id,
                    node_id=node.node_id,
                    version_id=version_id,
                    valid_from_revision=snapshot.revision_id,
                )
            )
            search_documents.append(_code_node_search_document(snapshot=snapshot, node=node, active=True))

        for change in _sorted_node_changes(diff.changed_nodes.values()):
            node = change.new
            version_id = _version_record_id(node.node_id, snapshot.revision_id)
            active_node_versions[node.node_id] = version_id
            nodes.extend(
                (
                    _code_node_record(snapshot=snapshot, node=node),
                    _code_node_version_record(snapshot=snapshot, node=node, version_id=version_id),
                )
            )
            edges.append(
                _has_version_record(
                    repo_id=snapshot.repo_id,
                    node_id=node.node_id,
                    version_id=version_id,
                    valid_from_revision=snapshot.revision_id,
                )
            )
            search_documents.append(_code_node_search_document(snapshot=snapshot, node=node, active=True))

        removed_edge_ids = sorted(diff.removed_edges)
        changed_edge_ids = sorted(diff.changed_edges)
        for edge_id in removed_edge_ids + changed_edge_ids:
            active_record_id = active_edge_versions.pop(edge_id, None)
            if active_record_id is None:
                continue
            closed_edge_versions[edge_id] = active_record_id
            old_edge = diff.removed_edges.get(edge_id)
            if old_edge is None:
                old_edge = diff.changed_edges[edge_id].old
            edges.append(
                _code_edge_record(
                    record_id=active_record_id,
                    repo_id=snapshot.repo_id,
                    edge=old_edge,
                    valid_to_revision=snapshot.revision_id,
                )
            )

        for edge in _sorted_edges(diff.added_edges.values()):
            edge_record_id = _edge_version_record_id(edge.edge_id, snapshot.revision_id)
            active_edge_versions[edge.edge_id] = edge_record_id
            edges.append(
                _code_edge_record(
                    record_id=edge_record_id,
                    repo_id=snapshot.repo_id,
                    edge=edge,
                    valid_from_revision=snapshot.revision_id,
                )
            )

        for change in _sorted_edge_changes(diff.changed_edges.values()):
            edge = change.new
            edge_record_id = _edge_version_record_id(edge.edge_id, snapshot.revision_id)
            active_edge_versions[edge.edge_id] = edge_record_id
            edges.append(
                _code_edge_record(
                    record_id=edge_record_id,
                    repo_id=snapshot.repo_id,
                    edge=edge,
                    valid_from_revision=snapshot.revision_id,
                )
            )

        state = DeterministicPlanningState(
            active_node_versions=active_node_versions,
            active_edge_versions=active_edge_versions,
            closed_node_versions=closed_node_versions,
            closed_edge_versions=closed_edge_versions,
        )
        enriched_snapshot = _snapshot_with_state(snapshot, state)
        return DeterministicSnapshotPlan(
            snapshot=enriched_snapshot,
            diff=diff,
            plan=MutationPlan(
                upsert_nodes=tuple(nodes),
                upsert_edges=tuple(edges),
                upsert_search_documents=tuple(search_documents),
            ),
            state=state,
        )

    def plan_commit_provenance(
        self,
        *,
        snapshot: GraphSnapshot,
        diff: SnapshotDiff,
        commit: CommitInfo,
        planning_state: DeterministicPlanningState,
    ) -> MutationPlan:
        """Plan deterministic commit provenance writes for one indexed revision."""

        project_node = _require_project_node(snapshot)
        commit_record_id = _commit_record_id(snapshot.repo_id, commit.sha)
        commit_nodes = [
            _commit_record(
                repo_id=snapshot.repo_id,
                commit=commit,
                placeholder=False,
            )
        ]
        search_documents = [_commit_search_document(repo_id=snapshot.repo_id, commit=commit)]
        edges = [
            EdgeRecord(
                record_id=f"has_commit:{project_node.node_id}:{commit_record_id}",
                record_type="has_commit",
                repo_id=snapshot.repo_id,
                group_id=snapshot.repo_id,
                relationship_type="HAS_COMMIT",
                source_record_id=project_node.node_id,
                target_record_id=commit_record_id,
            )
        ]

        for parent_sha in commit.parent_shas:
            parent_record_id = _commit_record_id(snapshot.repo_id, parent_sha)
            commit_nodes.append(
                _commit_record(
                    repo_id=snapshot.repo_id,
                    commit=CommitInfo(
                        sha=parent_sha,
                        parent_shas=(),
                        branch=None,
                        author_name="",
                        author_email="",
                        authored_at="",
                        committed_at="",
                        subject="",
                        body="",
                        changed_files=(),
                    ),
                    placeholder=True,
                )
            )
            edges.append(
                EdgeRecord(
                    record_id=f"precedes:{parent_record_id}:{commit_record_id}",
                    record_type="precedes",
                    repo_id=snapshot.repo_id,
                    group_id=snapshot.repo_id,
                    relationship_type="PRECEDES",
                    source_record_id=parent_record_id,
                    target_record_id=commit_record_id,
                )
            )

        for row in self._touched_node_rows(diff=diff, commit=commit):
            edges.append(
                EdgeRecord(
                    record_id=f"touched:{commit_record_id}:{row['node_id']}",
                    record_type="touched",
                    repo_id=snapshot.repo_id,
                    group_id=snapshot.repo_id,
                    relationship_type="TOUCHED",
                    source_record_id=commit_record_id,
                    target_record_id=row["node_id"],
                    properties={"change_kind": row["change_kind"]},
                )
            )

        for row in self._introduced_version_rows(
            diff=diff,
            commit=commit,
            active_node_versions=planning_state.active_node_versions,
        ):
            edges.append(
                EdgeRecord(
                    record_id=f"introduced_version:{commit_record_id}:{row['version_id']}",
                    record_type="introduced_version",
                    repo_id=snapshot.repo_id,
                    group_id=snapshot.repo_id,
                    relationship_type="INTRODUCED_VERSION",
                    source_record_id=commit_record_id,
                    target_record_id=row["version_id"],
                    properties={"change_kind": row["change_kind"]},
                )
            )

        for row in self._invalidated_version_rows(
            diff=diff,
            commit=commit,
            closed_node_versions=planning_state.closed_node_versions,
        ):
            edges.append(
                EdgeRecord(
                    record_id=f"invalidated_version:{commit_record_id}:{row['version_id']}",
                    record_type="invalidated_version",
                    repo_id=snapshot.repo_id,
                    group_id=snapshot.repo_id,
                    relationship_type="INVALIDATED_VERSION",
                    source_record_id=commit_record_id,
                    target_record_id=row["version_id"],
                    properties={"change_kind": row["change_kind"]},
                )
            )

        return MutationPlan(
            upsert_nodes=tuple(commit_nodes),
            upsert_edges=tuple(edges),
            upsert_search_documents=tuple(search_documents),
        )

    def merge_plans(self, *plans: MutationPlan) -> MutationPlan:
        """Merge mutation plans while deduplicating by record id."""

        return merge_mutation_plans(*plans)

    def _touched_node_rows(self, *, diff: SnapshotDiff, commit: CommitInfo) -> list[dict[str, Any]]:
        if diff.previous_revision_id is None:
            rows = [
                _touch_row(commit=commit, node=node, change_kind="changed")
                for node in diff.added_nodes.values()
                if node.kind != "Project" and node.file_path in commit.changed_files
            ]
            return sorted(rows, key=lambda row: row["node_id"])

        rows: dict[str, dict[str, Any]] = {}
        for node in diff.added_nodes.values():
            if node.kind == "Project":
                continue
            rows[node.node_id] = _touch_row(commit=commit, node=node, change_kind="added")
        for change in diff.changed_nodes.values():
            node = change.new
            if node.kind == "Project":
                continue
            rows[node.node_id] = _touch_row(commit=commit, node=node, change_kind="changed")
        for node in diff.removed_nodes.values():
            if node.kind == "Project":
                continue
            rows[node.node_id] = _touch_row(commit=commit, node=node, change_kind="removed")
        return sorted(rows.values(), key=lambda row: (row["change_kind"], row["node_id"]))

    def _introduced_version_rows(
        self,
        *,
        diff: SnapshotDiff,
        commit: CommitInfo,
        active_node_versions: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        if diff.previous_revision_id is None:
            touched_by_id = {
                row["node_id"]: row
                for row in self._touched_node_rows(diff=diff, commit=commit)
            }
            rows = [
                _version_provenance_row(
                    commit=commit,
                    node=node,
                    change_kind=touched_by_id[node.node_id]["change_kind"],
                    version_id=active_node_versions[node.node_id],
                )
                for node in diff.added_nodes.values()
                if node.kind != "Project"
                and node.node_id in touched_by_id
                and node.node_id in active_node_versions
            ]
            return sorted(rows, key=lambda row: row["node_id"])

        rows: dict[str, dict[str, Any]] = {}
        for node in diff.added_nodes.values():
            if node.kind == "Project" or node.node_id not in active_node_versions:
                continue
            rows[node.node_id] = _version_provenance_row(
                commit=commit,
                node=node,
                change_kind="added",
                version_id=active_node_versions[node.node_id],
            )
        for change in diff.changed_nodes.values():
            node = change.new
            if node.kind == "Project" or node.node_id not in active_node_versions:
                continue
            rows[node.node_id] = _version_provenance_row(
                commit=commit,
                node=node,
                change_kind="changed",
                version_id=active_node_versions[node.node_id],
            )
        return sorted(rows.values(), key=lambda row: (row["change_kind"], row["node_id"]))

    def _invalidated_version_rows(
        self,
        *,
        diff: SnapshotDiff,
        commit: CommitInfo,
        closed_node_versions: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        if diff.previous_revision_id is None:
            return []

        rows: dict[str, dict[str, Any]] = {}
        for change in diff.changed_nodes.values():
            node = change.old
            version_id = closed_node_versions.get(node.node_id)
            if node.kind == "Project" or version_id is None:
                continue
            rows[node.node_id] = _version_provenance_row(
                commit=commit,
                node=node,
                change_kind="changed",
                version_id=version_id,
            )
        for node in diff.removed_nodes.values():
            version_id = closed_node_versions.get(node.node_id)
            if node.kind == "Project" or version_id is None:
                continue
            rows[node.node_id] = _version_provenance_row(
                commit=commit,
                node=node,
                change_kind="removed",
                version_id=version_id,
            )
        return sorted(rows.values(), key=lambda row: (row["change_kind"], row["node_id"]))


def _require_project_node(snapshot: GraphSnapshot) -> CanonicalNode:
    project_node = next((node for node in snapshot.nodes if node.kind == "Project"), None)
    if project_node is None:
        raise ValueError("snapshot must contain a Project node")
    return project_node


def _state_from_snapshot(snapshot: GraphSnapshot | None) -> DeterministicPlanningState:
    raw = {}
    if snapshot is not None:
        raw = dict(snapshot.metadata.get(_STATE_METADATA_KEY) or {})
    return DeterministicPlanningState(
        active_node_versions={str(key): str(value) for key, value in dict(raw.get("active_node_versions") or {}).items()},
        active_edge_versions={str(key): str(value) for key, value in dict(raw.get("active_edge_versions") or {}).items()},
        closed_node_versions={},
        closed_edge_versions={},
    )


def _snapshot_with_state(snapshot: GraphSnapshot, state: DeterministicPlanningState) -> GraphSnapshot:
    metadata = dict(snapshot.metadata)
    metadata[_STATE_METADATA_KEY] = {
        "active_node_versions": dict(state.active_node_versions),
        "active_edge_versions": dict(state.active_edge_versions),
    }
    return GraphSnapshot(
        repo_id=snapshot.repo_id,
        revision_id=snapshot.revision_id,
        root_path=snapshot.root_path,
        generated_at=snapshot.generated_at,
        nodes=snapshot.nodes,
        edges=snapshot.edges,
        metadata=metadata,
    )


def _project_record(*, snapshot: GraphSnapshot, project_node: CanonicalNode) -> NodeRecord:
    return NodeRecord(
        record_id=project_node.node_id,
        record_type="project",
        repo_id=snapshot.repo_id,
        group_id=snapshot.repo_id,
        labels=("BackendRecord", "CodeNode", "Project"),
        properties={
            "node_id": project_node.node_id,
            "kind": "Project",
            "name": project_node.name,
            "qualified_name": project_node.qualified_name,
            "root_path": snapshot.root_path,
            "active": True,
            "last_seen_revision": snapshot.revision_id,
            "latest_revision_id": snapshot.revision_id,
        },
    )


def _code_node_record(*, snapshot: GraphSnapshot, node: CanonicalNode) -> NodeRecord:
    return NodeRecord(
        record_id=node.node_id,
        record_type="code_node",
        repo_id=snapshot.repo_id,
        group_id=snapshot.repo_id,
        labels=("BackendRecord", "CodeNode", node.kind),
        properties={
            "node_id": node.node_id,
            "kind": node.kind,
            "name": node.name,
            "qualified_name": node.qualified_name,
            "file_path": node.file_path,
            "start_line": node.start_line,
            "end_line": node.end_line,
            "properties_json": node.properties_json(),
            "active": True,
            "last_seen_revision": snapshot.revision_id,
        },
    )


def _removed_code_node_record(*, snapshot: GraphSnapshot, node: CanonicalNode) -> NodeRecord:
    return NodeRecord(
        record_id=node.node_id,
        record_type="code_node",
        repo_id=snapshot.repo_id,
        group_id=snapshot.repo_id,
        labels=("BackendRecord", "CodeNode", node.kind),
        properties={
            "node_id": node.node_id,
            "kind": node.kind,
            "name": node.name,
            "qualified_name": node.qualified_name,
            "file_path": node.file_path,
            "start_line": node.start_line,
            "end_line": node.end_line,
            "properties_json": node.properties_json(),
            "active": False,
            "last_seen_revision": snapshot.revision_id,
        },
    )


def _code_node_version_record(
    *,
    snapshot: GraphSnapshot,
    node: CanonicalNode,
    version_id: str,
) -> NodeRecord:
    return NodeRecord(
        record_id=version_id,
        record_type="code_node_version",
        repo_id=snapshot.repo_id,
        group_id=snapshot.repo_id,
        labels=("BackendRecord", "CodeNodeVersion"),
        properties={
            "version_id": version_id,
            "node_id": node.node_id,
            "revision_id": snapshot.revision_id,
            "kind": node.kind,
            "name": node.name,
            "qualified_name": node.qualified_name,
            "file_path": node.file_path,
            "start_line": node.start_line,
            "end_line": node.end_line,
            "properties_json": node.properties_json(),
        },
    )


def _has_version_record(
    *,
    repo_id: str,
    node_id: str,
    version_id: str,
    valid_from_revision: str | None = None,
    valid_to_revision: str | None = None,
) -> EdgeRecord:
    properties: dict[str, Any] = {
        "stable_node_id": node_id,
        "version_id": version_id,
    }
    if valid_from_revision is not None:
        properties["valid_from_revision"] = valid_from_revision
    if valid_to_revision is not None:
        properties["valid_to_revision"] = valid_to_revision
    return EdgeRecord(
        record_id=f"has_version:{node_id}:{version_id}",
        record_type="has_version",
        repo_id=repo_id,
        group_id=repo_id,
        relationship_type="HAS_VERSION",
        source_record_id=node_id,
        target_record_id=version_id,
        properties=properties,
    )


def _code_edge_record(
    *,
    record_id: str,
    repo_id: str,
    edge: CanonicalEdge,
    valid_from_revision: str | None = None,
    valid_to_revision: str | None = None,
) -> EdgeRecord:
    properties: dict[str, Any] = {
        "stable_edge_id": edge.edge_id,
        "edge_type": edge.edge_type,
        "properties_json": edge.properties_json(),
    }
    if valid_from_revision is not None:
        properties["valid_from_revision"] = valid_from_revision
    if valid_to_revision is not None:
        properties["valid_to_revision"] = valid_to_revision
    return EdgeRecord(
        record_id=record_id,
        record_type=f"code_edge_{_slug(edge.edge_type)}",
        repo_id=repo_id,
        group_id=repo_id,
        relationship_type=edge.edge_type,
        source_record_id=edge.source_id,
        target_record_id=edge.target_id,
        properties=properties,
    )


def _project_search_document(*, snapshot: GraphSnapshot, project_node: CanonicalNode) -> SearchDocumentRecord:
    return SearchDocumentRecord(
        record_kind=RecordKind.NODE,
        record_id=project_node.node_id,
        record_type="project",
        repo_id=snapshot.repo_id,
        group_id=snapshot.repo_id,
        search_text=_search_text(project_node.name, project_node.qualified_name, snapshot.root_path),
        properties={
            "kind": "Project",
            "active": True,
            "root_path": snapshot.root_path,
            "qualified_name": project_node.qualified_name,
        },
    )


def _code_node_search_document(
    *,
    snapshot: GraphSnapshot,
    node: CanonicalNode,
    active: bool,
) -> SearchDocumentRecord:
    return SearchDocumentRecord(
        record_kind=RecordKind.NODE,
        record_id=node.node_id,
        record_type="code_node",
        repo_id=snapshot.repo_id,
        group_id=snapshot.repo_id,
        search_text=_search_text(node.kind, node.name, node.qualified_name, node.file_path),
        properties={
            "kind": node.kind,
            "active": active,
            "file_path": node.file_path,
            "qualified_name": node.qualified_name,
        },
    )


def _commit_record(*, repo_id: str, commit: CommitInfo, placeholder: bool) -> NodeRecord:
    record_id = _commit_record_id(repo_id, commit.sha)
    return NodeRecord(
        record_id=record_id,
        record_type="commit",
        repo_id=repo_id,
        group_id=repo_id,
        labels=("BackendRecord", "Commit"),
        properties={
            "node_id": record_id,
            "sha": commit.sha,
            "branch": commit.branch,
            "author_name": commit.author_name,
            "authored_at": commit.authored_at,
            "committed_at": commit.committed_at,
            "subject": commit.subject,
            "placeholder": placeholder,
        },
    )


def _commit_search_document(*, repo_id: str, commit: CommitInfo) -> SearchDocumentRecord:
    record_id = _commit_record_id(repo_id, commit.sha)
    return SearchDocumentRecord(
        record_kind=RecordKind.NODE,
        record_id=record_id,
        record_type="commit",
        repo_id=repo_id,
        group_id=repo_id,
        search_text=_search_text(commit.sha, commit.subject, commit.author_name, commit.branch),
        properties={
            "sha": commit.sha,
            "branch": commit.branch,
            "author_name": commit.author_name,
            "placeholder": False,
        },
    )


def _touch_row(*, commit: CommitInfo, node: CanonicalNode, change_kind: str) -> dict[str, Any]:
    return {
        "repo_id": node.repo_id,
        "commit_sha": commit.sha,
        "node_id": node.node_id,
        "change_kind": change_kind,
    }


def _version_provenance_row(
    *,
    commit: CommitInfo,
    node: CanonicalNode,
    change_kind: str,
    version_id: str,
) -> dict[str, Any]:
    row = _touch_row(commit=commit, node=node, change_kind=change_kind)
    row["version_id"] = version_id
    return row


def _commit_record_id(repo_id: str, sha: str) -> str:
    return f"commit:{repo_id}:{sha}"


def _version_record_id(node_id: str, revision_id: str) -> str:
    return f"{node_id}@{revision_id}"


def _edge_version_record_id(edge_id: str, revision_id: str) -> str:
    return f"{edge_id}@{revision_id}"


def _search_text(*parts: Any) -> str:
    return " ".join(part for part in (_normalized_text(value) for value in parts) if part)


def _normalized_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _slug(value: str) -> str:
    normalized = _SLUG_RE.sub("_", value).strip("_").lower()
    return normalized or "record"


def _sorted_nodes(nodes: Any) -> list[CanonicalNode]:
    return sorted(nodes, key=lambda node: node.node_id)


def _sorted_edges(edges: Any) -> list[CanonicalEdge]:
    return sorted(edges, key=lambda edge: edge.edge_id)


def _sorted_node_changes(changes: Any) -> list[Any]:
    return sorted(changes, key=lambda change: change.new.node_id)


def _sorted_edge_changes(changes: Any) -> list[Any]:
    return sorted(changes, key=lambda change: change.new.edge_id)
