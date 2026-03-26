"""Compatibility entrypoint for the internal Context Graph MCP server."""

from context_graph.server.mcp.server import main


if __name__ == "__main__":
    raise SystemExit(main())
