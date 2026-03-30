"""Data models and constants for provenance-oriented MCP queries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

IMPLEMENTED_STATUS = "implemented"

SEARCH_CONTEXT_SOURCES = ("claims", "observations", "relations", "code")
SOURCE_RECORD_TYPES: dict[str, tuple[str, ...]] = {
    "claims": ("claim",),
    "observations": ("observation", "evidence_record"),
    "relations": ("claim_relation",),
    "code": ("code_node", "project", "commit"),
}
PROVENANCE_TRAVERSAL_LIMIT = 200

LEXICAL_CHANNEL = "lexical"
SEMANTIC_CLAIM_CHANNEL = "semantic_claim"
SEMANTIC_OBSERVATION_CHANNEL = "semantic_observation"
SEMANTIC_RELATION_CHANNEL = "semantic_relation"
HYBRID_RRF_K = 60
HYBRID_CHANNEL_WEIGHTS = {
    LEXICAL_CHANNEL: 1.0,
    SEMANTIC_CLAIM_CHANNEL: 0.9,
    SEMANTIC_OBSERVATION_CHANNEL: 0.75,
    SEMANTIC_RELATION_CHANNEL: 0.75,
}

SemanticSearchFn = Callable[..., list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class HybridContextEntry:
    """One fused provenance retrieval result."""

    record: dict[str, Any]
    score: float
    lexical_score: float | None
    semantic_score: float | None
    retrieval_channels: tuple[str, ...]
    matched_profile_key_text: str | None = None
    matched_profile_key_origin: str | None = None
    semantic_profile_kind: str | None = None
    matched_lexical_summary: str | None = None


@dataclass(frozen=True, slots=True)
class RelationContextEntry:
    """One relation-profile retrieval result."""

    record_id: str
    relation_type: str | None
    score: float
    is_active: bool
    lexical_score: float | None = None
    semantic_score: float | None = None
    retrieval_channels: tuple[str, ...] = ()
    value_text: str = ""
    matched_profile_key_text: str | None = None
    matched_profile_key_origin: str | None = None
    semantic_profile_kind: str | None = None
    source_record_id: str | None = None
    source_record_type: str | None = None
    target_record_id: str | None = None
    target_record_type: str | None = None
    observed_at: str | None = None
    inactive_at: str | None = None


@dataclass(slots=True)
class _HybridContextAggregate:
    record: dict[str, Any]
    score: float = 0.0
    lexical_score: float | None = None
    semantic_score: float | None = None
    retrieval_channels: list[str] = field(default_factory=list)
    matched_profile_key_text: str | None = None
    matched_profile_key_origin: str | None = None
    semantic_profile_kind: str | None = None
    matched_lexical_summary: str | None = None


@dataclass(slots=True)
class _LexicalNodeCandidate:
    record: dict[str, Any]
    raw_score: float
    matched_lexical_summary: str | None = None
