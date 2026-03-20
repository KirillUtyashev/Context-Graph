from __future__ import annotations

from abc import ABC, abstractmethod
import json
import sqlite3
from typing import Any, Iterable, Optional, Sequence

from .models import GraphSnapshot, canonical_json, utc_now_iso


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
    def record_index_run(
        self,
        *,
        repo_id: str,
        revision_id: str,
        status: str,
        stats: dict[str, Any],
    ) -> None:
        raise NotImplementedError


class InMemoryMetadataStore(MetadataStore):
    def __init__(self) -> None:
        self._snapshots: dict[str, GraphSnapshot] = {}
        self._runs: list[dict[str, Any]] = []

    def bootstrap(self) -> None:
        return None

    def load_latest_snapshot(self, repo_id: str) -> GraphSnapshot | None:
        return self._snapshots.get(repo_id)

    def save_latest_snapshot(self, snapshot: GraphSnapshot) -> None:
        self._snapshots[snapshot.repo_id] = snapshot

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
        self.connection.commit()

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
