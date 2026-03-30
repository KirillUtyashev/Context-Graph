"""Schema types for code-search and code-graph MCP tools."""

from __future__ import annotations

from typing_extensions import TypedDict

from context_graph.query.code_graph_types import (
    CodeGraphNodeRef,
    CodeGraphSchemaResponse,
    CodeSnippetResponse,
    SearchCodeGraphResponse,
    TraceCallPathResponse,
)


class SearchCodeMatch(TypedDict):
    file_path: str
    line_number: int
    line_text: str


class SearchCodeResponse(TypedDict):
    message: str
    matches: list[SearchCodeMatch]


__all__ = [
    "CodeGraphNodeRef",
    "CodeGraphSchemaResponse",
    "CodeSnippetResponse",
    "SearchCodeGraphResponse",
    "SearchCodeMatch",
    "SearchCodeResponse",
    "TraceCallPathResponse",
]
