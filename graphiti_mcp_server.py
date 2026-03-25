"""Compatibility wrapper for the internal Context Graph MCP server."""

from context_graph.server.mcp.server import *  # noqa: F401,F403


if __name__ == "__main__":
    raise SystemExit(main())
