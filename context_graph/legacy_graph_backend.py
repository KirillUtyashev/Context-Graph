from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import threading
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional, Sequence, TypeVar

from .episodes import (
    EntityNode,
    EpisodicNode,
    MentionRecord,
    entity_node_aliases,
    entity_node_file_path,
    entity_node_kind,
    entity_node_qualified_name,
    make_entity_node,
)
from context_graph.ingestion.git.git_metadata import CommitInfo
from .models import CanonicalEdge, CanonicalNode, GraphSnapshot, SnapshotDiff

if TYPE_CHECKING:
    from .embeddings import EmbeddingProvider
    from context_graph.ingestion.git.github_indexer import GitHubProvenanceBundle


REL_TYPE_RE = re.compile(r"[^A-Z0-9_]")
NODE_LABEL_RE = re.compile(r"[^A-Za-z0-9_]")
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CypherStatement:
    query: str
    parameters: Mapping[str, Any]


class BaseCypherExecutor(ABC):
    @abstractmethod
    def validate_execute_config(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def execute(self, statements: Iterable[CypherStatement]) -> int:
        raise NotImplementedError

    def search_context_nodes(
        self,
        *,
        repo_id: str,
        search_terms: Sequence[str],
        limit: int = 25,
    ) -> tuple[EntityNode, ...]:
        _ = (repo_id, search_terms, limit)
        return ()


class _PersistentLoop:
    """A single long-lived event loop running in a daemon thread.

    The neo4j async driver binds its connection pool to the loop that created
    it.  If we call ``asyncio.run()`` for every query we get a *new* loop each
    time, and the driver's internal futures end up attached to a dead loop —
    ``RuntimeError: … attached to a different loop``.

    This helper spins up one background loop on first use and keeps it alive
    for the lifetime of the process.  ``run(coro)`` schedules work on that
    loop from any thread and blocks until the result is ready.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure_running(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._loop.is_running():
            return self._loop
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, daemon=True, name="graphiti-neo4j-loop"
            )
            thread.start()
            self._loop = loop
            self._thread = thread
            return loop

    def run(self, coro: Any) -> Any:
        loop = self._ensure_running()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()


_persistent_loop = _PersistentLoop()


def _run_coroutine_sync(coro: Any) -> Any:
    return _persistent_loop.run(coro)


def _records_from_graphiti_result(result: Any) -> Sequence[Any]:
    if isinstance(result, tuple) and result:
        first = result[0]
        if isinstance(first, Sequence):
            return first
    records = getattr(result, "records", None)
    if records is not None:
        return records
    if isinstance(result, Sequence):
        return result
    return ()


class GraphitiNeo4jExecutor(BaseCypherExecutor):
    def __init__(
        self,
        *,
        uri: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        driver: Any = None,
        build_indices: bool = True,
    ) -> None:
        self.uri = uri
        self.username = username
        self.password = password
        self.database = database or "neo4j"
        self.driver = driver
        self.build_indices = build_indices
        self._indices_ready = False

    def validate_execute_config(self) -> None:
        if self.driver is not None:
            return
        if not self.uri or self.username is None or self.password is None:
            raise RuntimeError(
                "Graphiti-backed Neo4j execution requires "
                "--neo4j-uri/--neo4j-user/--neo4j-password "
                "(or NEO4J_URI/NEO4J_USERNAME or NEO4J_USER/NEO4J_PASSWORD)"
            )

    def _get_driver(self) -> Any:
        if self.driver is None:
            self.validate_execute_config()
            from context_graph.storage.graph import Neo4jDriver as GraphitiNeo4jDriver

            self.driver = GraphitiNeo4jDriver(
                uri=self.uri,
                user=self.username,
                password=self.password,
                database=self.database,
            )

        if self.build_indices and not self._indices_ready:
            _run_coroutine_sync(self.driver.build_indices_and_constraints())
            self._indices_ready = True
        return self.driver

    def execute(self, statements: Iterable[CypherStatement]) -> int:
        driver = self._get_driver()
        count = 0
        for statement in statements:
            _run_coroutine_sync(driver.execute_query(statement.query, params=dict(statement.parameters)))
            count += 1
        return count

    def search_context_nodes(
        self,
        *,
        repo_id: str,
        search_terms: Sequence[str],
        limit: int = 25,
    ) -> tuple[EntityNode, ...]:
        # If no credentials/driver configured, skip graph search gracefully
        # (planning-only mode with no live backend)
        if self.driver is None and (not self.uri or self.username is None or self.password is None):
            return ()

        normalized_terms = tuple(
            dict.fromkeys(term.strip().lower() for term in search_terms if term.strip())
        )
        if not normalized_terms:
            return ()

        driver = self._get_driver()
        query = (
            "MATCH (n {repo_id: $repo_id}) WHERE (n:Entity OR n:Episodic) "
            "  AND NOT n:Episode "
            "WITH n, "
            "     toLower(coalesce(n.name, '')) AS lname, "
            "     toLower(coalesce(n.title, '')) AS ltitle, "
            "     toLower(coalesce(n.qualified_name, '')) AS lqualified, "
            "     toLower(coalesce(n.file_path, '')) AS lfile, "
            "     toLower(coalesce(n.aliases_json, '')) AS laliases "
            "WITH n, [term IN $terms WHERE "
            "           lname = term OR "
            "           ltitle = term OR "
            "           lqualified = term OR "
            "           lfile = term OR "
            "           laliases CONTAINS '\"' + term + '\"' OR "
            "           lname CONTAINS term OR "
            "           ltitle CONTAINS term OR "
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
        result = _run_coroutine_sync(
            driver.execute_query(
                query,
                params={
                    "repo_id": repo_id,
                    "terms": list(normalized_terms),
                    "limit": limit,
                },
            )
        )
        records = _records_from_graphiti_result(result)
        return tuple(_context_node_from_record(record) for record in records)


class GraphitiNeptuneExecutor(BaseCypherExecutor):
    """Executor backed by Graphiti's vendored NeptuneDriver.

    Uses ``langchain_aws.NeptuneGraph`` / boto3 under the hood, which talks
    to Neptune via the HTTPS openCypher endpoint (NOT the Bolt protocol).
    """

    def __init__(
        self,
        *,
        host: Optional[str] = None,
        port: int = 8182,
        host_scheme: str = "neptune-db",
        aoss_host: Optional[str] = None,
        aoss_port: int = 443,
        driver: Any = None,
        build_indices: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.host_scheme = host_scheme
        self.aoss_host = aoss_host
        self.aoss_port = aoss_port
        self.driver = driver
        self.build_indices = build_indices
        self._indices_ready = False

    def validate_execute_config(self) -> None:
        if self.driver is not None:
            return
        if not self.host:
            raise RuntimeError(
                "Graphiti-backed Neptune execution requires "
                "--neptune-host (or CONTEXT_GRAPH_NEPTUNE_HOST)"
            )

    def _get_driver(self) -> Any:
        if self.driver is None:
            self.validate_execute_config()
            from context_graph.storage.graph import NeptuneDriver as GraphitiNeptuneDriver

            neptune_host = f"{self.host_scheme}://{self.host}"
            self.driver = GraphitiNeptuneDriver(
                host=neptune_host,
                port=self.port,
                aoss_host=self.aoss_host,
                aoss_port=self.aoss_port,
            )

        if self.build_indices and not self._indices_ready:
            _run_coroutine_sync(self.driver.build_indices_and_constraints())
            self._indices_ready = True
        return self.driver

    def execute(self, statements: Iterable[CypherStatement]) -> int:
        driver = self._get_driver()
        count = 0
        for statement in statements:
            _run_coroutine_sync(
                driver.execute_query(statement.query, params=dict(statement.parameters))
            )
            count += 1
        return count

    def search_context_nodes(
        self,
        *,
        repo_id: str,
        search_terms: Sequence[str],
        limit: int = 25,
    ) -> tuple[EntityNode, ...]:
        if self.driver is None and not self.host:
            return ()

        normalized_terms = tuple(
            dict.fromkeys(term.strip().lower() for term in search_terms if term.strip())
        )
        if not normalized_terms:
            return ()

        driver = self._get_driver()
        query = (
            "MATCH (n {repo_id: $repo_id}) WHERE (n:Entity OR n:Episodic) "
            "  AND NOT n:Episode "
            "WITH n, "
            "     toLower(coalesce(n.name, '')) AS lname, "
            "     toLower(coalesce(n.title, '')) AS ltitle, "
            "     toLower(coalesce(n.qualified_name, '')) AS lqualified, "
            "     toLower(coalesce(n.file_path, '')) AS lfile, "
            "     toLower(coalesce(n.aliases_json, '')) AS laliases "
            "WITH n, [term IN $terms WHERE "
            "           lname = term OR "
            "           ltitle = term OR "
            "           lqualified = term OR "
            "           lfile = term OR "
            "           laliases CONTAINS '\"' + term + '\"' OR "
            "           lname CONTAINS term OR "
            "           ltitle CONTAINS term OR "
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
        result = _run_coroutine_sync(
            driver.execute_query(
                query,
                params={
                    "repo_id": repo_id,
                    "terms": list(normalized_terms),
                    "limit": limit,
                },
            )
        )
        records = _records_from_graphiti_result(result)
        return tuple(_context_node_from_record(record) for record in records)


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


def _project_row(node: CanonicalNode, *, root_path: str, revision_id: str) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "repo_id": node.repo_id,
        "revision_id": revision_id,
        "kind": node.kind,
        "name": node.name,
        "qualified_name": node.qualified_name,
        "root_path": root_path,
    }


def _edge_row(edge: CanonicalEdge, revision_id: str) -> dict[str, Any]:
    return {
        "edge_id": edge.edge_id,
        "repo_id": edge.repo_id,
        "revision_id": revision_id,
        "edge_type": edge.edge_type,
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "properties_json": edge.properties_json(),
    }


def _json_list(values: Sequence[str]) -> str:
    return json.dumps(list(values), separators=(",", ":"))


def _normalized_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _summary_text(*parts: Any) -> str:
    return " ".join(
        normalized
        for normalized in (_normalized_text(part) for part in parts)
        if normalized
    )


def _aliases_from_row(row: Mapping[str, Any]) -> tuple[str, ...]:
    payload = row.get("aliases_json")
    if isinstance(payload, str):
        values = _maybe_json_list(payload)
        return tuple(_normalized_text(value) for value in values if _normalized_text(value))
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        return tuple(_normalized_text(value) for value in payload if _normalized_text(value))
    return ()


def _graphiti_entity_summary(row: Mapping[str, Any]) -> str:
    aliases = _aliases_from_row(row)
    alias_text = ""
    if aliases:
        alias_text = f"aliases {' '.join(aliases)}"
    return _summary_text(
        row.get("kind"),
        row.get("name"),
        f"qualified {row.get('qualified_name')}" if row.get("qualified_name") else "",
        f"file {row.get('file_path')}" if row.get("file_path") else "",
        f"root {row.get('root_path')}" if row.get("root_path") else "",
        alias_text,
    )


def _graphiti_commit_summary(row: Mapping[str, Any]) -> str:
    return _summary_text(
        row.get("kind"),
        row.get("name"),
        f"subject {row.get('subject')}" if row.get("subject") else "",
        f"branch {row.get('branch')}" if row.get("branch") else "",
        f"author {row.get('author_name')}" if row.get("author_name") else "",
    )


def _graphiti_external_entity_name(row: Mapping[str, Any]) -> str:
    title = _normalized_text(row.get("title"))
    if title:
        return title
    kind = _normalized_text(row.get("kind"))
    number = row.get("number")
    if kind and number is not None:
        return f"{kind} #{number}"
    return _normalized_text(row.get("external_id"))


def _graphiti_external_entity_summary(row: Mapping[str, Any]) -> str:
    return _summary_text(
        row.get("kind"),
        row.get("name"),
        f"number {row.get('number')}" if row.get("number") is not None else "",
        f"state {row.get('state')}" if row.get("state") else "",
        f"repo {row.get('github_repo')}" if row.get("github_repo") else "",
        f"url {row.get('url')}" if row.get("url") else "",
    )


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
    aliases: list[str] = []
    if entity.number is not None:
        aliases.append(f"#{entity.number}")
    return {
        "node_id": entity.node_id,
        "repo_id": entity.repo_id,
        "github_repo": entity.github_repo,
        "kind": entity.kind,
        "name": _normalized_text(entity.title) or f"{entity.kind} {entity.number or entity.external_id}",
        "external_id": entity.external_id,
        "number": entity.number,
        "url": entity.url,
        "title": entity.title,
        "state": entity.state,
        "author_login": entity.author_login,
        "created_at": entity.created_at,
        "updated_at": entity.updated_at,
        "aliases_json": json.dumps(aliases, separators=(",", ":")),
        "metadata_json": json.dumps(entity.metadata, separators=(",", ":"), sort_keys=True),
    }


def _external_link_row(repo_id: str, link: Any) -> dict[str, Any]:
    return {
        "repo_id": repo_id,
        "target_node_id": link.target_node_id,
        "source_node_id": getattr(link, "source_node_id", None),
        "commit_sha": getattr(link, "commit_sha", None),
    }


def _episode_row(episode: EpisodicNode) -> dict[str, Any]:
    return {
        "node_id": episode.uuid,
        "episode_id": episode.name,
        "repo_id": episode.group_id,
        "source_type": episode.source.value,
        "source_id": episode.source_description,
        "author": episode.author,
        "created_at": str(episode.created_at),
        "updated_at": str(episode.valid_at),
        "text": episode.content,
        "metadata_json": json.dumps(episode.episode_metadata, separators=(",", ":"), sort_keys=True),
    }


def _mention_row(mention: MentionRecord) -> dict[str, Any]:
    return {
        "repo_id": mention.repo_id,
        "episode_node_id": mention.episode_node_id,
        "target_node_id": mention.target_node_id,
        "metadata_json": mention.metadata_json(),
    }


class BaseCypherGraphSink:
    def __init__(
        self,
        *,
        batch_size: int = 250,
        executor: BaseCypherExecutor | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.batch_size = batch_size
        self.executor = executor
        self.embedding_provider = embedding_provider

    def _attach_embeddings(
        self,
        rows: Sequence[dict[str, Any]],
        *,
        text_key: str,
        embedding_key: str,
    ) -> list[dict[str, Any]]:
        materialized = [dict(row) for row in rows]
        for row in materialized:
            row.setdefault(embedding_key, None)

        if self.embedding_provider is None:
            return materialized

        positions: list[int] = []
        texts: list[str] = []
        for index, row in enumerate(materialized):
            text = _normalized_text(row.get(text_key))
            row[text_key] = text if text else row.get(text_key)
            if text:
                positions.append(index)
                texts.append(text)

        if not texts:
            return materialized

        embeddings = self.embedding_provider.embed_texts(texts)
        if len(embeddings) != len(texts):
            raise RuntimeError(
                f"embedding provider returned {len(embeddings)} vectors for {len(texts)} texts"
            )

        for index, embedding in zip(positions, embeddings, strict=True):
            materialized[index][embedding_key] = list(embedding)

        return materialized

    def _prepare_graphiti_entity_rows(
        self,
        rows: Sequence[dict[str, Any]],
        *,
        observed_at: str,
        summary_builder,
        name_builder=None,
        created_at_builder=None,
    ) -> list[dict[str, Any]]:
        # The helper name remains for compatibility, but ingestion-owned nodes no longer
        # receive Graphiti summary/embedding fields. We only normalize common scalar data.
        _ = summary_builder
        prepared: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            name = name_builder(item) if name_builder is not None else item.get("name")
            item["name"] = _normalized_text(name)
            created_at = created_at_builder(item, observed_at) if created_at_builder else observed_at
            item["created_at"] = _normalized_text(created_at) or observed_at
            prepared.append(item)
        return prepared

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
        project_row = self._prepare_graphiti_entity_rows(
            [
                _project_row(
                    project_node,
                    root_path=snapshot.root_path,
                    revision_id=snapshot.revision_id,
                )
            ],
            observed_at=observed_at,
            summary_builder=_graphiti_entity_summary,
        )[0]
        statements: list[CypherStatement] = [
            CypherStatement(
                query=(
                    "MERGE (project:CodeNode:Project:Entity {node_id: $node_id}) "
                    "ON CREATE SET project.created_at = $created_at "
                    "SET project.repo_id = $repo_id, "
                    "    project.kind = 'Project', "
                    "    project.name = $name, "
                    "    project.qualified_name = $qualified_name, "
                    "    project.root_path = $root_path, "
                    "    project.active = true, "
                    "    project.latest_revision_id = $revision_id"
                ),
                parameters=project_row,
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
                            "SET r.valid_to_revision = $revision_id"
                        ),
                        parameters={
                            "node_ids": batch,
                            "revision_id": snapshot.revision_id,
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
                enriched_batch = self._prepare_graphiti_entity_rows(
                    batch,
                    observed_at=observed_at,
                    summary_builder=_graphiti_entity_summary,
                )
                statements.append(
                    CypherStatement(
                        query=(
                            f"UNWIND $rows AS row "
                            f"MERGE (n:CodeNode:{label}:Entity {{node_id: row.node_id}}) "
                            "ON CREATE SET n.created_at = row.created_at "
                            "SET n.repo_id = row.repo_id, "
                            "    n.kind = row.kind, "
                            "    n.name = row.name, "
                            "    n.qualified_name = row.qualified_name, "
                            "    n.file_path = row.file_path, "
                            "    n.start_line = row.start_line, "
                            "    n.end_line = row.end_line, "
                            "    n.properties_json = row.properties_json, "
                            "    n.active = true, "
                            "    n.last_seen_revision = row.revision_id"
                        ),
                        parameters={"rows": enriched_batch},
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
                            "  properties_json: row.properties_json"
                            "}) "
                            "CREATE (n)-[:HAS_VERSION {"
                            "  valid_from_revision: row.revision_id, "
                            "  valid_to_revision: NULL"
                            "}]->(v)"
                        ),
                        parameters={"rows": enriched_batch},
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
                            "WHERE r.stable_edge_id = edge_id "
                            "  AND r.valid_to_revision IS NULL "
                            "SET r.valid_to_revision = $revision_id"
                        ),
                        parameters={
                            "edge_ids": batch,
                            "revision_id": snapshot.revision_id,
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
                            "  repo_id: row.repo_id, "
                            "  edge_type: row.edge_type, "
                            "  valid_from_revision: row.revision_id, "
                            "  valid_to_revision: NULL, "
                            "  properties_json: row.properties_json"
                            "}]->(t)"
                        ),
                        parameters={"rows": batch},
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
                    "MERGE (commit:Commit:Entity {repo_id: $repo_id, sha: $sha}) "
                    "ON CREATE SET commit.created_at = $created_at "
                    "SET commit.node_id = $node_id, "
                    "    commit.kind = 'Commit', "
                    "    commit.name = $name, "
                    "    commit.branch = $branch, "
                    "    commit.author_name = $author_name, "
                    "    commit.authored_at = $authored_at, "
                    "    commit.committed_at = $committed_at, "
                    "    commit.subject = $subject, "
                    "    commit.placeholder = false"
                ),
                parameters={
                    **self._prepare_graphiti_entity_rows(
                        [
                            {
                                "repo_id": snapshot.repo_id,
                                "sha": commit.sha,
                                "node_id": f"commit:{snapshot.repo_id}:{commit.sha}",
                                "name": commit.sha[:12],
                                "subject": commit.subject,
                                "branch": commit.branch,
                                "author_name": commit.author_name,
                                "authored_at": commit.authored_at,
                                "committed_at": commit.committed_at,
                            }
                        ],
                        observed_at=observed_at,
                        summary_builder=_graphiti_commit_summary,
                        created_at_builder=lambda row, fallback: (
                            row.get("committed_at") or row.get("authored_at") or fallback
                        ),
                    )[0],
                    "sha": commit.sha,
                    "branch": commit.branch,
                    "author_name": commit.author_name,
                    "authored_at": commit.authored_at,
                    "committed_at": commit.committed_at,
                    "subject": commit.subject,
                },
            ),
            CypherStatement(
                query=(
                    "MATCH (project:CodeNode:Project {node_id: $project_node_id}) "
                    "MATCH (commit:Commit {repo_id: $repo_id, sha: $sha}) "
                    "MERGE (project)-[:HAS_COMMIT]->(commit) "
                ),
                parameters={
                    "project_node_id": project_node.node_id,
                    "repo_id": snapshot.repo_id,
                    "sha": commit.sha,
                },
            ),
        ]

        if commit.parent_shas:
            parent_rows = self._prepare_graphiti_entity_rows(
                [
                    {
                        "repo_id": snapshot.repo_id,
                        "parent_sha": parent_sha,
                        "sha": commit.sha,
                        "node_id": f"commit:{snapshot.repo_id}:{parent_sha}",
                        "kind": "Commit",
                        "name": parent_sha[:12],
                    }
                    for parent_sha in commit.parent_shas
                ],
                observed_at=observed_at,
                summary_builder=_graphiti_commit_summary,
            )
            statements.append(
                CypherStatement(
                    query=(
                        "UNWIND $rows AS row "
                        "MERGE (parent:Commit:Entity {repo_id: row.repo_id, sha: row.parent_sha}) "
                        "ON CREATE SET parent.created_at = row.created_at, "
                        "              parent.placeholder = true "
                        "SET parent.node_id = row.node_id, "
                        "    parent.kind = 'Commit', "
                        "    parent.name = row.name, "
                        "WITH row "
                        "MATCH (parent:Commit {repo_id: row.repo_id, sha: row.parent_sha}) "
                        "MATCH (commit:Commit {repo_id: row.repo_id, sha: row.sha}) "
                        "MERGE (parent)-[:PRECEDES]->(commit) "
                    ),
                    parameters={"rows": parent_rows},
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
                            "MERGE (commit)-[r:TOUCHED]->(node) "
                            "SET r.change_kind = row.change_kind"
                        ),
                        parameters={"rows": batch},
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
                            "MERGE (commit)-[r:TOUCHED_FILE]->(file) "
                            "SET r.change_kind = row.change_kind"
                        ),
                        parameters={"rows": batch},
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
                            "MERGE (commit)-[r:INTRODUCED_VERSION]->(version) "
                            "SET r.change_kind = row.change_kind"
                        ),
                        parameters={"rows": batch},
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
                            "MERGE (commit)-[r:INVALIDATED_VERSION]->(version) "
                            "SET r.change_kind = row.change_kind"
                        ),
                        parameters={"rows": batch},
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
                enriched_batch = self._prepare_graphiti_entity_rows(
                    batch,
                    observed_at=observed_at,
                    summary_builder=_graphiti_external_entity_summary,
                    name_builder=_graphiti_external_entity_name,
                    created_at_builder=lambda row, fallback: row.get("created_at") or fallback,
                )
                statements.append(
                    CypherStatement(
                        query=(
                            f"UNWIND $rows AS row "
                            f"MERGE (n:GitHubEntity:{label}:Entity {{node_id: row.node_id}}) "
                            "ON CREATE SET n.created_at = row.created_at "
                            "SET n.repo_id = row.repo_id, "
                            "    n.github_repo = row.github_repo, "
                            "    n.kind = row.kind, "
                            "    n.name = row.name, "
                            "    n.aliases_json = row.aliases_json, "
                            "    n.external_id = row.external_id, "
                            "    n.number = row.number, "
                            "    n.url = row.url, "
                            "    n.title = row.title, "
                            "    n.state = row.state, "
                            "    n.author_login = row.author_login, "
                            "    n.updated_at = row.updated_at, "
                            "    n.metadata_json = row.metadata_json"
                        ),
                        parameters={"rows": enriched_batch},
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
                            f"MERGE (project)-[:{rel_type}]->(entity) "
                        ),
                        parameters={
                            "rows": batch,
                            "project_node_id": f"project:{bundle.repo_id}",
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
                            f"MERGE (commit)-[:{rel_type}]->(entity) "
                        ),
                        parameters={"rows": batch},
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
                            f"MERGE (source)-[:{rel_type}]->(target) "
                        ),
                        parameters={"rows": batch},
                    )
                )

        return statements

    def plan_upsert_episodes(
        self,
        episodes: Sequence[EpisodicNode],
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
                        "MERGE (e:Episode:Episodic {node_id: row.node_id}) "
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
                    "MATCH (target:Entity {node_id: $target_node_id}) "
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
        nodes: Sequence[EntityNode],
        *,
        observed_at: Optional[str] = None,
    ) -> list[CypherStatement]:
        # ContextNode is no longer part of the canonical ingestion graph.
        _ = (nodes, observed_at)
        return []

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
                        "MATCH (target:Entity {node_id: row.target_node_id}) "
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
        invalidations: Sequence[Any],
    ) -> list[CypherStatement]:
        # RELATES_TO invalidation is retired with the Graphiti-style fact model.
        _ = invalidations
        return []

    def plan_apply_fact_edges(
        self,
        facts: Sequence[Any],
        *,
        observed_at: Optional[str] = None,
    ) -> list[CypherStatement]:
        # RELATES_TO creation is retired with the Graphiti-style fact model.
        _ = (facts, observed_at)
        return []

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
        ]

    def validate_execute_config(self) -> None:
        if self.executor is not None:
            self.executor.validate_execute_config()
        return None

    def execute(self, statements: Iterable[CypherStatement]) -> int:
        if self.executor is None:
            raise NotImplementedError
        return self.executor.execute(statements)

    def search_context_nodes(
        self,
        *,
        repo_id: str,
        search_terms: Sequence[str],
        limit: int = 25,
    ) -> tuple[EntityNode, ...]:
        if self.executor is None:
            return ()
        return self.executor.search_context_nodes(
            repo_id=repo_id,
            search_terms=search_terms,
            limit=limit,
        )


class Neo4jGraphSink(BaseCypherGraphSink):
    def __init__(
        self,
        *,
        uri: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        database: Optional[str] = None,
        driver: Any = None,
        executor: BaseCypherExecutor | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        batch_size: int = 250,
    ) -> None:
        super().__init__(
            batch_size=batch_size,
            embedding_provider=embedding_provider,
            executor=executor
            or GraphitiNeo4jExecutor(
                uri=uri,
                username=username,
                password=password,
                database=database,
                driver=driver,
            ),
        )
        self.uri = uri
        self.username = username
        self.password = password
        self.database = database
        self.driver = driver


class NeptuneGraphSink(BaseCypherGraphSink):
    """Neptune graph sink backed by Graphiti's NeptuneDriver.

    Uses the Neptune openCypher HTTPS endpoint via langchain_aws / boto3,
    not the Neo4j Bolt protocol.
    """

    def __init__(
        self,
        *,
        host: Optional[str] = None,
        port: int = 8182,
        host_scheme: str = "neptune-db",
        aoss_host: Optional[str] = None,
        aoss_port: int = 443,
        driver: Any = None,
        executor: BaseCypherExecutor | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        batch_size: int = 250,
    ) -> None:
        super().__init__(
            batch_size=batch_size,
            embedding_provider=embedding_provider,
            executor=executor
            or GraphitiNeptuneExecutor(
                host=host,
                port=port,
                host_scheme=host_scheme,
                aoss_host=aoss_host,
                aoss_port=aoss_port,
                driver=driver,
            ),
        )
        self.host = host
        self.port = port
        self.host_scheme = host_scheme
        self.aoss_host = aoss_host
        self.aoss_port = aoss_port
        self.driver = driver



def _context_node_from_record(record: Any) -> EntityNode:
    metadata = _maybe_json_dict(record.get("metadata_json"))
    aliases_payload = _maybe_json_list(record.get("aliases_json"))
    return make_entity_node(
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
