"""MCP tool registration for provenance, neighborhood, timeline, and code-context queries."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from context_graph.query import code_graph as code_graph_queries

from ..provenance import (
    NeighborhoodQueryService,
    SEARCH_CONTEXT_SOURCES,
    TimelineQueryService,
    get_code_context as build_code_context,
    get_node_neighborhood_async,
    get_timeline_async,
    search_context_async as run_search_context_async,
)
from ..schemas import (
    CodeContextResponse,
    ErrorResponse,
    NodeNeighborhoodResponse,
    SearchContextResponse,
    TimelineResponse,
)
from ..state import ServerState
from .common import (
    CODE_CONTEXT_TARGET_KIND_FIELD_DESCRIPTION,
    CODE_CONTEXT_TARGET_KIND_VALUES,
    NODE_ID_FIELD_DESCRIPTION,
    NODE_ID_REQUIRED_ERROR,
    REPO_ID_FIELD_DESCRIPTION,
    SEARCH_CONTEXT_SOURCES_FIELD_DESCRIPTION,
    TARGET_ID_FIELD_DESCRIPTION,
    ParameterValidationError,
    _get_backend_runtime,
    _get_metadata_snapshot,
    _normalize_supported_list,
    _normalize_supported_value,
    error_response,
)

logger = logging.getLogger(__name__)

SearchContextSource = Literal["claims", "observations", "relations", "code"]
CodeContextTargetKind = Literal["symbol", "function", "class", "module", "file"]

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


async def search_context(
    state: ServerState,
    repo_id: str,
    query: str,
    group_id: str | None = None,
    sources: list[SearchContextSource] | None = None,
    limit: int = 10,
    include_historical: bool = False,
) -> SearchContextResponse | ErrorResponse:
    """Search indexed provenance and code records when you do not yet have an exact target_id or node_id."""

    try:
        if limit <= 0:
            return error_response("limit must be a positive integer")
        normalized_sources = _normalize_supported_list(
            sources,
            parameter_name="sources",
            supported_values=SEARCH_CONTEXT_SOURCES,
        )

        runtime = await _get_backend_runtime(state)
        driver = await runtime.get_driver()
        effective_group_id = group_id or state.require_config().namespace.group_id
        requested_sources = normalized_sources or list(SEARCH_CONTEXT_SOURCES)
        snapshot_service = state.metadata_snapshot_service
        semantic_search = (
            snapshot_service.search_profiles_sync
            if snapshot_service is not None
            else None
        )
        return await run_search_context_async(
            driver=driver,
            semantic_search=semantic_search,
            repo_id=repo_id,
            query=query,
            group_id=effective_group_id,
            sources=requested_sources,
            limit=limit,
            include_historical=include_historical,
        )
    except ParameterValidationError as exc:
        return error_response(str(exc))
    except ValueError as exc:
        return error_response(str(exc))
    except Exception as exc:
        logger.error("Error in search_context: %s", exc)
        return error_response(f"Error in search_context: {exc}")


async def get_node_neighborhood(
    state: ServerState,
    node_id: str,
    repo_id: str | None = None,
    hops: int = 2,
    edge_types: list[str] | None = None,
) -> NodeNeighborhoodResponse | ErrorResponse:
    """Requires exact node_id. Return the neighborhood around one indexed graph node."""

    try:
        if not node_id:
            return error_response(NODE_ID_REQUIRED_ERROR)
        if hops <= 0:
            return error_response("hops must be a positive integer")

        runtime = await _get_backend_runtime(state)
        driver = await runtime.get_driver()
        snapshot = None
        if repo_id and node_id.startswith(("project:", "file:", "symbol:")):
            try:
                snapshot = await _get_metadata_snapshot(state, repo_id)
            except code_graph_queries.CodeGraphQueryError:
                snapshot = None

        return await get_node_neighborhood_async(
            service=NeighborhoodQueryService(driver=driver, snapshot=snapshot),
            repo_id=repo_id,
            node_id=node_id,
            hops=hops,
            edge_types=edge_types,
        )
    except Exception as exc:
        logger.error("Error in get_node_neighborhood: %s", exc)
        return error_response(f"Error in get_node_neighborhood: {exc}")


async def get_timeline(
    state: ServerState,
    target_id: str,
    repo_id: str | None = None,
    limit: int = 50,
) -> TimelineResponse | ErrorResponse:
    """Requires exact target_id. Return a historical provenance timeline around one graph anchor."""

    try:
        if limit <= 0:
            return error_response("limit must be a positive integer")

        runtime = await _get_backend_runtime(state)
        driver = await runtime.get_driver()
        return await get_timeline_async(
            service=TimelineQueryService(driver=driver),
            repo_id=repo_id,
            target_id=target_id,
            limit=limit,
        )
    except Exception as exc:
        logger.error("Error in get_timeline: %s", exc)
        return error_response(f"Error in get_timeline: {exc}")


async def get_code_context(
    state: ServerState,
    repo_id: str,
    target: str,
    target_kind: CodeContextTargetKind | None = None,
    include_neighbors: bool = True,
    include_references: bool = True,
    max_related_symbols: int = 10,
) -> CodeContextResponse | ErrorResponse:
    """Return a deterministic code-context slice for a file, symbol, class, or module."""

    try:
        if max_related_symbols <= 0:
            return error_response("max_related_symbols must be a positive integer")
        normalized_target_kind = _normalize_supported_value(
            target_kind,
            parameter_name="target_kind",
            supported_values=CODE_CONTEXT_TARGET_KIND_VALUES,
        )

        snapshot = await _get_metadata_snapshot(state, repo_id)
        return await asyncio.to_thread(
            build_code_context,
            snapshot=snapshot,
            repo_id=repo_id,
            target=target,
            target_kind=normalized_target_kind,
            include_neighbors=include_neighbors,
            include_references=include_references,
            max_related_symbols=max_related_symbols,
        )
    except ParameterValidationError as exc:
        return error_response(str(exc))
    except code_graph_queries.CodeGraphQueryError as exc:
        return error_response(str(exc))
    except Exception as exc:
        logger.error("Error in get_code_context: %s", exc)
        return error_response(f"Error in get_code_context: {exc}")


def register_provenance_tools(mcp: "FastMCP", state: ServerState) -> None:
    """Register provenance-oriented MCP tools on one FastMCP instance."""

    @mcp.tool(name="search_context")
    async def search_context_tool(
        repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
        query: Annotated[str, Field(description="Natural-language provenance query text.")],
        group_id: Annotated[
            str | None,
            Field(description="Optional group or namespace override. Defaults to the server namespace."),
        ] = None,
        sources: Annotated[
            list[SearchContextSource] | None,
            Field(description=SEARCH_CONTEXT_SOURCES_FIELD_DESCRIPTION),
        ] = None,
        limit: Annotated[int, Field(description="Maximum number of provenance hits to return.")] = 10,
        include_historical: Annotated[
            bool,
            Field(description="Include inactive semantic profiles for historical provenance inspection."),
        ] = False,
    ) -> SearchContextResponse | ErrorResponse:
        """Search indexed provenance and code records when you do not yet have an exact target_id or node_id."""

        return await search_context(
            state,
            repo_id,
            query,
            group_id=group_id,
            sources=sources,
            limit=limit,
            include_historical=include_historical,
        )

    @mcp.tool(name="get_node_neighborhood")
    async def get_node_neighborhood_tool(
        node_id: Annotated[str, Field(description=NODE_ID_FIELD_DESCRIPTION)],
        repo_id: Annotated[
            str | None,
            Field(description="Optional repo_id used to load a snapshot for code-node expansion."),
        ] = None,
        hops: Annotated[int, Field(description="Positive hop count for neighborhood expansion.")] = 2,
        edge_types: Annotated[
            list[str] | None,
            Field(description="Optional edge-type filters such as CALLS or DEFINES."),
        ] = None,
    ) -> NodeNeighborhoodResponse | ErrorResponse:
        """Requires exact node_id. Return the neighborhood around one indexed graph node."""

        return await get_node_neighborhood(
            state,
            node_id,
            repo_id=repo_id,
            hops=hops,
            edge_types=edge_types,
        )

    @mcp.tool(name="get_timeline")
    async def get_timeline_tool(
        target_id: Annotated[str, Field(description=TARGET_ID_FIELD_DESCRIPTION)],
        repo_id: Annotated[
            str | None,
            Field(description="Optional repo_id used to scope historical provenance assembly."),
        ] = None,
        limit: Annotated[int, Field(description="Maximum number of historical events to return.")] = 50,
    ) -> TimelineResponse | ErrorResponse:
        """Requires exact target_id. Return a historical provenance timeline around one graph anchor."""

        return await get_timeline(
            state,
            target_id,
            repo_id=repo_id,
            limit=limit,
        )

    @mcp.tool(name="get_code_context")
    async def get_code_context_tool(
        repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
        target: Annotated[
            str,
            Field(
                description=(
                    "Code target to resolve. This can be an exact qualified_name, exact node_id, "
                    "file path, or a search-like string when target_kind is provided."
                )
            ),
        ],
        target_kind: Annotated[
            CodeContextTargetKind | None,
            Field(description=CODE_CONTEXT_TARGET_KIND_FIELD_DESCRIPTION),
        ] = None,
        include_neighbors: Annotated[
            bool,
            Field(description="Include nearby sibling snippets for the resolved primary symbol."),
        ] = True,
        include_references: Annotated[
            bool,
            Field(description="Include directly related referenced symbols in the response."),
        ] = True,
        max_related_symbols: Annotated[
            int,
            Field(description="Maximum number of related symbols to include in the context slice."),
        ] = 10,
    ) -> CodeContextResponse | ErrorResponse:
        """Return a deterministic code-context slice for a file, symbol, class, or module."""

        return await get_code_context(
            state,
            repo_id,
            target,
            target_kind=target_kind,
            include_neighbors=include_neighbors,
            include_references=include_references,
            max_related_symbols=max_related_symbols,
        )
