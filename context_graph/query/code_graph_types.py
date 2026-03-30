"""Typed response payloads for deterministic code-graph queries."""

from typing import TypedDict


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
