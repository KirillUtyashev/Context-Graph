"""MCP tool registration for deterministic code-graph queries."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from context_graph.query import code_graph as code_graph_queries

from ..schemas import (
    CodeGraphSchemaResponse,
    CodeSnippetResponse,
    ErrorResponse,
    SearchCodeGraphResponse,
    TraceCallPathResponse,
)
from ..state import ServerState
from .common import (
    QUALIFIED_NAME_FIELD_DESCRIPTION,
    REPO_ID_FIELD_DESCRIPTION,
    TRACE_DIRECTION_FIELD_DESCRIPTION,
    TRACE_DIRECTION_VALUES,
    ParameterValidationError,
    _get_metadata_snapshot,
    _normalize_supported_value,
    error_response,
)

logger = logging.getLogger(__name__)

TraceDirection = Literal["inbound", "outbound", "both"]

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


async def get_code_graph_schema(
    state: ServerState,
    repo_id: str,
) -> CodeGraphSchemaResponse | ErrorResponse:
    """Return structural node kinds, edge types, and relationship patterns for the latest snapshot."""

    try:
        snapshot = await _get_metadata_snapshot(state, repo_id)
        return await asyncio.to_thread(code_graph_queries.get_code_graph_schema, snapshot)
    except code_graph_queries.CodeGraphQueryError as exc:
        return error_response(str(exc))
    except Exception as exc:
        logger.error("Error in get_code_graph_schema: %s", exc)
        return error_response(f"Error in get_code_graph_schema: {exc}")


async def search_code_graph(
    state: ServerState,
    repo_id: str,
    query: str | None = None,
    node_kinds: list[str] | None = None,
    file_pattern: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> SearchCodeGraphResponse | ErrorResponse:
    """Search structural code nodes in the latest indexed snapshot when exact ids are not yet known."""

    try:
        snapshot = await _get_metadata_snapshot(state, repo_id)
        return await asyncio.to_thread(
            code_graph_queries.search_code_graph,
            snapshot,
            query=query,
            node_kinds=node_kinds,
            file_pattern=file_pattern,
            limit=limit,
            offset=offset,
        )
    except code_graph_queries.CodeGraphQueryError as exc:
        return error_response(str(exc))
    except Exception as exc:
        logger.error("Error in search_code_graph: %s", exc)
        return error_response(f"Error in search_code_graph: {exc}")


async def trace_call_path(
    state: ServerState,
    repo_id: str,
    qualified_name: str,
    direction: TraceDirection = "both",
    depth: int = 3,
) -> TraceCallPathResponse | ErrorResponse:
    """Requires exact qualified_name. Trace bounded BFS call relationships from one indexed symbol."""

    try:
        normalized_direction = _normalize_supported_value(
            direction,
            parameter_name="direction",
            supported_values=TRACE_DIRECTION_VALUES,
        )
        snapshot = await _get_metadata_snapshot(state, repo_id)
        return await asyncio.to_thread(
            code_graph_queries.trace_call_path,
            snapshot,
            qualified_name=qualified_name,
            direction=normalized_direction,
            depth=depth,
        )
    except ParameterValidationError as exc:
        return error_response(str(exc))
    except code_graph_queries.CodeGraphQueryError as exc:
        return error_response(str(exc))
    except Exception as exc:
        logger.error("Error in trace_call_path: %s", exc)
        return error_response(f"Error in trace_call_path: {exc}")


async def get_code_snippet(
    state: ServerState,
    repo_id: str,
    qualified_name: str,
    include_neighbors: bool = False,
) -> CodeSnippetResponse | ErrorResponse:
    """Requires exact qualified_name. Return source code for one indexed symbol in the latest snapshot."""

    try:
        snapshot = await _get_metadata_snapshot(state, repo_id)
        return await asyncio.to_thread(
            code_graph_queries.get_code_snippet,
            snapshot,
            qualified_name=qualified_name,
            include_neighbors=include_neighbors,
        )
    except code_graph_queries.CodeGraphQueryError as exc:
        return error_response(str(exc))
    except Exception as exc:
        logger.error("Error in get_code_snippet: %s", exc)
        return error_response(f"Error in get_code_snippet: {exc}")


def register_code_graph_tools(mcp: "FastMCP", state: ServerState) -> None:
    """Register code-graph MCP tools on one FastMCP instance."""

    @mcp.tool(name="get_code_graph_schema")
    async def get_code_graph_schema_tool(
        repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
    ) -> CodeGraphSchemaResponse | ErrorResponse:
        """Return structural node kinds, edge types, and relationship patterns for the latest snapshot."""

        return await get_code_graph_schema(state, repo_id)

    @mcp.tool(name="search_code_graph")
    async def search_code_graph_tool(
        repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
        query: Annotated[
            str | None,
            Field(
                description="Optional search text for symbol names, qualified names, or file paths."
            ),
        ] = None,
        node_kinds: Annotated[
            list[str] | None,
            Field(
                description="Optional structural node kinds such as Function, Class, Module, or File."
            ),
        ] = None,
        file_pattern: Annotated[
            str | None,
            Field(description="Optional glob filter applied to indexed file paths."),
        ] = None,
        limit: Annotated[int, Field(description="Maximum number of ranked matches to return.")] = 20,
        offset: Annotated[int, Field(description="Pagination offset into the ranked match set.")] = 0,
    ) -> SearchCodeGraphResponse | ErrorResponse:
        """Search structural code nodes in the latest indexed snapshot when exact ids are not yet known."""

        return await search_code_graph(
            state,
            repo_id,
            query=query,
            node_kinds=node_kinds,
            file_pattern=file_pattern,
            limit=limit,
            offset=offset,
        )

    @mcp.tool(name="trace_call_path")
    async def trace_call_path_tool(
        repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
        qualified_name: Annotated[str, Field(description=QUALIFIED_NAME_FIELD_DESCRIPTION)],
        direction: Annotated[
            TraceDirection,
            Field(description=TRACE_DIRECTION_FIELD_DESCRIPTION),
        ] = "both",
        depth: Annotated[int, Field(description="Bounded traversal depth from 1 to 5.")] = 3,
    ) -> TraceCallPathResponse | ErrorResponse:
        """Requires exact qualified_name. Trace bounded BFS call relationships from one indexed symbol."""

        return await trace_call_path(
            state,
            repo_id,
            qualified_name,
            direction=direction,
            depth=depth,
        )

    @mcp.tool(name="get_code_snippet")
    async def get_code_snippet_tool(
        repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
        qualified_name: Annotated[str, Field(description=QUALIFIED_NAME_FIELD_DESCRIPTION)],
        include_neighbors: Annotated[
            bool,
            Field(description="Include a small amount of surrounding source context when true."),
        ] = False,
    ) -> CodeSnippetResponse | ErrorResponse:
        """Requires exact qualified_name. Return source code for one indexed symbol in the latest snapshot."""

        return await get_code_snippet(
            state,
            repo_id,
            qualified_name,
            include_neighbors=include_neighbors,
        )
