"""MCP tool registration for text and regex search across indexed files."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from context_graph.models import GraphSnapshot
from context_graph.query import code_graph as code_graph_queries

from ..schemas import ErrorResponse, SearchCodeResponse
from ..state import ServerState
from .common import (
    REPO_ID_FIELD_DESCRIPTION,
    _get_metadata_snapshot,
    _normalize_repo_file_path,
    error_response,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def _iter_file_matches(
    *,
    snapshot: GraphSnapshot,
    query: str,
    file_pattern: str | None,
    limit: int,
    regex: bool,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    matcher = re.compile(query) if regex else None
    seen_files: set[str] = set()
    matches: list[dict[str, Any]] = []

    for node in snapshot.nodes:
        file_path = node.file_path
        if not file_path or file_path in seen_files:
            continue
        seen_files.add(file_path)

        if file_pattern and not fnmatch.fnmatch(file_path, file_pattern):
            continue

        absolute_path = _normalize_repo_file_path(snapshot.root_path, file_path)
        if absolute_path is None or not absolute_path.exists():
            continue

        try:
            lines = absolute_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for index, line in enumerate(lines, start=1):
            is_match = bool(matcher.search(line)) if matcher else query.lower() in line.lower()
            if not is_match:
                continue
            matches.append(
                {
                    "file_path": file_path,
                    "line_number": index,
                    "line_text": line.strip(),
                }
            )
            if len(matches) >= limit:
                return matches

    return matches


async def search_code(
    state: ServerState,
    repo_id: str,
    query: str,
    file_pattern: str | None = None,
    limit: int = 20,
    regex: bool = False,
) -> SearchCodeResponse | ErrorResponse:
    """Search indexed repository files by text or regex."""

    try:
        if limit <= 0:
            return error_response("limit must be a positive integer")

        snapshot = await _get_metadata_snapshot(state, repo_id)
        matches = await asyncio.to_thread(
            _iter_file_matches,
            snapshot=snapshot,
            query=query,
            file_pattern=file_pattern,
            limit=limit,
            regex=regex,
        )
        if not matches:
            return SearchCodeResponse(
                message=f"No code matches found for '{query}' in repo '{repo_id}'",
                matches=[],
            )

        return SearchCodeResponse(
            message=(
                f"Found {len(matches)} code match{'es' if len(matches) != 1 else ''} "
                f"for '{query}' in repo '{repo_id}'"
            ),
            matches=matches,
        )
    except code_graph_queries.CodeGraphQueryError as exc:
        return error_response(str(exc))
    except re.error as exc:
        return error_response(f"Invalid regex: {exc}")
    except Exception as exc:
        logger.error("Error in search_code: %s", exc)
        return error_response(f"Error in search_code: {exc}")


def register_search_code_tools(mcp: "FastMCP", state: ServerState) -> None:
    """Register search_code on one FastMCP instance."""

    @mcp.tool(name="search_code")
    async def search_code_tool(
        repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
        query: Annotated[
            str,
            Field(description="Text or regex pattern to search for in indexed files."),
        ],
        file_pattern: Annotated[
            str | None,
            Field(description="Optional glob filter such as '*.py' or 'src/**/*.ts'."),
        ] = None,
        limit: Annotated[int, Field(description="Maximum number of line matches to return.")] = 20,
        regex: Annotated[
            bool,
            Field(description="Treat query as a regular expression when true."),
        ] = False,
    ) -> SearchCodeResponse | ErrorResponse:
        """Search indexed repository files by text or regex."""

        return await search_code(
            state,
            repo_id,
            query,
            file_pattern=file_pattern,
            limit=limit,
            regex=regex,
        )
