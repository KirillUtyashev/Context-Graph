from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Dict, Mapping, Optional


JsonDict = Dict[str, Any]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(dict(data), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class CanonicalNode:
    node_id: str
    repo_id: str
    kind: str
    name: str
    qualified_name: Optional[str] = None
    file_path: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    properties: Mapping[str, Any] = field(default_factory=dict)

    def properties_json(self) -> str:
        return canonical_json(self.properties)

    def to_dict(self) -> JsonDict:
        return {
            "node_id": self.node_id,
            "repo_id": self.repo_id,
            "kind": self.kind,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "properties": dict(self.properties),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CanonicalNode":
        return cls(
            node_id=str(payload["node_id"]),
            repo_id=str(payload["repo_id"]),
            kind=str(payload["kind"]),
            name=str(payload["name"]),
            qualified_name=payload.get("qualified_name"),
            file_path=payload.get("file_path"),
            start_line=payload.get("start_line"),
            end_line=payload.get("end_line"),
            properties=dict(payload.get("properties") or {}),
        )


@dataclass(frozen=True, slots=True)
class CanonicalEdge:
    edge_id: str
    repo_id: str
    edge_type: str
    source_id: str
    target_id: str
    properties: Mapping[str, Any] = field(default_factory=dict)

    def properties_json(self) -> str:
        return canonical_json(self.properties)

    def to_dict(self) -> JsonDict:
        return {
            "edge_id": self.edge_id,
            "repo_id": self.repo_id,
            "edge_type": self.edge_type,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "properties": dict(self.properties),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CanonicalEdge":
        return cls(
            edge_id=str(payload["edge_id"]),
            repo_id=str(payload["repo_id"]),
            edge_type=str(payload["edge_type"]),
            source_id=str(payload["source_id"]),
            target_id=str(payload["target_id"]),
            properties=dict(payload.get("properties") or {}),
        )


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    repo_id: str
    revision_id: str
    root_path: str
    generated_at: str
    nodes: tuple[CanonicalNode, ...]
    edges: tuple[CanonicalEdge, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def node_map(self) -> Dict[str, CanonicalNode]:
        return {node.node_id: node for node in self.nodes}

    def edge_map(self) -> Dict[str, CanonicalEdge]:
        return {edge.edge_id: edge for edge in self.edges}

    def to_dict(self) -> JsonDict:
        return {
            "repo_id": self.repo_id,
            "revision_id": self.revision_id,
            "root_path": self.root_path,
            "generated_at": self.generated_at,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GraphSnapshot":
        return cls(
            repo_id=str(payload["repo_id"]),
            revision_id=str(payload["revision_id"]),
            root_path=str(payload["root_path"]),
            generated_at=str(payload["generated_at"]),
            nodes=tuple(CanonicalNode.from_dict(node) for node in payload.get("nodes", [])),
            edges=tuple(CanonicalEdge.from_dict(edge) for edge in payload.get("edges", [])),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class NodeChange:
    old: CanonicalNode
    new: CanonicalNode


@dataclass(frozen=True, slots=True)
class EdgeChange:
    old: CanonicalEdge
    new: CanonicalEdge


@dataclass(frozen=True, slots=True)
class SnapshotDiff:
    repo_id: str
    previous_revision_id: Optional[str]
    new_revision_id: str
    added_nodes: Mapping[str, CanonicalNode]
    changed_nodes: Mapping[str, NodeChange]
    removed_nodes: Mapping[str, CanonicalNode]
    added_edges: Mapping[str, CanonicalEdge]
    changed_edges: Mapping[str, EdgeChange]
    removed_edges: Mapping[str, CanonicalEdge]

    def is_empty(self) -> bool:
        return not (
            self.added_nodes
            or self.changed_nodes
            or self.removed_nodes
            or self.added_edges
            or self.changed_edges
            or self.removed_edges
        )


@dataclass(frozen=True, slots=True)
class SnapshotApplyResult:
    snapshot: GraphSnapshot
    diff: SnapshotDiff
    operations_emitted: int
    operations_applied: int

    @property
    def statements_emitted(self) -> int:
        """Compatibility alias for older callers that still say "statements"."""

        return self.operations_emitted

    @property
    def statements_executed(self) -> int:
        """Compatibility alias for older callers that still say "statements"."""

        return self.operations_applied
