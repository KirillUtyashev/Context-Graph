"""Canonical profile-vector store implementations for the additive backend."""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
import math
from typing import Any, Sequence

from context_graph.profiles import ProfileDeactivation

from .types import ProfileKeyRecord, ProfileVectorHit


logger = logging.getLogger(__name__)


class ProfileVectorStore(ABC):
    """Synchronous storage boundary for searchable profile-key embeddings."""

    @abstractmethod
    def bootstrap(self) -> None:
        """Create any tables or indexes required by the vector store."""

    @abstractmethod
    def upsert_profile_keys(self, records: Sequence[ProfileKeyRecord]) -> None:
        """Persist or update profile-key embeddings."""

    @abstractmethod
    def deactivate_profile_keys(self, deactivations: Sequence[ProfileDeactivation]) -> None:
        """Mark matching profile-key rows inactive."""

    @abstractmethod
    def search_profile_keys(
        self,
        *,
        repo_id: str,
        embedding: Sequence[float],
        limit: int,
        active_only: bool = True,
        profile_kinds: Sequence[str] | None = None,
    ) -> tuple[ProfileVectorHit, ...]:
        """Search profile-key vectors for one repository."""

    @abstractmethod
    def reset_repo_context(self, repo_id: str) -> int:
        """Delete all profile-key rows associated with one repository."""


class InMemoryProfileVectorStore(ProfileVectorStore):
    """In-memory cosine-similarity implementation used in tests and local flows."""

    def __init__(self) -> None:
        self._records: dict[str, ProfileKeyRecord] = {}

    def bootstrap(self) -> None:
        return None

    def upsert_profile_keys(self, records: Sequence[ProfileKeyRecord]) -> None:
        profile_ids = {record.profile_id for record in records}
        if profile_ids:
            self._records = {
                key: record
                for key, record in self._records.items()
                if record.profile_id not in profile_ids
            }
        for record in records:
            self._records[record.profile_key_id] = record

    def deactivate_profile_keys(self, deactivations: Sequence[ProfileDeactivation]) -> None:
        for deactivation in deactivations:
            for key, record in list(self._records.items()):
                if record.repo_id != deactivation.repo_id:
                    continue
                if record.profile_kind != deactivation.profile_kind:
                    continue
                if record.relation_type != deactivation.relation_type:
                    continue
                if record.source_node_id != deactivation.source_node_id:
                    continue
                if record.target_node_id != deactivation.target_node_id:
                    continue
                if (
                    deactivation.replacement_profile_id
                    and record.profile_id == deactivation.replacement_profile_id
                ):
                    continue
                if record.is_active:
                    self._records[key] = ProfileKeyRecord(
                        profile_key_id=record.profile_key_id,
                        profile_id=record.profile_id,
                        repo_id=record.repo_id,
                        profile_kind=record.profile_kind,
                        relation_type=record.relation_type,
                        source_node_id=record.source_node_id,
                        target_node_id=record.target_node_id,
                        key_text=record.key_text,
                        key_origin=record.key_origin,
                        embedding=record.embedding,
                        is_active=False,
                        valid_from_revision=record.valid_from_revision,
                        valid_to_revision=record.valid_to_revision or deactivation.valid_to_revision,
                        updated_at=deactivation.observed_at,
                    )

    def search_profile_keys(
        self,
        *,
        repo_id: str,
        embedding: Sequence[float],
        limit: int,
        active_only: bool = True,
        profile_kinds: Sequence[str] | None = None,
    ) -> tuple[ProfileVectorHit, ...]:
        allowed_kinds = set(profile_kinds or ())
        hits: list[ProfileVectorHit] = []
        for record in self._records.values():
            if record.repo_id != repo_id:
                continue
            if active_only and not record.is_active:
                continue
            if allowed_kinds and record.profile_kind not in allowed_kinds:
                continue
            hits.append(
                ProfileVectorHit(
                    profile_id=record.profile_id,
                    profile_key_id=record.profile_key_id,
                    repo_id=record.repo_id,
                    profile_kind=record.profile_kind,
                    key_text=record.key_text,
                    key_origin=record.key_origin,
                    score=_cosine_similarity(embedding, record.embedding),
                )
            )
        ordered = sorted(hits, key=lambda hit: (-hit.score, hit.profile_id, hit.profile_key_id))
        return tuple(ordered[:limit])

    def reset_repo_context(self, repo_id: str) -> int:
        deleted = len([record for record in self._records.values() if record.repo_id == repo_id])
        self._records = {
            key: record for key, record in self._records.items() if record.repo_id != repo_id
        }
        return deleted


class PgVectorProfileVectorStore(ProfileVectorStore):
    """PostgreSQL/pgvector-backed store for profile-key embeddings."""

    def __init__(self, connection: Any, *, embedding_dim: int = 1024) -> None:
        self.connection = connection
        self.embedding_dim = embedding_dim
        self._needs_resync = False

    @property
    def needs_resync(self) -> bool:
        return self._needs_resync

    def mark_resynced(self) -> None:
        self._needs_resync = False

    @classmethod
    def from_metadata_store(
        cls,
        metadata_store,
        *,
        embedding_dim: int = 1024,
    ) -> "PgVectorProfileVectorStore":
        from context_graph.storage.backend.metadata import PostgresMetadataStore

        if not isinstance(metadata_store, PostgresMetadataStore):
            raise RuntimeError("PgVectorProfileVectorStore requires a PostgreSQL-compatible metadata store")
        return cls(metadata_store.connection, embedding_dim=embedding_dim)

    def bootstrap(self) -> None:
        cursor = self.connection.cursor()
        try:
            current_dim = _current_vector_dimension(cursor, "cg_profile_keys", "embedding")
            if current_dim is not None and current_dim != self.embedding_dim:
                logger.warning(
                    "Rebuilding cg_profile_keys because embedding dimension changed from %s to %s",
                    current_dim,
                    self.embedding_dim,
                )
                cursor.execute("DROP TABLE IF EXISTS cg_profile_keys")
                self.connection.commit()
                self._needs_resync = True
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS cg_profile_keys (
                    profile_key_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    repo_id TEXT NOT NULL,
                    profile_kind TEXT NOT NULL,
                    relation_type TEXT,
                    source_node_id TEXT,
                    target_node_id TEXT,
                    key_text TEXT NOT NULL,
                    key_origin TEXT NOT NULL DEFAULT 'legacy_keyword',
                    embedding vector({self.embedding_dim}) NOT NULL,
                    is_active BOOLEAN NOT NULL,
                    valid_from_revision TEXT,
                    valid_to_revision TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                ALTER TABLE cg_profile_keys
                ADD COLUMN IF NOT EXISTS key_origin TEXT NOT NULL DEFAULT 'legacy_keyword'
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS cg_profile_keys_repo_active_idx
                ON cg_profile_keys (repo_id, is_active)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS cg_profile_keys_profile_idx
                ON cg_profile_keys (profile_id)
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS cg_profile_keys_embedding_hnsw_idx
                ON cg_profile_keys USING hnsw (embedding vector_cosine_ops)
                """
            )
            self.connection.commit()
        except Exception:
            _rollback_connection(self.connection)
            raise

    def upsert_profile_keys(self, records: Sequence[ProfileKeyRecord]) -> None:
        if not records:
            return
        self.bootstrap()
        profile_ids = tuple(sorted({record.profile_id for record in records}))
        cursor = self.connection.cursor()
        try:
            if profile_ids:
                cursor.execute(
                    "DELETE FROM cg_profile_keys WHERE profile_id = ANY(%s)",
                    (list(profile_ids),),
                )
            sql = """
                INSERT INTO cg_profile_keys (
                    profile_key_id,
                    profile_id,
                    repo_id,
                    profile_kind,
                    relation_type,
                    source_node_id,
                    target_node_id,
                    key_text,
                    key_origin,
                    embedding,
                    is_active,
                    valid_from_revision,
                    valid_to_revision,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS vector), %s, %s, %s, %s)
                ON CONFLICT (profile_key_id) DO UPDATE SET
                    profile_id = excluded.profile_id,
                    repo_id = excluded.repo_id,
                    profile_kind = excluded.profile_kind,
                    relation_type = excluded.relation_type,
                    source_node_id = excluded.source_node_id,
                    target_node_id = excluded.target_node_id,
                    key_text = excluded.key_text,
                    key_origin = excluded.key_origin,
                    embedding = excluded.embedding,
                    is_active = excluded.is_active,
                    valid_from_revision = excluded.valid_from_revision,
                    valid_to_revision = excluded.valid_to_revision,
                    updated_at = excluded.updated_at
            """
            rows = [
                (
                    record.profile_key_id,
                    record.profile_id,
                    record.repo_id,
                    record.profile_kind,
                    record.relation_type,
                    record.source_node_id,
                    record.target_node_id,
                    record.key_text,
                    record.key_origin,
                    _vector_literal(record.embedding),
                    record.is_active,
                    record.valid_from_revision,
                    record.valid_to_revision,
                    record.updated_at,
                )
                for record in records
            ]
            cursor.executemany(sql, rows)
            self.connection.commit()
        except Exception:
            _rollback_connection(self.connection)
            raise

    def deactivate_profile_keys(self, deactivations: Sequence[ProfileDeactivation]) -> None:
        if not deactivations:
            return
        self.bootstrap()
        sql = """
            UPDATE cg_profile_keys
            SET
                is_active = FALSE,
                valid_to_revision = COALESCE(valid_to_revision, %s),
                updated_at = %s
            WHERE repo_id = %s
              AND profile_kind = %s
              AND relation_type = %s
              AND source_node_id = %s
              AND target_node_id = %s
              AND is_active = TRUE
              AND (%s IS NULL OR profile_id <> %s)
        """
        rows = [
            (
                deactivation.valid_to_revision,
                deactivation.observed_at,
                deactivation.repo_id,
                deactivation.profile_kind,
                deactivation.relation_type,
                deactivation.source_node_id,
                deactivation.target_node_id,
                deactivation.replacement_profile_id,
                deactivation.replacement_profile_id,
            )
            for deactivation in deactivations
        ]
        cursor = self.connection.cursor()
        try:
            cursor.executemany(sql, rows)
            self.connection.commit()
        except Exception:
            _rollback_connection(self.connection)
            raise

    def search_profile_keys(
        self,
        *,
        repo_id: str,
        embedding: Sequence[float],
        limit: int,
        active_only: bool = True,
        profile_kinds: Sequence[str] | None = None,
    ) -> tuple[ProfileVectorHit, ...]:
        self.bootstrap()
        sql = """
            SELECT
                profile_id,
                profile_key_id,
                repo_id,
                profile_kind,
                key_text,
                key_origin,
                1 - (embedding <=> CAST(%s AS vector)) AS score
            FROM cg_profile_keys
            WHERE repo_id = %s
              AND (%s = FALSE OR is_active = TRUE)
              AND (%s::text[] IS NULL OR profile_kind = ANY(%s::text[]))
            ORDER BY embedding <=> CAST(%s AS vector)
            LIMIT %s
        """
        kinds = list(profile_kinds) if profile_kinds else None
        cursor = self.connection.cursor()
        try:
            cursor.execute(
                sql,
                (
                    _vector_literal(embedding),
                    repo_id,
                    not active_only,
                    kinds,
                    kinds,
                    _vector_literal(embedding),
                    limit,
                ),
            )
            rows = cursor.fetchall()
        except Exception:
            _rollback_connection(self.connection)
            raise
        return tuple(
            ProfileVectorHit(
                profile_id=str(row[0]),
                profile_key_id=str(row[1]),
                repo_id=str(row[2]),
                profile_kind=str(row[3]),
                key_text=str(row[4]),
                key_origin=str(row[5]),
                score=float(row[6]),
            )
            for row in rows
        )

    def reset_repo_context(self, repo_id: str) -> int:
        self.bootstrap()
        cursor = self.connection.cursor()
        try:
            cursor.execute("SELECT COUNT(*) FROM cg_profile_keys WHERE repo_id = %s", (repo_id,))
            count = int(cursor.fetchone()[0])
            cursor.execute("DELETE FROM cg_profile_keys WHERE repo_id = %s", (repo_id,))
            self.connection.commit()
        except Exception:
            _rollback_connection(self.connection)
            raise
        return count


class AuroraProfileVectorStore(PgVectorProfileVectorStore):
    """Backward-compatible alias for Aurora PostgreSQL deployments."""


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(value):.12g}" for value in values) + "]"


def _rollback_connection(connection: Any) -> None:
    rollback = getattr(connection, "rollback", None)
    if callable(rollback):
        rollback()


def _current_vector_dimension(cursor: Any, table_name: str, column_name: str) -> int | None:
    cursor.execute("SELECT to_regclass(%s)", (table_name,))
    table_ref = cursor.fetchone()
    if not table_ref or not isinstance(table_ref[0], str) or not table_ref[0]:
        return None
    cursor.execute(
        """
        SELECT atttypmod
        FROM pg_attribute
        WHERE attrelid = to_regclass(%s)
          AND attname = %s
        """,
        (table_name, column_name),
    )
    row = cursor.fetchone()
    if row is None or row[0] is None:
        return None
    value = int(row[0])
    return value if value > 0 else None


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right:
        return 0.0
    numerator = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)
