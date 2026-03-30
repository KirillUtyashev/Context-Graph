"""Response type definitions for MCP server tools."""

from typing import Any, Literal

from typing_extensions import NotRequired, TypedDict

from context_graph.query.code_graph_types import (
    CallTraceStep,
    CodeGraphEdgeTypeCount,
    CodeGraphNodeRef,
    CodeGraphNodeTypeCount,
    CodeGraphRelationshipPattern,
    CodeGraphSchemaResponse,
    CodeGraphSearchMatch,
    CodeSnippetResponse,
    SearchCodeGraphResponse,
    TraceCallPathResponse,
)


class ErrorResponse(TypedDict):
    error: str


class SuccessResponse(TypedDict):
    message: str


class NodeResult(TypedDict):
    uuid: str
    name: str
    labels: list[str]
    created_at: str | None
    summary: str | None
    group_id: str
    attributes: dict[str, Any]


class NodeSearchResponse(TypedDict):
    message: str
    nodes: list[NodeResult]


class FactSearchResponse(TypedDict):
    message: str
    facts: list[dict[str, Any]]


class EpisodeSearchResponse(TypedDict):
    message: str
    episodes: list[dict[str, Any]]


class StatusResponse(TypedDict):
    status: str
    message: str


class ProfileResult(TypedDict):
    profile_id: str
    profile_kind: str
    profile_target_kind: str
    profile_target_id: str
    relation_type: str | None
    value_text: str
    source_node_id: str | None
    target_node_id: str | None
    is_active: bool
    score: float
    matched_key_text: str
    matched_key_origin: str
    metadata: dict[str, Any]


class SearchMemoryResponse(TypedDict):
    message: str
    facts: list[dict[str, Any]]
    profiles: list[ProfileResult]


class CodeSearchMatch(TypedDict):
    file_path: str
    line_number: int
    line_text: str


class SearchCodeResponse(TypedDict):
    message: str
    matches: list[CodeSearchMatch]


ToolImplementationStatus = Literal["scaffold", "implemented", "unsupported"]
ContextHitKind = Literal["node", "relation"]


class ToolScaffoldResponse(TypedDict):
    status: ToolImplementationStatus
    message: str
    capability: NotRequired[str]
    todo: NotRequired[str]


class NodeContextHit(TypedDict):
    hit_kind: Literal["node"]
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
    hit_kind: Literal["relation"]
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


ContextSearchHit = NodeContextHit | RelationContextHit


class SearchContextResponse(ToolScaffoldResponse):
    repo_id: str
    query: str
    hits: list[ContextSearchHit]


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


class RecordAgentObservationResponse(ToolScaffoldResponse):
    repo_id: str
    observation_id: str | None
    task_id: str | None
    claim_ids: list[str]
    accepted: bool
    persisted: bool
