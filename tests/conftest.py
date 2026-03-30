from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVER = REPO_ROOT / "mcp_server"

mcp_server_str = str(MCP_SERVER)
if mcp_server_str not in sys.path:
    sys.path.insert(0, mcp_server_str)
