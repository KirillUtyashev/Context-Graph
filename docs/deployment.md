# Deployment

The deployable MCP app source of truth is `mcp_server/`.

Deployment assets should package:

- `context_graph/` as the core library
- `mcp_server/` as the standalone MCP app

For EC2-style deployments, the target runtime should install both packages into the same virtual environment and start the app via:

```bash
context-graph-mcp --config /path/to/config.yaml
```
