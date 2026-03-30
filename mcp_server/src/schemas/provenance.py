"""Schema types for provenance-oriented MCP tools."""

from __future__ import annotations

from typing_extensions import NotRequired, TypedDict

from context_graph.query.code_graph_types import CodeGraphNodeRef

from .common import ToolScaffoldResponse


class NodeContextHit(TypedDict):
    hit_kind: str
    record_id: str
    record_type: str
    title: str | None
    summary: str | None
    is_active: bool
    observed_at: str | None
    inactive_at: str | None
    file_path: NotRequired[str]
    qualified_name: NotRequired[str]


class RelationContextHit(TypedDict):
    hit_kind: str
    record_id: str
    relation_type: str | None
    summary: str | None
    is_active: bool
    observed_at: str | None
    inactive_at: str | None
    source_record_id: str | None
    source_record_type: str | None
    target_record_id: str | None
    target_record_type: str | None


class SearchContextResponse(ToolScaffoldResponse):
    repo_id: str
    query: str
    hits: list[NodeContextHit | RelationContextHit]


class NeighborhoodNode(TypedDict):
    record_id: str
    record_type: str
    display_name: str | None
    summary: str | None
    observed_at: str | None
    qualified_name: NotRequired[str]


class NeighborhoodEdge(TypedDict):
    edge_type: str
    source_record_id: str
    target_record_id: str
    observed_at: str | None


class NodeNeighborhoodResponse(ToolScaffoldResponse):
    repo_id: str | None
    center_record_id: str | None
    nodes: list[NeighborhoodNode]
    edges: list[NeighborhoodEdge]


class TimelineEvent(TypedDict):
    event_id: str
    event_type: str
    title: str | None
    summary: str | None
    observed_at: str | None
    record_id: str | None


class TimelineResponse(ToolScaffoldResponse):
    repo_id: str | None
    target_id: str
    events: list[TimelineEvent]


class CodeContextSnippet(TypedDict):
    file_path: str
    qualified_name: str | None
    start_line: int | None
    end_line: int | None
    snippet: str | None


class CodeContextResponse(ToolScaffoldResponse):
    repo_id: str
    target: str
    target_kind: str | None
    primary_symbol: CodeGraphNodeRef | None
    related_symbols: list[CodeGraphNodeRef]
    snippets: list[CodeContextSnippet]
