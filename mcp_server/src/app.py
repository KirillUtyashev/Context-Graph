"""App construction for the Context Graph MCP server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .state import ServerState
from .tools.code_graph import register_code_graph_tools
from .tools.health import register_health_routes
from .tools.observations import register_observation_tools
from .tools.provenance import register_provenance_tools
from .tools.search_code import register_search_code_tools

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

CONTEXT_GRAPH_MCP_INSTRUCTIONS = """
Context Graph exposes a small, opinionated MCP surface for repository analysis.

Tool selection guide:
- Use search_code when you need text or regex matches in indexed repository files.
- Use get_code_graph_schema when you need the structural node kinds and edge types in the latest snapshot.
- Use search_code_graph when you are discovering symbols, files, or modules and do not yet know an exact qualified_name.
- Use trace_call_path when you already know an exact qualified_name and need bounded inbound or outbound CALLS edges.
- Use get_code_snippet when you already know an exact qualified_name and need the source text for that symbol.
- Use get_code_context when you want a deterministic summary around a file or symbol target and can optionally narrow with target_kind.
- Use search_context when you need provenance, claim, observation, relation, or code retrieval and do not yet know an exact graph anchor.
- Use get_node_neighborhood when you already know an exact node_id and need immediate graph neighbors around that anchor.
- Use get_timeline when you already know an exact target_id and need claim-history events around that anchor.
- Use record_agent_observation when you want to persist an agent note or trace into the provenance pipeline.

Identifier primer:
- repo_id: stable repository identifier used at indexing time, for example `owner-repo`.
- qualified_name: fully qualified code symbol such as `pkg.module.func`.
- node_id: exact indexed graph id such as `project:owner-repo`, `file:owner-repo:path/to/file.py`, or `symbol:owner-repo:pkg.module.func`.
- target_id: exact provenance graph id such as `claim:123`, `observation:456`, or `evidence:789`.

Recovery rule:
- If an exact id is unknown, start with a search tool, inspect the returned ids, then switch to the exact-id tool.

Code tools operate on the latest stored GraphSnapshot for a repo_id.
search_context runs separate node and relation provenance retrieval pipelines, with optional historical semantic retrieval.
get_node_neighborhood requires an exact node_id and uses deterministic snapshot expansion for code nodes.
get_timeline requires an exact target_id and returns claim-only historical events around that graph anchor.
"""


def build_app(
    state: ServerState | None = None,
    *,
    fastmcp_cls: type["FastMCP"] | None = None,
) -> Any:
    """Build a FastMCP app bound to one mutable server state object."""

    if fastmcp_cls is None:
        from mcp.server.fastmcp import FastMCP as fastmcp_cls

    server_state = state or ServerState()
    mcp = fastmcp_cls(
        "Context Graph MCP",
        instructions=CONTEXT_GRAPH_MCP_INSTRUCTIONS,
    )
    register_search_code_tools(mcp, server_state)
    register_code_graph_tools(mcp, server_state)
    register_provenance_tools(mcp, server_state)
    register_observation_tools(mcp, server_state)
    register_health_routes(mcp)
    return mcp


DEFAULT_STATE = ServerState()
_default_app: Any | None = None


def get_default_app() -> Any:
    """Return the lazily constructed default FastMCP app."""

    global _default_app
    if _default_app is None:
        _default_app = build_app(DEFAULT_STATE)
    return _default_app

__all__ = [
    "CONTEXT_GRAPH_MCP_INSTRUCTIONS",
    "DEFAULT_STATE",
    "build_app",
    "get_default_app",
]
