from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_SRC = REPO_ROOT / "mcp_server" / "src"

app_src_str = str(APP_SRC)
if app_src_str not in sys.path:
    sys.path.insert(0, app_src_str)
