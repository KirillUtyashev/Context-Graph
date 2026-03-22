from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Any, Iterable, Iterator, Optional, Sequence

from .episodes import ContextNodeRef
from .models import GraphSnapshot, canonical_json, utc_now_iso


@dataclass(frozen=True, slots=True)
class RepoIndexState:
    repo_id: str
    root_path: str
    latest_revision_id: str
    latest_indexed_at: str
    metadata: dict[str, Any]
    latest_commit_sha: str | None = None
    latest_branch: str | None = None
    last_commit_indexed_at: str | None = None


@dataclass(frozen=True, slots=True)
class RepoSourceState:
    repo_id: str
    provider: str
    canonical_repo: str
    clone_url: str
    default_branch: str | None
    clone_path: str
    last_fetch_at: str | None = None
    last_fetched_head_sha: str | None = None
    last_github_sync_at: str | None = None
    last_github_sync_status: str | None = None
    last_github_sync_error: str | None = None
    metadata: dict[str, Any] | None = None


class MetadataStore(ABC):
    @abstractmethod
    def bootstrap(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def load_latest_snapshot(self, repo_id: str) -> GraphSnapshot | None:
        raise NotImplementedError

    @abstractmethod
    def save_latest_snapshot(self, snapshot: GraphSnapshot) -> None:
        raise NotImplementedError

    @abstractmethod
    def load_repo_index_state(self, repo_id: str) -> RepoIndexState | None:
        raise NotImplementedError

    @abstractmethod
    def save_repo_index_state(self, state: RepoIndexState) -> None:
        raise NotImplementedError

    @abstractmethod
    def load_repo_source_state(self, repo_id: str) -> RepoSourceState | None:
        raise NotImplementedError

    @abstractmethod
    def save_repo_source_state(self, state: RepoSourceState) -> None:
        raise NotImplementedError

    @abstractmethod
    def load_context_nodes(self, repo_id: str) -> tuple[ContextNodeRef, ...]:
        raise NotImplementedError

    @abstractmethod
    def upsert_context_nodes(self, repo_id: str, nodes: Sequence[ContextNodeRef]) -> None:
        raise NotImplementedError

    @abstractmethod
    @contextmanager
    def repo_lock(self, repo_key: str) -> Iterator[None]:
        raise NotImplementedError

    @abstractmethod
    def record_index_run(
        self,
        *,
        repo_id: str,
        revision_id: str,
        status: str,
        stats: dict[str, Any],
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def reset_repo_context(self, repo_id: str) -> dict[str, int]:
        raise NotImplementedError


class InMemoryMetadataStore(MetadataStore):
    def __init__(self) -> None:
        self._snapshots: dict[str, GraphSnapshot] = {}
        self._repo_states: dict[str, RepoIndexState] = {}
        self._repo_sources: dict[str, RepoSourceState] = {}
        self._context_nodes: dict[str, dict[str, ContextNodeRef]] = {}
        self._runs: list[dict[str, Any]] = []

    def bootstrap(self) -> None:
        return None

    def load_latest_snapshot(self, repo_id: str) -> GraphSnapshot | None:
        return self._snapshots.get(repo_id)

    def save_latest_snapshot(self, snapshot: GraphSnapshot) -> None:
        self._snapshots[snapshot.repo_id] = snapshot
        existing = self._repo_states.get(snapshot.repo_id)
        self._repo_states[snapshot.repo_id] = RepoIndexState(
            repo_id=snapshot.repo_id,
            root_path=snapshot.root_path,
            latest_revision_id=snapshot.revision_id,
            latest_indexed_at=snapshot.generated_at,
            metadata=dict(snapshot.metadata),
            latest_commit_sha=existing.latest_commit_sha if existing else None,
            latest_branch=existing.latest_branch if existing else None,
            last_commit_indexed_at=existing.last_commit_indexed_at if existing else None,
        )

    def load_repo_index_state(self, repo_id: str) -> RepoIndexState | None:
        return self._repo_states.get(repo_id)

    def save_repo_index_state(self, state: RepoIndexState) -> None:
        self._repo_states[state.repo_id] = state

    def load_repo_source_state(self, repo_id: str) -> RepoSourceState | None:
        return self._repo_sources.get(repo_id)

    def save_repo_source_state(self, state: RepoSourceState) -> None:
        self._repo_sources[state.repo_id] = state

    def load_context_nodes(self, repo_id: str) -> tuple[ContextNodeRef, ...]:
        return tuple(self._context_nodes.get(repo_id, {}).values())

    def upsert_context_nodes(self, repo_id: str, nodes: Sequence[ContextNodeRef]) -> None:
        bucket = self._context_nodes.setdefault(repo_id, {})
        for node in nodes:
            bucket[node.node_id] = node

    @contextmanager
    def repo_lock(self, repo_key: str) -> Iterator[None]:
        _ = repo_key
        yield

    def record_index_run(
        self,
        *,
        repo_id: str,
        revision_id: str,
        status: str,
        stats: dict[str, Any],
    ) -> None:
        self._runs.append(
            {
                "repo_id": repo_id,
                "revision_id": revision_id,
                "status": status,
                "stats": dict(stats),
                "recorded_at": utc_now_iso(),
            }
        )

    def reset_repo_context(self, repo_id: str) -> dict[str, int]:
        snapshot_deleted = 1 if repo_id in self._repo_states or repo_id in self._snapshots else 0
        source_deleted = 1 if repo_id in self._repo_sources else 0
        context_deleted = len(self._context_nodes.get(repo_id, {}))
        self._snapshots.pop(repo_id, None)
        self._repo_states.pop(repo_id, None)
        self._repo_sources.pop(repo_id, None)
        self._context_nodes.pop(repo_id, None)
        before_runs = len(self._runs)
        self._runs = [run for run in self._runs if run["repo_id"] != repo_id]
        return {
            "repo_state": snapshot_deleted,
            "repo_source": source_deleted,
            "current_nodes": 0,
            "current_edges": 0,
            "context_nodes": context_deleted,
            "profiles": 0,
            "index_runs": before_runs - len(self._runs),
        }


class _DbApiMetadataStore(MetadataStore):
    def __init__(self, connection: Any, placeholder: str) -> None:
        self.connection = connection
        self.placeholder = placeholder

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        cursor = self.connection.cursor()
        cursor.execute(sql, params)
        return cursor

    def _executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        cursor = self.connection.cursor()
        cursor.executemany(sql, rows)

    def bootstrap(self) -> None:
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS cg_repo_state (
                repo_id TEXT PRIMARY KEY,
                root_path TEXT NOT NULL,
                latest_revision_id TEXT NOT NULL,
                latest_indexed_at TEXT NOT NULL,
                stats_json TEXT NOT NULL
            )
            """
        )
        self._ensure_repo_state_column("latest_commit_sha TEXT")
        self._ensure_repo_state_column("latest_branch TEXT")
        self._ensure_repo_state_column("last_commit_indexed_at TEXT")
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS cg_current_nodes (
                repo_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                node_json TEXT NOT NULL,
                PRIMARY KEY (repo_id, node_id)
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS cg_current_edges (
                repo_id TEXT NOT NULL,
                edge_id TEXT NOT NULL,
                edge_json TEXT NOT NULL,
                PRIMARY KEY (repo_id, edge_id)
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS cg_index_runs (
                run_id TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                revision_id TEXT NOT NULL,
                status TEXT NOT NULL,
                stats_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS cg_context_nodes (
                repo_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                node_json TEXT NOT NULL,
                PRIMARY KEY (repo_id, node_id)
            )
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS cg_profiles (
                profile_id TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                profile_kind TEXT NOT NULL,
                profile_target_kind TEXT NOT NULL,
                profile_target_id TEXT NOT NULL,
                relation_type TEXT,
                source_node_id TEXT,
                source_node_kind TEXT,
                target_node_id TEXT,
                target_node_kind TEXT,
                target_revision_id TEXT,
                value_text TEXT NOT NULL,
                keys_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                is_active INTEGER NOT NULL,
                valid_from_revision TEXT,
                valid_to_revision TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._execute(
            """
            CREATE INDEX IF NOT EXISTS cg_profiles_repo_active_idx
            ON cg_profiles (repo_id, is_active)
            """
        )
        self._execute(
            """
            CREATE INDEX IF NOT EXISTS cg_profiles_repo_target_idx
            ON cg_profiles (repo_id, profile_target_kind, profile_target_id)
            """
        )
        self._execute(
            """
            CREATE INDEX IF NOT EXISTS cg_profiles_repo_relation_idx
            ON cg_profiles (repo_id, relation_type, is_active)
            """
        )
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS cg_repo_source (
                repo_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                canonical_repo TEXT NOT NULL,
                clone_url TEXT NOT NULL,
                default_branch TEXT,
                clone_path TEXT NOT NULL,
                last_fetch_at TEXT,
                last_fetched_head_sha TEXT,
                last_github_sync_at TEXT,
                last_github_sync_status TEXT,
                last_github_sync_error TEXT,
                metadata_json TEXT NOT NULL
            )
            """
        )
        self.connection.commit()

    def _ensure_repo_state_column(self, definition: str) -> None:
        try:
            self._execute(f"ALTER TABLE cg_repo_state ADD COLUMN {definition}")
        except Exception as exc:  # pragma: no cover - exact DB exception types vary by driver
            message = str(exc).lower()
            if "duplicate column" not in message and "already exists" not in message:
                raise
            rollback = getattr(self.connection, "rollback", None)
            if callable(rollback):
                rollback()

    def load_latest_snapshot(self, repo_id: str) -> GraphSnapshot | None:
        state_sql = f"""
            SELECT root_path, latest_revision_id, latest_indexed_at, stats_json
            FROM cg_repo_state
            WHERE repo_id = {self.placeholder}
        """
        row = self._execute(state_sql, (repo_id,)).fetchone()
        if row is None:
            return None

        nodes_sql = f"""
            SELECT node_json
            FROM cg_current_nodes
            WHERE repo_id = {self.placeholder}
            ORDER BY node_id
        """
        edges_sql = f"""
            SELECT edge_json
            FROM cg_current_edges
            WHERE repo_id = {self.placeholder}
            ORDER BY edge_id
        """
        nodes = [json.loads(item[0]) for item in self._execute(nodes_sql, (repo_id,)).fetchall()]
        edges = [json.loads(item[0]) for item in self._execute(edges_sql, (repo_id,)).fetchall()]

        return GraphSnapshot.from_dict(
            {
                "repo_id": repo_id,
                "revision_id": row[1],
                "root_path": row[0],
                "generated_at": row[2],
                "nodes": nodes,
                "edges": edges,
                "metadata": json.loads(row[3]),
            }
        )

    def load_repo_index_state(self, repo_id: str) -> RepoIndexState | None:
        self.bootstrap()
        state_sql = f"""
            SELECT
                root_path,
                latest_revision_id,
                latest_indexed_at,
                stats_json,
                latest_commit_sha,
                latest_branch,
                last_commit_indexed_at
            FROM cg_repo_state
            WHERE repo_id = {self.placeholder}
        """
        row = self._execute(state_sql, (repo_id,)).fetchone()
        if row is None:
            return None

        return RepoIndexState(
            repo_id=repo_id,
            root_path=row[0],
            latest_revision_id=row[1],
            latest_indexed_at=row[2],
            metadata=json.loads(row[3]),
            latest_commit_sha=row[4],
            latest_branch=row[5],
            last_commit_indexed_at=row[6],
        )

    def save_latest_snapshot(self, snapshot: GraphSnapshot) -> None:
        self.bootstrap()
        delete_nodes_sql = f"DELETE FROM cg_current_nodes WHERE repo_id = {self.placeholder}"
        delete_edges_sql = f"DELETE FROM cg_current_edges WHERE repo_id = {self.placeholder}"
        self._execute(delete_nodes_sql, (snapshot.repo_id,))
        self._execute(delete_edges_sql, (snapshot.repo_id,))

        node_rows = [
            (snapshot.repo_id, node.node_id, canonical_json(node.to_dict()))
            for node in snapshot.nodes
        ]
        edge_rows = [
            (snapshot.repo_id, edge.edge_id, canonical_json(edge.to_dict()))
            for edge in snapshot.edges
        ]

        node_insert = f"""
            INSERT INTO cg_current_nodes (repo_id, node_id, node_json)
            VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder})
        """
        edge_insert = f"""
            INSERT INTO cg_current_edges (repo_id, edge_id, edge_json)
            VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder})
        """
        if node_rows:
            self._executemany(node_insert, node_rows)
        if edge_rows:
            self._executemany(edge_insert, edge_rows)

        state_insert = f"""
            INSERT INTO cg_repo_state (repo_id, root_path, latest_revision_id, latest_indexed_at, stats_json)
            VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder})
            ON CONFLICT (repo_id) DO UPDATE SET
                root_path = excluded.root_path,
                latest_revision_id = excluded.latest_revision_id,
                latest_indexed_at = excluded.latest_indexed_at,
                stats_json = excluded.stats_json
        """
        self._execute(
            state_insert,
            (
                snapshot.repo_id,
                snapshot.root_path,
                snapshot.revision_id,
                snapshot.generated_at,
                canonical_json(snapshot.metadata),
            ),
        )
        self.connection.commit()

    def save_repo_index_state(self, state: RepoIndexState) -> None:
        self.bootstrap()
        state_insert = f"""
            INSERT INTO cg_repo_state (
                repo_id,
                root_path,
                latest_revision_id,
                latest_indexed_at,
                stats_json,
                latest_commit_sha,
                latest_branch,
                last_commit_indexed_at
            )
            VALUES (
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder}
            )
            ON CONFLICT (repo_id) DO UPDATE SET
                root_path = excluded.root_path,
                latest_revision_id = excluded.latest_revision_id,
                latest_indexed_at = excluded.latest_indexed_at,
                stats_json = excluded.stats_json,
                latest_commit_sha = excluded.latest_commit_sha,
                latest_branch = excluded.latest_branch,
                last_commit_indexed_at = excluded.last_commit_indexed_at
        """
        self._execute(
            state_insert,
            (
                state.repo_id,
                state.root_path,
                state.latest_revision_id,
                state.latest_indexed_at,
                canonical_json(state.metadata),
                state.latest_commit_sha,
                state.latest_branch,
                state.last_commit_indexed_at,
            ),
        )
        self.connection.commit()

    def load_repo_source_state(self, repo_id: str) -> RepoSourceState | None:
        self.bootstrap()
        sql = f"""
            SELECT
                provider,
                canonical_repo,
                clone_url,
                default_branch,
                clone_path,
                last_fetch_at,
                last_fetched_head_sha,
                last_github_sync_at,
                last_github_sync_status,
                last_github_sync_error,
                metadata_json
            FROM cg_repo_source
            WHERE repo_id = {self.placeholder}
        """
        row = self._execute(sql, (repo_id,)).fetchone()
        if row is None:
            return None

        return RepoSourceState(
            repo_id=repo_id,
            provider=row[0],
            canonical_repo=row[1],
            clone_url=row[2],
            default_branch=row[3],
            clone_path=row[4],
            last_fetch_at=row[5],
            last_fetched_head_sha=row[6],
            last_github_sync_at=row[7],
            last_github_sync_status=row[8],
            last_github_sync_error=row[9],
            metadata=json.loads(row[10]),
        )

    def save_repo_source_state(self, state: RepoSourceState) -> None:
        self.bootstrap()
        sql = f"""
            INSERT INTO cg_repo_source (
                repo_id,
                provider,
                canonical_repo,
                clone_url,
                default_branch,
                clone_path,
                last_fetch_at,
                last_fetched_head_sha,
                last_github_sync_at,
                last_github_sync_status,
                last_github_sync_error,
                metadata_json
            )
            VALUES (
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder},
                {self.placeholder}
            )
            ON CONFLICT (repo_id) DO UPDATE SET
                provider = excluded.provider,
                canonical_repo = excluded.canonical_repo,
                clone_url = excluded.clone_url,
                default_branch = excluded.default_branch,
                clone_path = excluded.clone_path,
                last_fetch_at = excluded.last_fetch_at,
                last_fetched_head_sha = excluded.last_fetched_head_sha,
                last_github_sync_at = excluded.last_github_sync_at,
                last_github_sync_status = excluded.last_github_sync_status,
                last_github_sync_error = excluded.last_github_sync_error,
                metadata_json = excluded.metadata_json
        """
        self._execute(
            sql,
            (
                state.repo_id,
                state.provider,
                state.canonical_repo,
                state.clone_url,
                state.default_branch,
                state.clone_path,
                state.last_fetch_at,
                state.last_fetched_head_sha,
                state.last_github_sync_at,
                state.last_github_sync_status,
                state.last_github_sync_error,
                canonical_json(state.metadata or {}),
            ),
        )
        self.connection.commit()

    def load_context_nodes(self, repo_id: str) -> tuple[ContextNodeRef, ...]:
        self.bootstrap()
        sql = f"""
            SELECT node_json
            FROM cg_context_nodes
            WHERE repo_id = {self.placeholder}
            ORDER BY node_id
        """
        rows = self._execute(sql, (repo_id,)).fetchall()
        return tuple(_context_node_from_payload(json.loads(row[0])) for row in rows)

    def upsert_context_nodes(self, repo_id: str, nodes: Sequence[ContextNodeRef]) -> None:
        if not nodes:
            return
        self.bootstrap()
        sql = f"""
            INSERT INTO cg_context_nodes (repo_id, node_id, node_json)
            VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder})
            ON CONFLICT (repo_id, node_id) DO UPDATE SET
                node_json = excluded.node_json
        """
        rows = [
            (
                repo_id,
                node.node_id,
                canonical_json(_context_node_to_payload(node)),
            )
            for node in nodes
        ]
        self._executemany(sql, rows)
        self.connection.commit()

    @contextmanager
    def repo_lock(self, repo_key: str) -> Iterator[None]:
        _ = repo_key
        yield

    def record_index_run(
        self,
        *,
        repo_id: str,
        revision_id: str,
        status: str,
        stats: dict[str, Any],
    ) -> None:
        self.bootstrap()
        run_id = f"{repo_id}:{revision_id}:{status}:{utc_now_iso()}"
        run_insert = f"""
            INSERT INTO cg_index_runs (run_id, repo_id, revision_id, status, stats_json, recorded_at)
            VALUES ({self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder}, {self.placeholder})
        """
        self._execute(
            run_insert,
            (
                run_id,
                repo_id,
                revision_id,
                status,
                canonical_json(stats),
                utc_now_iso(),
            ),
        )
        self.connection.commit()

    def reset_repo_context(self, repo_id: str) -> dict[str, int]:
        self.bootstrap()
        table_specs = (
            ("cg_profiles", "profiles"),
            ("cg_context_nodes", "context_nodes"),
            ("cg_current_edges", "current_edges"),
            ("cg_current_nodes", "current_nodes"),
            ("cg_repo_source", "repo_source"),
            ("cg_repo_state", "repo_state"),
            ("cg_index_runs", "index_runs"),
        )
        deleted_counts: dict[str, int] = {}
        for table_name, result_key in table_specs:
            count_sql = (
                f"SELECT COUNT(*) FROM {table_name} WHERE repo_id = {self.placeholder}"
            )
            delete_sql = f"DELETE FROM {table_name} WHERE repo_id = {self.placeholder}"
            count = int(self._execute(count_sql, (repo_id,)).fetchone()[0])
            self._execute(delete_sql, (repo_id,))
            deleted_counts[result_key] = count
        self.connection.commit()
        return deleted_counts


class SQLiteMetadataStore(_DbApiMetadataStore):
    def __init__(self, path: str) -> None:
        connection = sqlite3.connect(path)
        super().__init__(connection=connection, placeholder="?")


class PostgresMetadataStore(_DbApiMetadataStore):
    """
    PostgreSQL-compatible metadata store.

    This expects a DB-API style connection object, for example from psycopg/psycopg2.
    We keep the schema intentionally simple so the same latest-snapshot metadata can
    also be stored locally in SQLite during development.
    """

    def __init__(self, connection: Any) -> None:
        super().__init__(connection=connection, placeholder="%s")

    @contextmanager
    def repo_lock(self, repo_key: str) -> Iterator[None]:
        self.bootstrap()
        lock_id = _stable_lock_id(repo_key)
        try:
            self._execute(
                f"SELECT pg_advisory_lock({self.placeholder})",
                (lock_id,),
            )
            yield
        finally:
            self._execute(
                f"SELECT pg_advisory_unlock({self.placeholder})",
                (lock_id,),
            )
            self.connection.commit()

    @classmethod
    def from_dsn(cls, dsn: str) -> "PostgresMetadataStore":
        try:
            import psycopg  # type: ignore

            connection = psycopg.connect(dsn)
            return cls(connection=connection)
        except ImportError:
            pass

        try:
            import psycopg2  # type: ignore

            connection = psycopg2.connect(dsn)
            return cls(connection=connection)
        except ImportError as exc:
            raise RuntimeError(
                "PostgreSQL metadata storage requires psycopg or psycopg2 to be installed"
            ) from exc


class AuroraMetadataStore(PostgresMetadataStore):
    """Backward-compatible alias for Aurora PostgreSQL deployments."""


def _stable_lock_id(value: str) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _context_node_to_payload(node: ContextNodeRef) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "repo_id": node.repo_id,
        "kind": node.kind,
        "name": node.name,
        "qualified_name": node.qualified_name,
        "file_path": node.file_path,
        "aliases": list(node.aliases),
        "metadata": dict(node.metadata),
    }


def _context_node_from_payload(payload: dict[str, Any]) -> ContextNodeRef:
    return ContextNodeRef(
        node_id=str(payload["node_id"]),
        repo_id=str(payload["repo_id"]),
        kind=str(payload["kind"]),
        name=str(payload["name"]),
        qualified_name=payload.get("qualified_name"),
        file_path=payload.get("file_path"),
        aliases=tuple(payload.get("aliases") or ()),
        metadata=dict(payload.get("metadata") or {}),
    )
