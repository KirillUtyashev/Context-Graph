"""Response type definitions for MCP server tools."""

from typing import Any, Literal

from typing_extensions import NotRequired, TypedDict


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


class CodeGraphNodeRef(TypedDict):
    node_id: str
    kind: str
    name: str
    qualified_name: str | None
    file_path: str | None
    start_line: int | None
    end_line: int | None


class CodeGraphNodeTypeCount(TypedDict):
    node_kind: str
    count: int


class CodeGraphEdgeTypeCount(TypedDict):
    edge_type: str
    count: int


class CodeGraphRelationshipPattern(TypedDict):
    source_kind: str
    edge_type: str
    target_kind: str
    count: int


class CodeGraphSchemaResponse(TypedDict):
    repo_id: str
    revision_id: str
    generated_at: str
    node_type_counts: list[CodeGraphNodeTypeCount]
    edge_type_counts: list[CodeGraphEdgeTypeCount]
    relationship_patterns: list[CodeGraphRelationshipPattern]


class CodeGraphSearchMatch(CodeGraphNodeRef):
    pass


class SearchCodeGraphResponse(TypedDict):
    repo_id: str
    revision_id: str
    matches: list[CodeGraphSearchMatch]
    total: int


class CodeSnippetResponse(TypedDict):
    repo_id: str
    revision_id: str
    node: CodeGraphNodeRef
    file_path: str
    start_line: int
    end_line: int
    snippet: str


class CallTraceStep(TypedDict):
    depth: int
    edge_type: str
    source_node_id: str
    source_name: str
    source_qualified_name: str | None
    target_node_id: str
    target_name: str
    target_qualified_name: str | None


class TraceCallPathResponse(TypedDict):
    repo_id: str
    revision_id: str
    start_node: CodeGraphNodeRef
    steps: list[CallTraceStep]


ToolImplementationStatus = Literal["scaffold", "implemented", "unsupported"]
TimelineTargetKind = Literal["pull_request", "issue", "file", "symbol", "claim_cluster", "topic"]
ContextHitKind = Literal["node", "relation"]


class ToolScaffoldResponse(TypedDict):
    status: ToolImplementationStatus
    message: str
    capability: NotRequired[str]
    todo: NotRequired[str]


class ContextSearchHit(TypedDict):
    hit_kind: ContextHitKind
    record_id: str
    record_type: str
    title: str | None
    summary: str | None
    score: float | None
    lexical_score: float | None
    semantic_score: float | None
    is_active: bool
    observed_at: str | None
    source: str | None
    file_path: str | None
    relation_type: str | None
    source_record_id: str | None
    source_record_type: str | None
    target_record_id: str | None
    target_record_type: str | None
    retrieval_channels: list[str]
    matched_profile_key_text: str | None
    matched_profile_key_origin: str | None
    semantic_profile_kind: str | None


class SearchContextResponse(ToolScaffoldResponse):
    repo_id: str
    query: str
    group_id: str | None
    requested_sources: list[str]
    hits: list[ContextSearchHit]


class NeighborhoodNode(TypedDict):
    record_id: str
    record_type: str
    display_name: str | None
    summary: str | None
    observed_at: str | None


class NeighborhoodEdge(TypedDict):
    edge_type: str
    source_record_id: str
    target_record_id: str
    observed_at: str | None


class NodeNeighborhoodResponse(ToolScaffoldResponse):
    repo_id: str | None
    center_record_id: str | None
    concept: str | None
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
    target_kind: TimelineTargetKind
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
    accepted: bool
    persisted: bool
    queue_position: NotRequired[int]
