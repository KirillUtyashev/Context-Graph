from __future__ import annotations

from abc import ABC, abstractmethod
import math
from dataclasses import dataclass
import hashlib
from typing import Any, Iterable, Mapping, Sequence

from .embeddings import EmbeddingProvider
from .metadata import MetadataStore, PostgresMetadataStore
from .profiles import ProfileDeactivation, ProfileRecord
from .query_parser import QueryParseResult, QueryParser


@dataclass(frozen=True, slots=True)
class ProfileKeyRecord:
    profile_key_id: str
    profile_id: str
    repo_id: str
    profile_kind: str
    relation_type: str | None
    source_node_id: str | None
    target_node_id: str | None
    key_text: str
    embedding: tuple[float, ...]
    is_active: bool
    valid_from_revision: str | None
    valid_to_revision: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class ProfileVectorHit:
    profile_id: str
    profile_key_id: str
    repo_id: str
    profile_kind: str
    key_text: str
    score: float


@dataclass(frozen=True, slots=True)
class ProfileSearchResult:
    parse_result: QueryParseResult
    hits: tuple[ProfileVectorHit, ...]
    profiles: tuple[ProfileRecord, ...]


class BaseProfileVectorStore(ABC):
    @abstractmethod
    def bootstrap(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def upsert_profile_keys(self, records: Sequence[ProfileKeyRecord]) -> None:
        raise NotImplementedError

    @abstractmethod
    def deactivate_profile_keys(self, deactivations: Sequence[ProfileDeactivation]) -> None:
        raise NotImplementedError

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
        raise NotImplementedError

    @abstractmethod
    def reset_repo_context(self, repo_id: str) -> int:
        raise NotImplementedError


class InMemoryProfileVectorStore(BaseProfileVectorStore):
    def __init__(self) -> None:
        self._records: dict[str, ProfileKeyRecord] = {}

    def bootstrap(self) -> None:
        return None

    def upsert_profile_keys(self, records: Sequence[ProfileKeyRecord]) -> None:
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


class AuroraProfileVectorStore(BaseProfileVectorStore):
    def __init__(self, connection: Any, *, embedding_dim: int = 1024) -> None:
        self.connection = connection
        self.embedding_dim = embedding_dim
        self.placeholder = "%s"

    @classmethod
    def from_metadata_store(
        cls,
        metadata_store: MetadataStore,
        *,
        embedding_dim: int = 1024,
    ) -> "AuroraProfileVectorStore":
        if not isinstance(metadata_store, PostgresMetadataStore):
            raise RuntimeError("AuroraProfileVectorStore requires a PostgreSQL-compatible metadata store")
        return cls(metadata_store.connection, embedding_dim=embedding_dim)

    def bootstrap(self) -> None:
        cursor = self.connection.cursor()
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

    def upsert_profile_keys(self, records: Sequence[ProfileKeyRecord]) -> None:
        if not records:
            return
        self.bootstrap()
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
                embedding,
                is_active,
                valid_from_revision,
                valid_to_revision,
                updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS vector), %s, %s, %s, %s)
            ON CONFLICT (profile_key_id) DO UPDATE SET
                profile_id = excluded.profile_id,
                repo_id = excluded.repo_id,
                profile_kind = excluded.profile_kind,
                relation_type = excluded.relation_type,
                source_node_id = excluded.source_node_id,
                target_node_id = excluded.target_node_id,
                key_text = excluded.key_text,
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
                _vector_literal(record.embedding),
                record.is_active,
                record.valid_from_revision,
                record.valid_to_revision,
                record.updated_at,
            )
            for record in records
        ]
        cursor = self.connection.cursor()
        cursor.executemany(sql, rows)
        self.connection.commit()

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
        cursor.executemany(sql, rows)
        self.connection.commit()

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
        return tuple(
            ProfileVectorHit(
                profile_id=str(row[0]),
                profile_key_id=str(row[1]),
                repo_id=str(row[2]),
                profile_kind=str(row[3]),
                key_text=str(row[4]),
                score=float(row[5]),
            )
            for row in rows
        )

    def reset_repo_context(self, repo_id: str) -> int:
        self.bootstrap()
        cursor = self.connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM cg_profile_keys WHERE repo_id = %s", (repo_id,))
        count = int(cursor.fetchone()[0])
        cursor.execute("DELETE FROM cg_profile_keys WHERE repo_id = %s", (repo_id,))
        self.connection.commit()
        return count


class ProfileVectorIndexer:
    def __init__(
        self,
        *,
        vector_store: BaseProfileVectorStore,
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider

    def apply_profile_updates(
        self,
        *,
        profiles: Sequence[ProfileRecord],
        deactivations: Sequence[ProfileDeactivation] = (),
    ) -> int:
        self.vector_store.bootstrap()
        if deactivations:
            self.vector_store.deactivate_profile_keys(deactivations)
        if not profiles:
            return 0
        records = build_profile_key_records(profiles, self.embedding_provider)
        self.vector_store.upsert_profile_keys(records)
        return len(records)

    def sync_repo(self, *, repo_id: str, metadata_store: MetadataStore) -> int:
        profiles = metadata_store.load_profiles(repo_id, active_only=True)
        return self.apply_profile_updates(profiles=profiles)


class ProfileSearchService:
    def __init__(
        self,
        *,
        metadata_store: MetadataStore,
        vector_store: BaseProfileVectorStore,
        embedding_provider: EmbeddingProvider,
        query_parser: QueryParser,
    ) -> None:
        self.metadata_store = metadata_store
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider
        self.query_parser = query_parser

    def search(
        self,
        *,
        repo_id: str,
        query: str,
        limit: int = 10,
        active_only: bool = True,
        llm_enabled: bool = True,
    ) -> ProfileSearchResult:
        parse_result = self.query_parser.parse(query, llm_enabled=llm_enabled)
        keywords = list(parse_result.high_level_keywords)
        if not keywords:
            keywords = [query]
        embeddings = self.embedding_provider.embed_texts(keywords)
        merged_hits: dict[str, ProfileVectorHit] = {}
        for embedding in embeddings:
            hits = self.vector_store.search_profile_keys(
                repo_id=repo_id,
                embedding=embedding,
                limit=limit,
                active_only=active_only,
            )
            for hit in hits:
                current = merged_hits.get(hit.profile_id)
                if current is None or hit.score > current.score:
                    merged_hits[hit.profile_id] = hit
        ordered_hits = tuple(sorted(merged_hits.values(), key=lambda hit: (-hit.score, hit.profile_id))[:limit])
        profile_map = {
            profile.profile_id: profile
            for profile in self.metadata_store.load_profiles(repo_id, active_only=active_only)
        }
        profiles = tuple(
            profile_map[hit.profile_id]
            for hit in ordered_hits
            if hit.profile_id in profile_map
        )
        return ProfileSearchResult(
            parse_result=parse_result,
            hits=ordered_hits,
            profiles=profiles,
        )


def build_profile_key_records(
    profiles: Sequence[ProfileRecord],
    embedding_provider: EmbeddingProvider,
) -> tuple[ProfileKeyRecord, ...]:
    profile_key_pairs: list[tuple[ProfileRecord, str]] = []
    for profile in profiles:
        for key in profile.keys:
            normalized = " ".join(key.strip().split())
            if not normalized:
                continue
            profile_key_pairs.append((profile, normalized))
    if not profile_key_pairs:
        return ()
    embeddings = embedding_provider.embed_texts([key for _, key in profile_key_pairs])
    records: list[ProfileKeyRecord] = []
    for (profile, key), embedding in zip(profile_key_pairs, embeddings):
        records.append(
            ProfileKeyRecord(
                profile_key_id=profile_key_id(profile.profile_id, key),
                profile_id=profile.profile_id,
                repo_id=profile.repo_id,
                profile_kind=profile.profile_kind,
                relation_type=profile.relation_type,
                source_node_id=profile.source_node_id,
                target_node_id=profile.target_node_id,
                key_text=key,
                embedding=tuple(float(value) for value in embedding),
                is_active=profile.is_active,
                valid_from_revision=profile.valid_from_revision,
                valid_to_revision=profile.valid_to_revision,
                updated_at=profile.updated_at,
            )
        )
    return tuple(records)


def profile_key_id(profile_id: str, key_text: str) -> str:
    payload = f"{profile_id}|{' '.join(key_text.strip().split()).casefold()}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"pkey:{digest}"


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(value):.12g}" for value in values) + "]"


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right:
        return 0.0
    numerator = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)
