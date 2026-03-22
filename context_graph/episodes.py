from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any, Mapping, Sequence

from .models import canonical_json


def episode_node_id(repo_id: str, episode_id: str) -> str:
    return f"episode:{repo_id}:{episode_id}"


def generic_entity_node_id(repo_id: str, name: str) -> str:
    normalized = " ".join(name.strip().split()).casefold()
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()
    return f"entity:{repo_id}:{digest}"


def fact_edge_id(
    *,
    repo_id: str,
    source_node_id: str,
    relation_type: str,
    target_node_id: str,
    fact: str,
) -> str:
    payload = "|".join(
        [
            repo_id,
            source_node_id,
            relation_type,
            target_node_id,
            fact.strip(),
        ]
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"fact:{repo_id}:{digest}"


@dataclass(frozen=True, slots=True)
class ContextNodeRef:
    node_id: str
    repo_id: str
    kind: str
    name: str
    qualified_name: str | None = None
    file_path: str | None = None
    aliases: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def searchable_terms(self) -> tuple[str, ...]:
        values = {
            self.name,
            self.qualified_name or "",
            self.file_path or "",
            *(alias for alias in self.aliases if alias),
        }
        if self.file_path:
            values.add(self.file_path.rsplit("/", 1)[-1])
        return tuple(sorted(value for value in values if value))


@dataclass(frozen=True, slots=True)
class EpisodeRecord:
    episode_id: str
    repo_id: str
    source_type: str
    source_id: str
    author: str | None
    created_at: str
    updated_at: str | None
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    parent_node_id: str | None = None

    @property
    def node_id(self) -> str:
        return episode_node_id(self.repo_id, self.episode_id)

    def metadata_json(self) -> str:
        return canonical_json(self.metadata)


@dataclass(frozen=True, slots=True)
class MentionRecord:
    episode_node_id: str
    target_node_id: str
    repo_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def metadata_json(self) -> str:
        return canonical_json(self.metadata)


@dataclass(frozen=True, slots=True)
class FactRecord:
    fact_id: str
    repo_id: str
    source_node_id: str
    target_node_id: str
    relation_type: str
    fact: str
    episode_ids: tuple[str, ...]
    created_at: str
    valid_at: str | None = None
    invalid_at: str | None = None
    expired_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def metadata_json(self) -> str:
        return canonical_json(self.metadata)


@dataclass(frozen=True, slots=True)
class FactInvalidation:
    repo_id: str
    source_node_id: str
    target_node_id: str
    relation_type: str
    observed_at: str
    replacement_fact_id: str | None = None
    replacement_valid_at: str | None = None


@dataclass(frozen=True, slots=True)
class EpisodeExtractionResult:
    mentions: tuple[ContextNodeRef, ...]
    facts: tuple[FactRecord, ...]
    invalidations: tuple[FactInvalidation, ...]
    mention_metadata: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EpisodeProcessingResult:
    episode: EpisodeRecord
    mentions: tuple[MentionRecord, ...]
    facts: tuple[FactRecord, ...]
    invalidations: tuple[FactInvalidation, ...]
    statements: tuple[Any, ...]
    created_context_nodes: tuple[ContextNodeRef, ...] = ()


def combine_episode_ids(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if value}))
