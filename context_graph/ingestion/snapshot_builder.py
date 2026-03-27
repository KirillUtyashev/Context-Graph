from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Optional

from ..models import GraphSnapshot
from .snapshot_parsing import build_snapshot_from_graph_export


class CodebaseMemorySnapshotProducer:
    """Runs the exporter binary and turns its JSON output into snapshots."""

    def __init__(self, *, root: Optional[Path] = None) -> None:
        self.root = root or Path(__file__).resolve().parent.parent.parent
        self.vendor_dir = self.root / "vendor" / "codebase_memory_pipeline"
        self.binary = self.vendor_dir / "build" / "repo-graph-export"

    def ensure_binary(self) -> None:
        """Build the exporter once if the binary is not present yet."""

        if self.binary.exists():
            return
        subprocess.run(
            ["make", "repo-graph-export"],
            cwd=self.vendor_dir,
            check=True,
        )

    def export_graph(self, repo_path: str, *, repo_id: Optional[str] = None) -> dict[str, Any]:
        """Run the exporter against a checkout and return its raw JSON payload."""

        self.ensure_binary()
        with tempfile.TemporaryDirectory(prefix="context-graph-export-") as tmp_dir:
            output_path = Path(tmp_dir) / "graph.json"
            command = [str(self.binary), repo_path, str(output_path)]
            if repo_id:
                command.append(repo_id)
            subprocess.run(
                command,
                cwd=self.vendor_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            return json.loads(output_path.read_text())

    def build_snapshot(
        self,
        repo_path: str,
        *,
        repo_id: Optional[str] = None,
        revision_id: str,
        limit: int = 1000000,
    ) -> GraphSnapshot:
        """Build a deterministic snapshot for one checkout revision."""

        _ = limit
        export_payload = self.export_graph(repo_path, repo_id=repo_id)
        return build_snapshot_from_graph_export(
            export_payload,
            repo_id=repo_id,
            revision_id=revision_id,
        )
