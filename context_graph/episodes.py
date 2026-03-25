"""Episode data types – unified with Graphiti's type system.

The canonical types are Graphiti's ``EpisodicNode``, ``EntityNode``, and
``EntityEdge``.  This module re-exports them and provides thin factory
helpers (``make_episode``, ``make_entity_node``, ``make_entity_edge``)
that accept the field names historically used by Context Graph for
backward compatibility during the migration.

Deprecated aliases retained for downstream imports:
- ``EpisodicNode``  →  ``EpisodicNode``
- ``EntityNode`` →  ``EntityNode``
- ``EntityEdge``     →  ``EntityEdge``
"""
from __future__ import annotations

import hashlib
import json as _json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Sequence

# ── re-export Graphiti canonical types ────────────────────────────
from graphiti_core.edges import EntityEdge          # noqa: F401  (re-export)
from graphiti_core.nodes import (                   # noqa: F401  (re-export)
    EntityNode,
    EpisodeType,
    EpisodicNode,
)

from .models import canonical_json

if TYPE_CHECKING:
    from .profiles import ProfileDeactivation, ProfileRecord


# ──────────────────────────────────────────────────────────────────
# ID helpers (unchanged)
# ──────────────────────────────────────────────────────────────────

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
        [repo_id, source_node_id, relation_type, target_node_id, fact.strip()]
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return f"fact:{repo_id}:{digest}"


# ──────────────────────────────────────────────────────────────────
# Factory helpers – map old Context Graph field names to Graphiti types
# ──────────────────────────────────────────────────────────────────

def _parse_dt(value: str | datetime | None) -> datetime:
    """Accept ISO-8601 string or datetime; return UTC datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        # Handle common ISO formats including trailing Z
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    return datetime.now(timezone.utc)


def make_episode(
    *,
    episode_id: str,
    repo_id: str,
    source_type: str,
    source_id: str,
    author: str | None = None,
    created_at: str | datetime | None = None,
    updated_at: str | datetime | None = None,
    text: str,
    metadata: Mapping[str, Any] | None = None,
    parent_node_id: str | None = None,
) -> EpisodicNode:
    """Build an ``EpisodicNode`` using the old Context Graph field names."""
    return EpisodicNode(
        uuid=episode_node_id(repo_id, episode_id),
        name=episode_id,
        group_id=repo_id,
        source=EpisodeType(source_type),
        source_description=source_type,
        content=text,
        created_at=_parse_dt(created_at),
        valid_at=_parse_dt(updated_at or created_at),
        author=author,
        episode_metadata=dict(metadata) if metadata else {},
        parent_node_id=parent_node_id,
    )


def make_entity_node(
    *,
    node_id: str,
    repo_id: str,
    kind: str,
    name: str,
    qualified_name: str | None = None,
    file_path: str | None = None,
    aliases: Sequence[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> EntityNode:
    """Build an ``EntityNode`` using the old Context Graph ``EntityNode`` field names."""
    attrs: dict[str, Any] = {}
    if qualified_name is not None:
        attrs["qualified_name"] = qualified_name
    if file_path is not None:
        attrs["file_path"] = file_path
    if aliases:
        attrs["aliases"] = list(aliases)
    if metadata:
        attrs.update(metadata)
    labels = [kind] if kind and kind != "Entity" else []
    return EntityNode(
        uuid=node_id,
        name=name,
        group_id=repo_id,
        labels=labels,
        attributes=attrs,
    )


def make_entity_edge(
    *,
    fact_id: str,
    repo_id: str,
    source_node_id: str,
    target_node_id: str,
    relation_type: str,
    fact: str,
    episode_ids: Sequence[str] = (),
    created_at: str | datetime | None = None,
    valid_at: str | datetime | None = None,
    invalid_at: str | datetime | None = None,
    expired_at: str | datetime | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> EntityEdge:
    """Build an ``EntityEdge`` using the old Context Graph ``EntityEdge`` field names."""
    return EntityEdge(
        uuid=fact_id,
        group_id=repo_id,
        source_node_uuid=source_node_id,
        target_node_uuid=target_node_id,
        name=relation_type,
        fact=fact,
        episodes=list(episode_ids),
        created_at=_parse_dt(created_at),
        valid_at=_parse_dt(valid_at) if valid_at else None,
        invalid_at=_parse_dt(invalid_at) if invalid_at else None,
        expired_at=_parse_dt(expired_at) if expired_at else None,
        attributes=dict(metadata) if metadata else {},
    )


# ──────────────────────────────────────────────────────────────────
# Convenience accessors for EntityNode (code-graph fields)
# ──────────────────────────────────────────────────────────────────

def entity_node_kind(node: EntityNode) -> str:
    """Return the primary 'kind' label (e.g. 'Function', 'File')."""
    for label in node.labels:
        if label != "Entity":
            return label
    return "Entity"


def entity_node_qualified_name(node: EntityNode) -> str | None:
    return node.attributes.get("qualified_name")


def entity_node_file_path(node: EntityNode) -> str | None:
    return node.attributes.get("file_path")


def entity_node_aliases(node: EntityNode) -> tuple[str, ...]:
    aliases = node.attributes.get("aliases", [])
    return tuple(aliases) if isinstance(aliases, list) else ()


def entity_node_searchable_terms(node: EntityNode) -> tuple[str, ...]:
    """Equivalent to old ``EntityNode.searchable_terms()``."""
    values = {
        node.name,
        entity_node_qualified_name(node) or "",
        entity_node_file_path(node) or "",
        *entity_node_aliases(node),
    }
    fp = entity_node_file_path(node)
    if fp:
        values.add(fp.rsplit("/", 1)[-1])
    return tuple(sorted(v for v in values if v))




# ──────────────────────────────────────────────────────────────────
# Types that have no Graphiti equivalent (kept as-is)
# ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class MentionRecord:
    episode_node_id: str
    target_node_id: str
    repo_id: str
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
class EpisodeProcessingResult:
    episode: EpisodicNode
    mentions: tuple[MentionRecord, ...]
    facts: tuple[EntityEdge, ...]
    invalidations: tuple[FactInvalidation, ...]
    statements: tuple[Any, ...]
    profiles: tuple[ProfileRecord, ...] = ()
    profile_deactivations: tuple[ProfileDeactivation, ...] = ()
    created_context_nodes: tuple[EntityNode, ...] = ()


def combine_episode_ids(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted({value for value in values if value}))
