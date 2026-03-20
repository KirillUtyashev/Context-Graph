from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Iterable, Mapping, Optional, TypeVar

from .models import CanonicalEdge, CanonicalNode, GraphSnapshot, SnapshotDiff


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
        "edge_type": edge.edge_type,
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "properties_json": edge.properties_json(),
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
                    "MERGE (project:CodeNode:Project {node_id: $node_id}) "
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
                            f"MERGE (n:CodeNode:{label} {{node_id: row.node_id}}) "
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
                            "WHERE r.stable_edge_id = edge_id AND r.valid_to_revision IS NULL "
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

    def execute(self, statements: Iterable[CypherStatement]) -> int:
        raise NotImplementedError


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
