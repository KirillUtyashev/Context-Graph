from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional, Sequence, TypeVar

from .episodes import ContextNodeRef, EpisodeRecord, FactInvalidation, FactRecord, MentionRecord
from .git_metadata import CommitInfo
from .models import CanonicalEdge, CanonicalNode, GraphSnapshot, SnapshotDiff

if TYPE_CHECKING:
    from .github_indexer import GitHubProvenanceBundle


REL_TYPE_RE = re.compile(r"[^A-Z0-9_]")
NODE_LABEL_RE = re.compile(r"[^A-Za-z0-9_]")
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CypherStatement:
    query: str
    parameters: Mapping[str, Any]


def _sanitize_relationship_type(edge_type: str) -> str:
    normalized = REL_TYPE_RE.sub("_", edge_type.upper())
    normalized = normalized.strip("_")
    if not normalized:
        raise ValueError(f"invalid relationship type: {edge_type!r}")
    if normalized[0].isdigit():
        normalized = f"R_{normalized}"
    return normalized


def _sanitize_node_label(kind: str) -> str:
    normalized = NODE_LABEL_RE.sub("_", kind).strip("_")
    if not normalized:
        raise ValueError(f"invalid node label: {kind!r}")
    if normalized[0].isdigit():
        normalized = f"N_{normalized}"
    return normalized


def _chunked(items: Iterable[T], size: int) -> Iterable[list[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _node_row(node: CanonicalNode, revision_id: str) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "version_id": f"{node.node_id}@{revision_id}",
        "repo_id": node.repo_id,
        "revision_id": revision_id,
        "kind": node.kind,
        "name": node.name,
        "qualified_name": node.qualified_name,
        "file_path": node.file_path,
        "start_line": node.start_line,
        "end_line": node.end_line,
        "properties_json": node.properties_json(),
    }


def _edge_row(edge: CanonicalEdge, revision_id: str) -> dict[str, Any]:
    return {
        "edge_id": edge.edge_id,
        "version_edge_id": f"{edge.edge_id}@{revision_id}",
        "repo_id": edge.repo_id,
        "revision_id": revision_id,
        "edge_kind": "CodeEdge",
        "edge_type": edge.edge_type,
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "properties_json": edge.properties_json(),
    }


def _json_list(values: Sequence[str]) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _touch_row(
    *,
    commit: CommitInfo,
    node: CanonicalNode,
    change_kind: str,
) -> dict[str, Any]:
    return {
        "repo_id": node.repo_id,
        "commit_sha": commit.sha,
        "node_id": node.node_id,
        "change_kind": change_kind,
        "node_kind": node.kind,
        "qualified_name": node.qualified_name,
        "file_path": node.file_path,
    }


def _version_provenance_row(
    *,
    commit: CommitInfo,
    node: CanonicalNode,
    change_kind: str,
    version_id: str | None = None,
) -> dict[str, Any]:
    row = _touch_row(commit=commit, node=node, change_kind=change_kind)
    if version_id is not None:
        row["version_id"] = version_id
    return row


def _external_entity_row(entity: Any) -> dict[str, Any]:
    return {
        "node_id": entity.node_id,
        "repo_id": entity.repo_id,
        "github_repo": entity.github_repo,
        "kind": entity.kind,
        "external_id": entity.external_id,
        "number": entity.number,
        "url": entity.url,
        "title": entity.title,
        "state": entity.state,
        "author_login": entity.author_login,
        "created_at": entity.created_at,
        "updated_at": entity.updated_at,
        "metadata_json": json.dumps(entity.metadata, separators=(",", ":"), sort_keys=True),
        "raw_json": json.dumps(entity.raw, separators=(",", ":"), sort_keys=True),
    }


def _external_link_row(repo_id: str, link: Any) -> dict[str, Any]:
    return {
        "repo_id": repo_id,
        "target_node_id": link.target_node_id,
        "source_node_id": getattr(link, "source_node_id", None),
        "commit_sha": getattr(link, "commit_sha", None),
        "metadata_json": json.dumps(link.metadata, separators=(",", ":"), sort_keys=True),
    }


def _episode_row(episode: EpisodeRecord) -> dict[str, Any]:
    return {
        "node_id": episode.node_id,
        "episode_id": episode.episode_id,
        "repo_id": episode.repo_id,
        "source_type": episode.source_type,
        "source_id": episode.source_id,
        "author": episode.author,
        "created_at": episode.created_at,
        "updated_at": episode.updated_at,
        "text": episode.text,
        "metadata_json": episode.metadata_json(),
    }


def _context_node_row(node: ContextNodeRef) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "repo_id": node.repo_id,
        "kind": node.kind,
        "name": node.name,
        "qualified_name": node.qualified_name,
        "file_path": node.file_path,
        "aliases_json": json.dumps(list(node.aliases), separators=(",", ":")),
        "metadata_json": json.dumps(dict(node.metadata), separators=(",", ":"), sort_keys=True),
    }


def _mention_row(mention: MentionRecord) -> dict[str, Any]:
    return {
        "repo_id": mention.repo_id,
        "episode_node_id": mention.episode_node_id,
        "target_node_id": mention.target_node_id,
        "metadata_json": mention.metadata_json(),
    }


def _fact_row(fact: FactRecord) -> dict[str, Any]:
    return {
        "repo_id": fact.repo_id,
        "fact_id": fact.fact_id,
        "source_node_id": fact.source_node_id,
        "target_node_id": fact.target_node_id,
        "relation_type": fact.relation_type,
        "fact": fact.fact,
        "episode_ids_json": json.dumps(list(fact.episode_ids), separators=(",", ":")),
        "created_at": fact.created_at,
        "valid_at": fact.valid_at,
        "invalid_at": fact.invalid_at,
        "expired_at": fact.expired_at,
        "metadata_json": fact.metadata_json(),
    }


def _fact_invalidation_row(invalidation: FactInvalidation) -> dict[str, Any]:
    return {
        "repo_id": invalidation.repo_id,
        "source_node_id": invalidation.source_node_id,
        "target_node_id": invalidation.target_node_id,
        "relation_type": invalidation.relation_type,
        "observed_at": invalidation.observed_at,
        "replacement_fact_id": invalidation.replacement_fact_id,
        "replacement_valid_at": invalidation.replacement_valid_at,
    }


class BaseCypherGraphSink:
    def __init__(self, *, batch_size: int = 250) -> None:
        self.batch_size = batch_size

    def plan_apply_diff(
        self,
        snapshot: GraphSnapshot,
        diff: SnapshotDiff,
        *,
        observed_at: Optional[str] = None,
    ) -> list[CypherStatement]:
        observed_at = observed_at or datetime.now(timezone.utc).isoformat()
        project_node = next(
            (node for node in snapshot.nodes if node.kind == "Project"),
            None,
        )
        if project_node is None:
            raise ValueError("snapshot must contain a Project node")
        statements: list[CypherStatement] = [
            CypherStatement(
                query=(
                    "MERGE (project:CodeNode:Project:ContextNode {node_id: $node_id}) "
                    "SET project.repo_id = $repo_id, "
                    "    project.kind = 'Project', "
                    "    project.name = $name, "
                    "    project.qualified_name = $qualified_name, "
                    "    project.root_path = $root_path, "
                    "    project.active = true, "
                    "    project.latest_revision_id = $revision_id, "
                    "    project.updated_at = $observed_at"
                ),
                parameters={
                    "node_id": project_node.node_id,
                    "repo_id": snapshot.repo_id,
                    "name": project_node.name,
                    "qualified_name": project_node.qualified_name,
                    "root_path": snapshot.root_path,
                    "revision_id": snapshot.revision_id,
                    "observed_at": observed_at,
                },
            )
        ]

        closed_node_ids = sorted(set(diff.removed_nodes) | set(diff.changed_nodes))
        if closed_node_ids:
            for batch in _chunked(closed_node_ids, self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            "UNWIND $node_ids AS node_id "
                            "MATCH (n:CodeNode {node_id: node_id})-[r:HAS_VERSION]->(:CodeNodeVersion) "
                            "WHERE r.valid_to_revision IS NULL "
                            "SET r.valid_to_revision = $revision_id, "
                            "    r.invalidated_at = $observed_at"
                        ),
                        parameters={
                            "node_ids": batch,
                            "revision_id": snapshot.revision_id,
                            "observed_at": observed_at,
                        },
                    )
                )

        removed_node_ids = sorted(diff.removed_nodes)
        if removed_node_ids:
            for batch in _chunked(removed_node_ids, self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            "UNWIND $node_ids AS node_id "
                            "MATCH (n:CodeNode {node_id: node_id}) "
                            "SET n.active = false, "
                            "    n.last_seen_revision = $revision_id, "
                            "    n.removed_at = $observed_at"
                        ),
                        parameters={
                            "node_ids": batch,
                            "revision_id": snapshot.revision_id,
                            "observed_at": observed_at,
                        },
                    )
                )

        changed_nodes = list(diff.added_nodes.values()) + [
            change.new for change in diff.changed_nodes.values()
        ]
        by_kind: dict[str, list[CanonicalNode]] = {}
        for node in changed_nodes:
            by_kind.setdefault(node.kind, []).append(node)

        for kind, nodes in sorted(by_kind.items()):
            label = _sanitize_node_label(kind)
            for batch in _chunked((_node_row(node, snapshot.revision_id) for node in nodes), self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            f"UNWIND $rows AS row "
                            f"MERGE (n:CodeNode:{label}:ContextNode {{node_id: row.node_id}}) "
                            "SET n.repo_id = row.repo_id, "
                            "    n.kind = row.kind, "
                            "    n.name = row.name, "
                            "    n.qualified_name = row.qualified_name, "
                            "    n.file_path = row.file_path, "
                            "    n.start_line = row.start_line, "
                            "    n.end_line = row.end_line, "
                            "    n.properties_json = row.properties_json, "
                            "    n.active = true, "
                            "    n.last_seen_revision = row.revision_id, "
                            "    n.updated_at = $observed_at"
                        ),
                        parameters={"rows": batch, "observed_at": observed_at},
                    )
                )
                statements.append(
                    CypherStatement(
                        query=(
                            "UNWIND $rows AS row "
                            "MATCH (n:CodeNode {node_id: row.node_id}) "
                            "CREATE (v:CodeNodeVersion {"
                            "  version_id: row.version_id, "
                            "  repo_id: row.repo_id, "
                            "  revision_id: row.revision_id, "
                            "  kind: row.kind, "
                            "  name: row.name, "
                            "  qualified_name: row.qualified_name, "
                            "  file_path: row.file_path, "
                            "  start_line: row.start_line, "
                            "  end_line: row.end_line, "
                            "  properties_json: row.properties_json, "
                            "  observed_at: $observed_at"
                            "}) "
                            "CREATE (n)-[:HAS_VERSION {"
                            "  stable_node_id: row.node_id, "
                            "  version_id: row.version_id, "
                            "  valid_from_revision: row.revision_id, "
                            "  valid_to_revision: NULL, "
                            "  observed_at: $observed_at"
                            "}]->(v)"
                        ),
                        parameters={"rows": batch, "observed_at": observed_at},
                    )
                )

        closed_edge_ids = sorted(set(diff.removed_edges) | set(diff.changed_edges))
        if closed_edge_ids:
            for batch in _chunked(closed_edge_ids, self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            "UNWIND $edge_ids AS edge_id "
                            "MATCH ()-[r]->() "
                            "WHERE r.edge_kind = 'CodeEdge' "
                            "  AND r.stable_edge_id = edge_id "
                            "  AND r.valid_to_revision IS NULL "
                            "SET r.valid_to_revision = $revision_id, "
                            "    r.invalidated_at = $observed_at"
                        ),
                        parameters={
                            "edge_ids": batch,
                            "revision_id": snapshot.revision_id,
                            "observed_at": observed_at,
                        },
                    )
                )

        changed_edges = list(diff.added_edges.values()) + [
            change.new for change in diff.changed_edges.values()
        ]
        by_type: dict[str, list[CanonicalEdge]] = {}
        for edge in changed_edges:
            by_type.setdefault(edge.edge_type, []).append(edge)

        for edge_type, edges in sorted(by_type.items()):
            rel_type = _sanitize_relationship_type(edge_type)
            for batch in _chunked((_edge_row(edge, snapshot.revision_id) for edge in edges), self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            f"UNWIND $rows AS row "
                            f"MATCH (s:CodeNode {{node_id: row.source_id}}) "
                            f"MATCH (t:CodeNode {{node_id: row.target_id}}) "
                            f"CREATE (s)-[r:{rel_type} {{"
                            "  edge_kind: row.edge_kind, "
                            "  stable_edge_id: row.edge_id, "
                            "  version_edge_id: row.version_edge_id, "
                            "  repo_id: row.repo_id, "
                            "  edge_type: row.edge_type, "
                            "  valid_from_revision: row.revision_id, "
                            "  valid_to_revision: NULL, "
                            "  observed_at: $observed_at, "
                            "  properties_json: row.properties_json"
                            "}]->(t)"
                        ),
                        parameters={"rows": batch, "observed_at": observed_at},
                    )
                )

        return statements

    def plan_attach_commit(
        self,
        snapshot: GraphSnapshot,
        diff: SnapshotDiff,
        *,
        commit: CommitInfo,
        observed_at: Optional[str] = None,
    ) -> list[CypherStatement]:
        observed_at = observed_at or datetime.now(timezone.utc).isoformat()
        project_node = next(
            (node for node in snapshot.nodes if node.kind == "Project"),
            None,
        )
        if project_node is None:
            raise ValueError("snapshot must contain a Project node")

        statements: list[CypherStatement] = [
            CypherStatement(
                query=(
                    "MERGE (commit:Commit:ContextNode {repo_id: $repo_id, sha: $sha}) "
                    "SET commit.node_id = $node_id, "
                    "    commit.branch = $branch, "
                    "    commit.author_name = $author_name, "
                    "    commit.author_email = $author_email, "
                    "    commit.authored_at = $authored_at, "
                    "    commit.committed_at = $committed_at, "
                    "    commit.subject = $subject, "
                    "    commit.body = $body, "
                    "    commit.parent_shas_json = $parent_shas_json, "
                    "    commit.changed_files_json = $changed_files_json, "
                    "    commit.placeholder = false, "
                    "    commit.updated_at = $observed_at"
                ),
                parameters={
                    "repo_id": snapshot.repo_id,
                    "sha": commit.sha,
                    "node_id": f"commit:{snapshot.repo_id}:{commit.sha}",
                    "branch": commit.branch,
                    "author_name": commit.author_name,
                    "author_email": commit.author_email,
                    "authored_at": commit.authored_at,
                    "committed_at": commit.committed_at,
                    "subject": commit.subject,
                    "body": commit.body,
                    "parent_shas_json": _json_list(commit.parent_shas),
                    "changed_files_json": _json_list(commit.changed_files),
                    "observed_at": observed_at,
                },
            ),
            CypherStatement(
                query=(
                    "MATCH (project:CodeNode:Project {node_id: $project_node_id}) "
                    "MATCH (commit:Commit {repo_id: $repo_id, sha: $sha}) "
                    "MERGE (project)-[r:HAS_COMMIT]->(commit) "
                    "SET r.observed_at = $observed_at"
                ),
                parameters={
                    "project_node_id": project_node.node_id,
                    "repo_id": snapshot.repo_id,
                    "sha": commit.sha,
                    "observed_at": observed_at,
                },
            ),
        ]

        if commit.parent_shas:
            parent_rows = [
                {
                    "repo_id": snapshot.repo_id,
                    "parent_sha": parent_sha,
                    "sha": commit.sha,
                }
                for parent_sha in commit.parent_shas
            ]
            statements.append(
                CypherStatement(
                        query=(
                            "UNWIND $rows AS row "
                        "MERGE (parent:Commit:ContextNode {repo_id: row.repo_id, sha: row.parent_sha}) "
                        "ON CREATE SET parent.placeholder = true, "
                        "              parent.node_id = 'commit:' + row.repo_id + ':' + row.parent_sha, "
                        "              parent.updated_at = $observed_at "
                        "SET parent.node_id = 'commit:' + row.repo_id + ':' + row.parent_sha, "
                        "    parent.updated_at = $observed_at "
                        "WITH row "
                        "MATCH (parent:Commit {repo_id: row.repo_id, sha: row.parent_sha}) "
                        "MATCH (commit:Commit {repo_id: row.repo_id, sha: row.sha}) "
                        "MERGE (parent)-[r:PRECEDES]->(commit) "
                        "SET r.observed_at = $observed_at"
                    ),
                    parameters={"rows": parent_rows, "observed_at": observed_at},
                )
            )

        touched_node_rows = self._touched_node_rows(diff=diff, commit=commit)
        if touched_node_rows:
            for batch in _chunked(touched_node_rows, self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            "UNWIND $rows AS row "
                            "MATCH (commit:Commit {repo_id: row.repo_id, sha: row.commit_sha}) "
                            "MATCH (node:CodeNode {node_id: row.node_id}) "
                            "MERGE (commit)-[r:TOUCHED {stable_node_id: row.node_id}]->(node) "
                            "SET r.change_kind = row.change_kind, "
                            "    r.node_kind = row.node_kind, "
                            "    r.file_path = row.file_path, "
                            "    r.qualified_name = row.qualified_name, "
                            "    r.observed_at = $observed_at"
                        ),
                        parameters={"rows": batch, "observed_at": observed_at},
                    )
                )

        touched_file_rows = self._touched_file_rows(snapshot=snapshot, diff=diff, commit=commit)
        if touched_file_rows:
            for batch in _chunked(touched_file_rows, self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            "UNWIND $rows AS row "
                            "MATCH (commit:Commit {repo_id: row.repo_id, sha: row.commit_sha}) "
                            "MATCH (file:CodeNode:File {node_id: row.node_id}) "
                            "MERGE (commit)-[r:TOUCHED_FILE {stable_node_id: row.node_id}]->(file) "
                            "SET r.change_kind = row.change_kind, "
                            "    r.file_path = row.file_path, "
                            "    r.observed_at = $observed_at"
                        ),
                        parameters={"rows": batch, "observed_at": observed_at},
                    )
                )

        introduced_version_rows = self._introduced_version_rows(diff=diff, commit=commit)
        if introduced_version_rows:
            for batch in _chunked(introduced_version_rows, self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            "UNWIND $rows AS row "
                            "MATCH (commit:Commit {repo_id: row.repo_id, sha: row.commit_sha}) "
                            "MATCH (version:CodeNodeVersion {version_id: row.version_id}) "
                            "MERGE (commit)-[r:INTRODUCED_VERSION {version_id: row.version_id}]->(version) "
                            "SET r.stable_node_id = row.node_id, "
                            "    r.change_kind = row.change_kind, "
                            "    r.node_kind = row.node_kind, "
                            "    r.file_path = row.file_path, "
                            "    r.qualified_name = row.qualified_name, "
                            "    r.observed_at = $observed_at"
                        ),
                        parameters={"rows": batch, "observed_at": observed_at},
                    )
                )

        invalidated_version_rows = self._invalidated_version_rows(diff=diff, commit=commit)
        if invalidated_version_rows:
            for batch in _chunked(invalidated_version_rows, self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            "UNWIND $rows AS row "
                            "MATCH (commit:Commit {repo_id: row.repo_id, sha: row.commit_sha}) "
                            "MATCH (node:CodeNode {node_id: row.node_id})-[hv:HAS_VERSION]->(version:CodeNodeVersion) "
                            "WHERE hv.valid_to_revision = row.commit_sha "
                            "MERGE (commit)-[r:INVALIDATED_VERSION {version_id: hv.version_id}]->(version) "
                            "SET r.stable_node_id = row.node_id, "
                            "    r.change_kind = row.change_kind, "
                            "    r.node_kind = row.node_kind, "
                            "    r.file_path = row.file_path, "
                            "    r.qualified_name = row.qualified_name, "
                            "    r.observed_at = $observed_at"
                        ),
                        parameters={"rows": batch, "observed_at": observed_at},
                    )
                )

        return statements

    def plan_attach_github_provenance(
        self,
        bundle: "GitHubProvenanceBundle",
        *,
        observed_at: Optional[str] = None,
    ) -> list[CypherStatement]:
        observed_at = observed_at or datetime.now(timezone.utc).isoformat()
        statements: list[CypherStatement] = []

        by_kind: dict[str, list[Any]] = {}
        for entity in bundle.entities:
            by_kind.setdefault(entity.kind, []).append(entity)

        for kind, entities in sorted(by_kind.items()):
            label = _sanitize_node_label(kind)
            for batch in _chunked((_external_entity_row(entity) for entity in entities), self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            f"UNWIND $rows AS row "
                            f"MERGE (n:GitHubEntity:{label}:ContextNode {{node_id: row.node_id}}) "
                            "SET n.repo_id = row.repo_id, "
                            "    n.github_repo = row.github_repo, "
                            "    n.kind = row.kind, "
                            "    n.external_id = row.external_id, "
                            "    n.number = row.number, "
                            "    n.url = row.url, "
                            "    n.title = row.title, "
                            "    n.state = row.state, "
                            "    n.author_login = row.author_login, "
                            "    n.created_at = row.created_at, "
                            "    n.updated_at = row.updated_at, "
                            "    n.metadata_json = row.metadata_json, "
                            "    n.raw_json = row.raw_json, "
                            "    n.last_synced_at = $observed_at"
                        ),
                        parameters={"rows": batch, "observed_at": observed_at},
                    )
                )

        project_links_by_type: dict[str, list[Any]] = {}
        for link in bundle.project_links:
            project_links_by_type.setdefault(link.relation_type, []).append(link)
        for relation_type, links in sorted(project_links_by_type.items()):
            rel_type = _sanitize_relationship_type(relation_type)
            for batch in _chunked((_external_link_row(bundle.repo_id, link) for link in links), self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            "UNWIND $rows AS row "
                            "MATCH (project:CodeNode:Project {node_id: $project_node_id}) "
                            "MATCH (entity:GitHubEntity {node_id: row.target_node_id}) "
                            f"MERGE (project)-[r:{rel_type}]->(entity) "
                            "SET r.repo_id = row.repo_id, "
                            "    r.metadata_json = row.metadata_json, "
                            "    r.observed_at = $observed_at"
                        ),
                        parameters={
                            "rows": batch,
                            "project_node_id": f"project:{bundle.repo_id}",
                            "observed_at": observed_at,
                        },
                    )
                )

        commit_links_by_type: dict[str, list[Any]] = {}
        for link in bundle.commit_links:
            commit_links_by_type.setdefault(link.relation_type, []).append(link)
        for relation_type, links in sorted(commit_links_by_type.items()):
            rel_type = _sanitize_relationship_type(relation_type)
            for batch in _chunked((_external_link_row(bundle.repo_id, link) for link in links), self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            "UNWIND $rows AS row "
                            "MATCH (commit:Commit {repo_id: row.repo_id, sha: row.commit_sha}) "
                            "MATCH (entity:GitHubEntity {node_id: row.target_node_id}) "
                            f"MERGE (commit)-[r:{rel_type}]->(entity) "
                            "SET r.repo_id = row.repo_id, "
                            "    r.metadata_json = row.metadata_json, "
                            "    r.observed_at = $observed_at"
                        ),
                        parameters={"rows": batch, "observed_at": observed_at},
                    )
                )

        entity_links_by_type: dict[str, list[Any]] = {}
        for link in bundle.entity_links:
            entity_links_by_type.setdefault(link.relation_type, []).append(link)
        for relation_type, links in sorted(entity_links_by_type.items()):
            rel_type = _sanitize_relationship_type(relation_type)
            for batch in _chunked((_external_link_row(bundle.repo_id, link) for link in links), self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            "UNWIND $rows AS row "
                            "MATCH (source:GitHubEntity {node_id: row.source_node_id}) "
                            "MATCH (target:GitHubEntity {node_id: row.target_node_id}) "
                            f"MERGE (source)-[r:{rel_type}]->(target) "
                            "SET r.repo_id = row.repo_id, "
                            "    r.metadata_json = row.metadata_json, "
                            "    r.observed_at = $observed_at"
                        ),
                        parameters={"rows": batch, "observed_at": observed_at},
                    )
                )

        return statements

    def plan_upsert_episodes(
        self,
        episodes: Sequence[EpisodeRecord],
        *,
        observed_at: Optional[str] = None,
    ) -> list[CypherStatement]:
        if not episodes:
            return []
        observed_at = observed_at or datetime.now(timezone.utc).isoformat()
        statements: list[CypherStatement] = []
        for batch in _chunked((_episode_row(episode) for episode in episodes), self.batch_size):
            statements.append(
                CypherStatement(
                    query=(
                        "UNWIND $rows AS row "
                        "MERGE (e:Episode:ContextNode {node_id: row.node_id}) "
                        "SET e.episode_id = row.episode_id, "
                        "    e.repo_id = row.repo_id, "
                        "    e.source_type = row.source_type, "
                        "    e.source_id = row.source_id, "
                        "    e.author = row.author, "
                        "    e.created_at = row.created_at, "
                        "    e.updated_at = row.updated_at, "
                        "    e.text = row.text, "
                        "    e.metadata_json = row.metadata_json, "
                        "    e.last_seen_at = $observed_at"
                    ),
                    parameters={"rows": batch, "observed_at": observed_at},
                )
            )
        return statements

    def plan_attach_episode_parent(
        self,
        *,
        repo_id: str,
        episode_node_id: str,
        target_node_id: str,
        observed_at: Optional[str] = None,
    ) -> list[CypherStatement]:
        observed_at = observed_at or datetime.now(timezone.utc).isoformat()
        return [
            CypherStatement(
                query=(
                    "MATCH (episode:Episode {node_id: $episode_node_id}) "
                    "MATCH (target:ContextNode {node_id: $target_node_id}) "
                    "MERGE (episode)-[r:ON]->(target) "
                    "SET r.repo_id = $repo_id, "
                    "    r.observed_at = $observed_at"
                ),
                parameters={
                    "repo_id": repo_id,
                    "episode_node_id": episode_node_id,
                    "target_node_id": target_node_id,
                    "observed_at": observed_at,
                },
            )
        ]

    def plan_upsert_context_nodes(
        self,
        nodes: Sequence[ContextNodeRef],
        *,
        observed_at: Optional[str] = None,
    ) -> list[CypherStatement]:
        if not nodes:
            return []
        observed_at = observed_at or datetime.now(timezone.utc).isoformat()
        by_kind: dict[str, list[ContextNodeRef]] = {}
        for node in nodes:
            by_kind.setdefault(node.kind, []).append(node)
        statements: list[CypherStatement] = []
        for kind, grouped_nodes in sorted(by_kind.items()):
            label = _sanitize_node_label(kind)
            for batch in _chunked((_context_node_row(node) for node in grouped_nodes), self.batch_size):
                statements.append(
                    CypherStatement(
                        query=(
                            f"UNWIND $rows AS row "
                            f"MERGE (n:ContextNode:{label} {{node_id: row.node_id}}) "
                            "SET n.repo_id = row.repo_id, "
                            "    n.kind = row.kind, "
                            "    n.name = row.name, "
                            "    n.qualified_name = row.qualified_name, "
                            "    n.file_path = row.file_path, "
                            "    n.aliases_json = row.aliases_json, "
                            "    n.metadata_json = row.metadata_json, "
                            "    n.updated_at = $observed_at"
                        ),
                        parameters={"rows": batch, "observed_at": observed_at},
                    )
                )
        return statements

    def plan_attach_mentions(
        self,
        mentions: Sequence[MentionRecord],
        *,
        observed_at: Optional[str] = None,
    ) -> list[CypherStatement]:
        if not mentions:
            return []
        observed_at = observed_at or datetime.now(timezone.utc).isoformat()
        statements: list[CypherStatement] = []
        for batch in _chunked((_mention_row(mention) for mention in mentions), self.batch_size):
            statements.append(
                CypherStatement(
                    query=(
                        "UNWIND $rows AS row "
                        "MATCH (episode:Episode {node_id: row.episode_node_id}) "
                        "MATCH (target:ContextNode {node_id: row.target_node_id}) "
                        "MERGE (episode)-[r:MENTIONS {repo_id: row.repo_id, target_node_id: row.target_node_id}]->(target) "
                        "SET r.metadata_json = row.metadata_json, "
                        "    r.observed_at = $observed_at"
                    ),
                    parameters={"rows": batch, "observed_at": observed_at},
                )
            )
        return statements

    def plan_invalidate_fact_edges(
        self,
        invalidations: Sequence[FactInvalidation],
    ) -> list[CypherStatement]:
        if not invalidations:
            return []
        statements: list[CypherStatement] = []
        for batch in _chunked(
            (_fact_invalidation_row(invalidation) for invalidation in invalidations),
            self.batch_size,
        ):
            statements.append(
                CypherStatement(
                    query=(
                        "UNWIND $rows AS row "
                        "MATCH (source:ContextNode {node_id: row.source_node_id})-[r:RELATES_TO]->(target:ContextNode {node_id: row.target_node_id}) "
                        "WHERE r.repo_id = row.repo_id "
                        "  AND r.name = row.relation_type "
                        "  AND r.expired_at IS NULL "
                        "  AND (row.replacement_fact_id IS NULL OR r.fact_id <> row.replacement_fact_id) "
                        "SET r.expired_at = row.observed_at, "
                        "    r.invalid_at = coalesce(r.invalid_at, row.replacement_valid_at, row.observed_at)"
                    ),
                    parameters={"rows": batch},
                )
            )
        return statements

    def plan_apply_fact_edges(
        self,
        facts: Sequence[FactRecord],
        *,
        observed_at: Optional[str] = None,
    ) -> list[CypherStatement]:
        if not facts:
            return []
        observed_at = observed_at or datetime.now(timezone.utc).isoformat()
        statements: list[CypherStatement] = []
        for batch in _chunked((_fact_row(fact) for fact in facts), self.batch_size):
            statements.append(
                CypherStatement(
                    query=(
                        "UNWIND $rows AS row "
                        "MATCH (source:ContextNode {node_id: row.source_node_id}) "
                        "MATCH (target:ContextNode {node_id: row.target_node_id}) "
                        "MERGE (source)-[r:RELATES_TO {fact_id: row.fact_id}]->(target) "
                        "SET r.repo_id = row.repo_id, "
                        "    r.name = row.relation_type, "
                        "    r.fact = row.fact, "
                        "    r.episode_ids_json = row.episode_ids_json, "
                        "    r.created_at = row.created_at, "
                        "    r.valid_at = row.valid_at, "
                        "    r.invalid_at = row.invalid_at, "
                        "    r.expired_at = row.expired_at, "
                        "    r.metadata_json = row.metadata_json, "
                        "    r.updated_at = $observed_at"
                    ),
                    parameters={"rows": batch, "observed_at": observed_at},
                )
            )
        return statements

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

    def _touched_file_rows(
        self,
        *,
        snapshot: GraphSnapshot,
        diff: SnapshotDiff,
        commit: CommitInfo,
    ) -> list[dict[str, Any]]:
        if diff.previous_revision_id is None:
            current_by_path = {
                node.file_path: node
                for node in snapshot.nodes
                if node.kind == "File" and node.file_path
            }
            rows = [
                _touch_row(commit=commit, node=node, change_kind="changed")
                for file_path in commit.changed_files
                if (node := current_by_path.get(file_path)) is not None
            ]
            return sorted(rows, key=lambda row: row["node_id"])

        rows: dict[str, dict[str, Any]] = {}
        current_by_path = {
            node.file_path: node
            for node in snapshot.nodes
            if node.kind == "File" and node.file_path
        }
        removed_by_path = {
            node.file_path: node
            for node in diff.removed_nodes.values()
            if node.kind == "File" and node.file_path
        }

        for node in diff.added_nodes.values():
            if node.kind == "File":
                rows[node.node_id] = _touch_row(commit=commit, node=node, change_kind="added")
        for change in diff.changed_nodes.values():
            if change.new.kind == "File":
                rows[change.new.node_id] = _touch_row(
                    commit=commit,
                    node=change.new,
                    change_kind="changed",
                )
        for node in diff.removed_nodes.values():
            if node.kind == "File":
                rows[node.node_id] = _touch_row(commit=commit, node=node, change_kind="removed")

        for file_path in commit.changed_files:
            node = current_by_path.get(file_path) or removed_by_path.get(file_path)
            if node is None:
                continue
            rows.setdefault(
                node.node_id,
                _touch_row(commit=commit, node=node, change_kind="changed"),
            )

        return sorted(rows.values(), key=lambda row: (row["change_kind"], row["node_id"]))

    def _introduced_version_rows(self, *, diff: SnapshotDiff, commit: CommitInfo) -> list[dict[str, Any]]:
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
                    version_id=f"{node.node_id}@{commit.sha}",
                )
                for node in diff.added_nodes.values()
                if node.kind != "Project" and node.node_id in touched_by_id
            ]
            return sorted(rows, key=lambda row: row["node_id"])

        rows: dict[str, dict[str, Any]] = {}
        for node in diff.added_nodes.values():
            if node.kind == "Project":
                continue
            rows[node.node_id] = _version_provenance_row(
                commit=commit,
                node=node,
                change_kind="added",
                version_id=f"{node.node_id}@{commit.sha}",
            )
        for change in diff.changed_nodes.values():
            node = change.new
            if node.kind == "Project":
                continue
            rows[node.node_id] = _version_provenance_row(
                commit=commit,
                node=node,
                change_kind="changed",
                version_id=f"{node.node_id}@{commit.sha}",
            )
        return sorted(rows.values(), key=lambda row: (row["change_kind"], row["node_id"]))

    def _invalidated_version_rows(self, *, diff: SnapshotDiff, commit: CommitInfo) -> list[dict[str, Any]]:
        if diff.previous_revision_id is None:
            return []

        rows: dict[str, dict[str, Any]] = {}
        for change in diff.changed_nodes.values():
            node = change.old
            if node.kind == "Project":
                continue
            rows[node.node_id] = _version_provenance_row(
                commit=commit,
                node=node,
                change_kind="changed",
            )
        for node in diff.removed_nodes.values():
            if node.kind == "Project":
                continue
            rows[node.node_id] = _version_provenance_row(
                commit=commit,
                node=node,
                change_kind="removed",
            )
        return sorted(rows.values(), key=lambda row: (row["change_kind"], row["node_id"]))

    def plan_reset_repo(
        self,
        repo_id: str,
    ) -> list[CypherStatement]:
        return [
            CypherStatement(
                query=(
                    "MATCH (e:Episode {repo_id: $repo_id}) "
                    "DETACH DELETE e"
                ),
                parameters={"repo_id": repo_id},
            ),
            CypherStatement(
                query=(
                    "MATCH (g:GitHubEntity {repo_id: $repo_id}) "
                    "DETACH DELETE g"
                ),
                parameters={"repo_id": repo_id},
            ),
            CypherStatement(
                query=(
                    "MATCH (c:Commit {repo_id: $repo_id}) "
                    "DETACH DELETE c"
                ),
                parameters={"repo_id": repo_id},
            ),
            CypherStatement(
                query=(
                    "MATCH (v:CodeNodeVersion {repo_id: $repo_id}) "
                    "DETACH DELETE v"
                ),
                parameters={"repo_id": repo_id},
            ),
            CypherStatement(
                query=(
                    "MATCH (n:CodeNode {repo_id: $repo_id}) "
                    "DETACH DELETE n"
                ),
                parameters={"repo_id": repo_id},
            ),
            CypherStatement(
                query=(
                    "MATCH (n:ContextNode {repo_id: $repo_id}) "
                    "WHERE NOT n:Episode "
                    "  AND NOT n:GitHubEntity "
                    "  AND NOT n:Commit "
                    "  AND NOT n:CodeNode "
                    "DETACH DELETE n"
                ),
                parameters={"repo_id": repo_id},
            ),
        ]

    def validate_execute_config(self) -> None:
        return None

    def execute(self, statements: Iterable[CypherStatement]) -> int:
        raise NotImplementedError

    def search_context_nodes(
        self,
        *,
        repo_id: str,
        search_terms: Sequence[str],
        limit: int = 25,
    ) -> tuple[ContextNodeRef, ...]:
        _ = (repo_id, search_terms, limit)
        return ()


class Neo4jGraphSink(BaseCypherGraphSink):
    def __init__(
        self,
        *,
        uri: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        driver: Any = None,
        batch_size: int = 250,
    ) -> None:
        super().__init__(batch_size=batch_size)
        self.uri = uri
        self.username = username
        self.password = password
        self.database = database
        self.driver = driver

    def validate_execute_config(self) -> None:
        if self.driver is not None:
            return
        if not self.uri or self.username is None or self.password is None:
            raise RuntimeError(
                "Neo4j execution requires either an injected driver or "
                "--neo4j-uri/--neo4j-user/--neo4j-password "
                "(or NEO4J_URI/NEO4J_USERNAME or NEO4J_USER/NEO4J_PASSWORD)"
            )

    def execute(self, statements: Iterable[CypherStatement]) -> int:
        try:
            from neo4j import GraphDatabase  # type: ignore
        except ImportError as exc:
            raise RuntimeError("neo4j driver is required to execute Cypher statements") from exc

        driver = self.driver
        managed_driver = False
        if driver is None:
            if not self.uri or self.username is None or self.password is None:
                raise RuntimeError("Neo4jGraphSink requires either a driver or uri/username/password")
            driver = GraphDatabase.driver(self.uri, auth=(self.username, self.password))
            managed_driver = True

        count = 0
        session_kwargs = {"database": self.database} if self.database else {}
        try:
            with driver.session(**session_kwargs) as session:
                for statement in statements:
                    session.run(statement.query, statement.parameters).consume()
                    count += 1
        finally:
            if managed_driver:
                driver.close()
        return count

    def search_context_nodes(
        self,
        *,
        repo_id: str,
        search_terms: Sequence[str],
        limit: int = 25,
    ) -> tuple[ContextNodeRef, ...]:
        return _search_context_nodes_with_neo4j(
            driver=self.driver,
            uri=self.uri,
            username=self.username,
            password=self.password,
            database=self.database,
            repo_id=repo_id,
            search_terms=search_terms,
            limit=limit,
        )


class NeptuneGraphSink(BaseCypherGraphSink):
    def __init__(
        self,
        *,
        host: Optional[str] = None,
        port: int = 8182,
        driver: Any = None,
        encrypted: bool = True,
        batch_size: int = 250,
    ) -> None:
        super().__init__(batch_size=batch_size)
        self.host = host
        self.port = port
        self.driver = driver
        self.encrypted = encrypted

    def validate_execute_config(self) -> None:
        if self.driver is not None:
            return
        if not self.host:
            raise RuntimeError(
                "Neptune execution requires either an injected driver or "
                "--neptune-host (or CONTEXT_GRAPH_NEPTUNE_HOST)"
            )

    def execute(self, statements: Iterable[CypherStatement]) -> int:
        try:
            from neo4j import GraphDatabase  # type: ignore
        except ImportError as exc:
            raise RuntimeError("neo4j driver is required to execute Neptune Bolt statements") from exc

        driver = self.driver
        managed_driver = False
        if driver is None:
            if not self.host:
                raise RuntimeError("NeptuneGraphSink requires either a driver or a host")
            uri = f"bolt://{self.host}:{self.port}"
            driver = GraphDatabase.driver(uri, auth=("", ""), encrypted=self.encrypted)
            managed_driver = True

        count = 0
        try:
            with driver.session() as session:
                for statement in statements:
                    session.run(statement.query, statement.parameters).consume()
                    count += 1
        finally:
            if managed_driver:
                driver.close()
        return count

    def search_context_nodes(
        self,
        *,
        repo_id: str,
        search_terms: Sequence[str],
        limit: int = 25,
    ) -> tuple[ContextNodeRef, ...]:
        return _search_context_nodes_with_neo4j(
            driver=self.driver,
            uri=f"bolt://{self.host}:{self.port}" if self.host else None,
            username="",
            password="",
            database=None,
            repo_id=repo_id,
            search_terms=search_terms,
            limit=limit,
            encrypted=self.encrypted,
        )


def _search_context_nodes_with_neo4j(
    *,
    driver: Any,
    uri: str | None,
    username: str | None,
    password: str | None,
    database: str | None,
    repo_id: str,
    search_terms: Sequence[str],
    limit: int,
    encrypted: bool | None = None,
) -> tuple[ContextNodeRef, ...]:
    normalized_terms = tuple(dict.fromkeys(term.strip().lower() for term in search_terms if term.strip()))
    if not normalized_terms:
        return ()

    try:
        from neo4j import GraphDatabase  # type: ignore
    except ImportError:
        return ()

    managed_driver = False
    active_driver = driver
    if active_driver is None:
        if not uri:
            return ()
        auth = None
        if username is not None and password is not None:
            auth = (username, password)
        kwargs: dict[str, Any] = {}
        if encrypted is not None:
            kwargs["encrypted"] = encrypted
        active_driver = GraphDatabase.driver(uri, auth=auth, **kwargs)
        managed_driver = True

    query = (
        "MATCH (n:ContextNode {repo_id: $repo_id}) "
        "WHERE NOT n:Episode "
        "WITH n, "
        "     toLower(coalesce(n.name, '')) AS lname, "
        "     toLower(coalesce(n.qualified_name, '')) AS lqualified, "
        "     toLower(coalesce(n.file_path, '')) AS lfile "
        "WITH n, [term IN $terms WHERE "
        "           lname = term OR "
        "           lqualified = term OR "
        "           lfile = term OR "
        "           lname CONTAINS term OR "
        "           lqualified ENDS WITH '.' + term OR "
        "           lfile ENDS WITH '/' + term OR "
        "           lfile ENDS WITH term"
        "     ] AS matches "
        "WHERE size(matches) > 0 "
        "RETURN "
        "  n.node_id AS node_id, "
        "  n.repo_id AS repo_id, "
        "  n.kind AS kind, "
        "  n.name AS name, "
        "  n.qualified_name AS qualified_name, "
        "  n.file_path AS file_path, "
        "  n.metadata_json AS metadata_json, "
        "  n.aliases_json AS aliases_json, "
        "  matches AS matches "
        "ORDER BY size(matches) DESC, n.kind, coalesce(n.qualified_name, n.name) "
        "LIMIT $limit"
    )

    session_kwargs = {"database": database} if database else {}
    try:
        with active_driver.session(**session_kwargs) as session:
            records = session.run(
                query,
                {
                    "repo_id": repo_id,
                    "terms": list(normalized_terms),
                    "limit": limit,
                },
            )
            nodes = tuple(_context_node_from_record(record) for record in records)
    except Exception:
        nodes = ()
    finally:
        if managed_driver:
            active_driver.close()

    return nodes


def _context_node_from_record(record: Any) -> ContextNodeRef:
    metadata = _maybe_json_dict(record.get("metadata_json"))
    aliases_payload = _maybe_json_list(record.get("aliases_json"))
    return ContextNodeRef(
        node_id=str(record.get("node_id")),
        repo_id=str(record.get("repo_id")),
        kind=str(record.get("kind")),
        name=str(record.get("name")),
        qualified_name=record.get("qualified_name"),
        file_path=record.get("file_path"),
        aliases=tuple(str(item) for item in aliases_payload),
        metadata=metadata,
    )


def _maybe_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            return {}
    if isinstance(value, dict):
        return dict(value)
    return {}


def _maybe_json_list(value: Any) -> list[Any]:
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
            if isinstance(decoded, list):
                return decoded
        except json.JSONDecodeError:
            return []
    if isinstance(value, list):
        return list(value)
    return []
