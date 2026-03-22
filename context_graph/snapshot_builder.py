from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Mapping, Optional

from .models import CanonicalEdge, CanonicalNode, GraphSnapshot, utc_now_iso


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return slug or "repo"


def _edge_id(repo_id: str, edge_type: str, source_id: str, target_id: str) -> str:
    return f"edge:{repo_id}:{edge_type}:{source_id}:{target_id}"


def _normalized_rel_path(rel_path: str) -> str:
    rel_path = rel_path.replace("\\", "/")
    while rel_path.startswith("./"):
        rel_path = rel_path[2:]
    if rel_path == ".":
        return ""
    return rel_path


def _stable_node_id(
    repo_id: str,
    *,
    kind: str,
    qualified_name: Optional[str],
    file_path: Optional[str],
    name: str,
) -> str:
    if kind == "Project":
        return f"project:{repo_id}"

    if qualified_name:
        return f"{kind.lower()}:{repo_id}:{qualified_name}"

    rel_path = _normalized_rel_path(file_path or "")
    if rel_path:
        return f"{kind.lower()}:{repo_id}:{rel_path}:{name}"
    return f"{kind.lower()}:{repo_id}:{name}"


def _coerce_properties(properties_json: Optional[str]) -> dict[str, Any]:
    raw = (properties_json or "{}").strip()
    if not raw or raw == "{}":
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw_properties_json": raw}

    if isinstance(parsed, Mapping):
        return dict(parsed)
    return {"value": parsed}


def build_snapshot_from_graph_export(
    export_payload: Mapping[str, Any],
    *,
    repo_id: Optional[str] = None,
    revision_id: str,
) -> GraphSnapshot:
    repo_path = str(export_payload["root_path"])
    project_name = str(export_payload.get("project") or repo_path)
    repo_id = repo_id or _slugify(project_name)
    generated_at = utc_now_iso()

    nodes: dict[str, CanonicalNode] = {}
    edges: dict[str, CanonicalEdge] = {}
    temp_to_stable: dict[int, str] = {}

    for node_payload in export_payload.get("nodes", []):
        kind = str(node_payload["label"])
        qualified_name = node_payload.get("qualified_name") or None
        file_path = _normalized_rel_path(str(node_payload.get("file_path") or ""))
        stable_node_id = _stable_node_id(
            repo_id,
            kind=kind,
            qualified_name=qualified_name,
            file_path=file_path or None,
            name=str(node_payload["name"]),
        )
        properties = _coerce_properties(node_payload.get("properties_json"))
        if kind == "Project":
            properties = {
                "root_path": repo_path,
                **properties,
            }

        node = CanonicalNode(
            node_id=stable_node_id,
            repo_id=repo_id,
            kind=kind,
            name=str(node_payload["name"]),
            qualified_name=str(qualified_name) if qualified_name else None,
            file_path=file_path or None,
            start_line=node_payload.get("start_line"),
            end_line=node_payload.get("end_line"),
            properties=properties,
        )
        nodes[stable_node_id] = node
        temp_to_stable[int(node_payload["temp_id"])] = stable_node_id

    for edge_payload in export_payload.get("edges", []):
        source_id = temp_to_stable.get(int(edge_payload["source_temp_id"]))
        target_id = temp_to_stable.get(int(edge_payload["target_temp_id"]))
        if not source_id or not target_id:
            continue

        edge_type = str(edge_payload["type"])
        edge = CanonicalEdge(
            edge_id=_edge_id(repo_id, edge_type, source_id, target_id),
            repo_id=repo_id,
            edge_type=edge_type,
            source_id=source_id,
            target_id=target_id,
            properties=_coerce_properties(edge_payload.get("properties_json")),
        )
        edges[edge.edge_id] = edge

    metadata = {
        "source": "codebase-memory-mcp-graph-export",
        "project_name": project_name,
        "export_node_count": export_payload.get("node_count"),
        "export_edge_count": export_payload.get("edge_count"),
    }
    return GraphSnapshot(
        repo_id=repo_id,
        revision_id=revision_id,
        root_path=repo_path,
        generated_at=generated_at,
        nodes=tuple(sorted(nodes.values(), key=lambda node: node.node_id)),
        edges=tuple(sorted(edges.values(), key=lambda edge: edge.edge_id)),
        metadata=metadata,
    )


class CodebaseMemorySnapshotProducer:
    def __init__(self, *, root: Optional[Path] = None) -> None:
        self.root = root or Path(__file__).resolve().parent.parent
        self.vendor_dir = self.root / "vendor" / "codebase_memory_pipeline"
        self.binary = self.vendor_dir / "build" / "repo-graph-export"

    def ensure_binary(self) -> None:
        if self.binary.exists():
            return
        subprocess.run(
            ["make", "repo-graph-export"],
            cwd=self.vendor_dir,
            check=True,
        )

    def export_graph(self, repo_path: str, *, repo_id: Optional[str] = None) -> dict[str, Any]:
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
        _ = limit
        export_payload = self.export_graph(repo_path, repo_id=repo_id)
        return build_snapshot_from_graph_export(
            export_payload,
            repo_id=repo_id,
            revision_id=revision_id,
        )
