"""Typed mutation primitives for provider-neutral graph writes.

The additive backend already exposes query, search, and traversal contracts.
This module adds the write-side surface used by deterministic ingestion so we
can stop planning provider-specific Cypher outside the backend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

from .types import RecordKind


@dataclass(frozen=True, slots=True)
class NodeRecord:
    """One graph node to upsert through the backend."""

    record_id: str
    record_type: str
    repo_id: str
    group_id: str
    labels: tuple[str, ...]
    properties: Mapping[str, Any] = field(default_factory=dict)

    def normalized_properties(self) -> dict[str, Any]:
        """Return the stored node properties with canonical identity fields."""

        payload = dict(self.properties)
        payload.setdefault("record_id", self.record_id)
        payload.setdefault("record_type", self.record_type)
        payload.setdefault("repo_id", self.repo_id)
        payload.setdefault("group_id", self.group_id)
        return payload


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    """One graph relationship to upsert through the backend."""

    record_id: str
    record_type: str
    repo_id: str
    group_id: str
    relationship_type: str
    source_record_id: str
    target_record_id: str
    properties: Mapping[str, Any] = field(default_factory=dict)

    def normalized_properties(self) -> dict[str, Any]:
        """Return the stored edge properties with canonical identity fields."""

        payload = dict(self.properties)
        payload.setdefault("record_id", self.record_id)
        payload.setdefault("record_type", self.record_type)
        payload.setdefault("repo_id", self.repo_id)
        payload.setdefault("group_id", self.group_id)
        return payload


@dataclass(frozen=True, slots=True)
class SearchDocumentRecord:
    """One lexical search document maintained by the backend."""

    record_kind: RecordKind
    record_id: str
    record_type: str
    repo_id: str
    group_id: str
    search_text: str
    properties: Mapping[str, Any] = field(default_factory=dict)

    def normalized_properties(self) -> dict[str, Any]:
        """Return the stored search-document properties."""

        payload = dict(self.properties)
        payload.setdefault("record_id", self.record_id)
        payload.setdefault("record_type", self.record_type)
        payload.setdefault("repo_id", self.repo_id)
        payload.setdefault("group_id", self.group_id)
        payload["search_text"] = self.search_text
        payload["record_kind"] = self.record_kind.value
        return payload


@dataclass(frozen=True, slots=True)
class MutationPlan:
    """A provider-neutral write plan produced by higher-level ingestion code."""

    upsert_nodes: tuple[NodeRecord, ...] = ()
    upsert_edges: tuple[EdgeRecord, ...] = ()
    upsert_search_documents: tuple[SearchDocumentRecord, ...] = ()

    def is_empty(self) -> bool:
        """Return whether the plan contains any write operations."""

        return not (self.upsert_nodes or self.upsert_edges or self.upsert_search_documents)

    def operation_count(self) -> int:
        """Return the total number of typed operations in the plan."""

        return len(self.upsert_nodes) + len(self.upsert_edges) + len(self.upsert_search_documents)


def merge_mutation_plans(*plans: MutationPlan) -> MutationPlan:
    """Merge multiple plans while deduplicating by canonical record id."""

    nodes: dict[str, NodeRecord] = {}
    edges: dict[str, EdgeRecord] = {}
    search_documents: dict[tuple[RecordKind, str], SearchDocumentRecord] = {}
    for plan in plans:
        for record in plan.upsert_nodes:
            nodes[record.record_id] = record
        for record in plan.upsert_edges:
            edges[record.record_id] = record
        for record in plan.upsert_search_documents:
            search_documents[(record.record_kind, record.record_id)] = record
    return MutationPlan(
        upsert_nodes=tuple(nodes.values()),
        upsert_edges=tuple(edges.values()),
        upsert_search_documents=tuple(search_documents.values()),
    )


@dataclass(frozen=True, slots=True)
class MutationApplyResult:
    """Counts returned after applying a mutation plan."""

    nodes_upserted: int
    edges_upserted: int
    search_documents_upserted: int

    @property
    def operations_applied(self) -> int:
        """Return the total number of applied typed operations."""

        return self.nodes_upserted + self.edges_upserted + self.search_documents_upserted


class MutationOperations(ABC):
    """Provider-neutral write entrypoint used by deterministic ingestion."""

    @abstractmethod
    async def apply(self, plan: MutationPlan) -> MutationApplyResult:
        """Apply the supplied plan and return aggregate write counts."""
