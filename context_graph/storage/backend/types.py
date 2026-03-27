"""Shared type definitions for the provider-agnostic backend interface.

The backend contract is intentionally small and schema-flexible. The storage
layer understands only physical record shapes (`node` vs `edge`) plus free-form
`record_type` values supplied by higher layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BackendProvider(Enum):
    """Supported graph storage providers."""

    NEO4J = "neo4j"
    NEPTUNE = "neptune"


class RecordKind(Enum):
    """Physical graph record kind used by the backend.

    `RecordKind` deliberately models storage shape, not ontology meaning.
    Concepts such as `claim`, `theory`, and `research_artifact` belong in
    `record_type`, while the backend only distinguishes nodes from edges.
    """

    NODE = "node"
    EDGE = "edge"


class SearchMode(Enum):
    """Search mode requested by higher layers."""

    LEXICAL = "lexical"
    VECTOR = "vector"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Capabilities advertised by a concrete backend driver."""

    supports_transactions: bool
    supports_lexical_search: bool
    supports_vector_search: bool
    supports_hybrid_search: bool
    supports_graph_traversal: bool
    uses_external_text_index: bool


@dataclass(frozen=True, slots=True)
class GraphRecordRef:
    """Stable reference to a graph record.

    The backend returns references from search and accepts them back during
    hydration. `record_type` remains open-ended so higher-level ontology changes
    do not require backend API changes.
    """

    record_kind: RecordKind
    record_id: str
    record_type: str
    repo_id: str | None = None
    group_id: str | None = None


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """Provider-neutral search request.

    V1 fully implements lexical search only. Vector and hybrid modes are kept in
    the contract now so higher layers can build against a stable interface.
    """

    mode: SearchMode
    text: str | None = None
    embedding: tuple[float, ...] | None = None
    record_kinds: tuple[RecordKind, ...] | None = None
    record_types: tuple[str, ...] | None = None
    repo_ids: tuple[str, ...] | None = None
    group_ids: tuple[str, ...] | None = None
    limit: int = 10
    min_score: float | None = None
    filters: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be >= 1")

        if self.mode is SearchMode.LEXICAL and not self.text:
            raise ValueError("lexical search requires text")

        if self.mode is SearchMode.VECTOR and not self.embedding:
            raise ValueError("vector search requires an embedding")

        if self.mode is SearchMode.HYBRID and not (self.text or self.embedding):
            raise ValueError("hybrid search requires text, an embedding, or both")


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One scored hit returned by backend search."""

    ref: GraphRecordRef
    score: float
    lexical_score: float | None = None
    vector_score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TraversalQuery:
    """Provider-neutral graph expansion request."""

    origin_refs: tuple[GraphRecordRef, ...]
    max_depth: int
    edge_types: tuple[str, ...] | None = None
    direction: str = "both"
    repo_ids: tuple[str, ...] | None = None
    group_ids: tuple[str, ...] | None = None
    limit: int = 25
    filters: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.origin_refs:
            raise ValueError("origin_refs must not be empty")
        if self.max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        if self.limit < 1:
            raise ValueError("limit must be >= 1")
        if self.direction not in {"inbound", "outbound", "both"}:
            raise ValueError("direction must be one of inbound, outbound, both")


@dataclass(frozen=True, slots=True)
class TraversalHit:
    """One hit returned by graph traversal expansion."""

    ref: GraphRecordRef
    distance: int
    path_metadata: dict[str, Any] = field(default_factory=dict)
