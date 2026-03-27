"""Compatibility layer for profile-vector storage and search services.

The storage implementations and record types now live under
`context_graph.storage.backend.profile_vectors`. This module retains the current
service layer and historical import path while delegating storage to the new
backend package.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sys
from typing import Sequence

from .embeddings import EmbeddingProvider
from .metadata import MetadataStore
from .profiles import ProfileDeactivation, ProfileRecord
from .query_parser import QueryParseResult, QueryParser
from .storage.backend.profile_vectors import (
    AuroraProfileVectorStore,
    InMemoryProfileVectorConfig,
    InMemoryProfileVectorStore,
    PgVectorProfileVectorConfig,
    PgVectorProfileVectorStore,
    ProfileKeyRecord,
    ProfileVectorHit,
    ProfileVectorStore,
    create_profile_vector_store,
    create_profile_vector_store_from_metadata,
)

BaseProfileVectorStore = ProfileVectorStore


@dataclass(frozen=True, slots=True)
class ProfileSearchResult:
    """Combined result of query parsing, vector hits, and profile hydration."""

    parse_result: QueryParseResult
    hits: tuple[ProfileVectorHit, ...]
    profiles: tuple[ProfileRecord, ...]


class ProfileVectorIndexer:
    """Apply profile upserts and deactivations to a vector store."""

    def __init__(
        self,
        *,
        vector_store: ProfileVectorStore,
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
    """Vector-backed profile retrieval service used by the MCP server and CLI."""

    def __init__(
        self,
        *,
        metadata_store: MetadataStore,
        vector_store: ProfileVectorStore,
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
        print(
            "[context_graph.debug] profile_search.search "
            f"repo_id={repo_id!r} "
            f"query={query!r} "
            f"keywords={keywords!r} "
            f"limit={limit} "
            f"active_only={active_only} "
            f"llm_enabled={llm_enabled}",
            file=sys.stderr,
            flush=True,
        )
        embeddings = self.embedding_provider.embed_texts(keywords)
        merged_hits: dict[str, ProfileVectorHit] = {}
        for keyword, embedding in zip(keywords, embeddings):
            hits = self.vector_store.search_profile_keys(
                repo_id=repo_id,
                embedding=embedding,
                limit=limit,
                active_only=active_only,
            )
            raw_hits = [
                {
                    "profile_id": hit.profile_id,
                    "profile_key_id": hit.profile_key_id,
                    "repo_id": hit.repo_id,
                    "profile_kind": hit.profile_kind,
                    "key_text": hit.key_text,
                    "score": hit.score,
                }
                for hit in hits
            ]
            print(
                "[context_graph.debug] profile_search.raw_vector_hits "
                f"repo_id={repo_id!r} "
                f"keyword={keyword!r} "
                f"embedding_dim={len(embedding)} "
                f"hits={raw_hits!r}",
                file=sys.stderr,
                flush=True,
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
    """Expand profile keys into concrete vector-store records."""

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
    """Build a stable identifier for one profile-key pair."""

    payload = f"{profile_id}|{' '.join(key_text.strip().split()).casefold()}"
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"pkey:{digest}"


__all__ = [
    "AuroraProfileVectorStore",
    "BaseProfileVectorStore",
    "InMemoryProfileVectorConfig",
    "InMemoryProfileVectorStore",
    "PgVectorProfileVectorConfig",
    "PgVectorProfileVectorStore",
    "ProfileKeyRecord",
    "ProfileSearchResult",
    "ProfileSearchService",
    "ProfileVectorHit",
    "ProfileVectorIndexer",
    "ProfileVectorStore",
    "build_profile_key_records",
    "create_profile_vector_store",
    "create_profile_vector_store_from_metadata",
    "profile_key_id",
]
