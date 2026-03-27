"""Response type definitions for MCP server tools."""

from typing import Any

from typing_extensions import TypedDict


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
    relation_type: str | None
    value_text: str
    source_node_id: str | None
    target_node_id: str | None
    is_active: bool
    score: float
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
