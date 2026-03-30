"""Health route registration for the MCP app."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register_health_routes(mcp: "FastMCP") -> None:
    """Register operational health routes on one FastMCP instance."""

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request):
        """Health check endpoint for Docker and load balancers."""

        from starlette.responses import JSONResponse

        return JSONResponse({"status": "healthy", "service": "context-graph-mcp"})
