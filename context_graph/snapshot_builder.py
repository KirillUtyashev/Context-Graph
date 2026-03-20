from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path, PurePosixPath
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


def _join_segments(segments: list[str]) -> str:
    return ".".join(segment for segment in segments if segment)


def _fqn_compute(project: str, rel_path: str, name: Optional[str]) -> str:
    rel_path = _normalized_rel_path(rel_path)
    parts = list(PurePosixPath(rel_path).parts) if rel_path else []
    if parts:
        filename = parts[-1]
        stem = filename.rsplit(".", 1)[0]
        parts[-1] = stem

    segments = [project, *parts]
    if segments and segments[-1] in {"__init__", "index"} and name:
        segments.pop()
    if name:
        segments.append(name)
    return _join_segments(segments)


def _fqn_folder(project: str, rel_dir: str) -> str:
    rel_dir = _normalized_rel_path(rel_dir)
    parts = list(PurePosixPath(rel_dir).parts) if rel_dir else []
    return _join_segments([project, *parts])


def _fqn_module(project: str, rel_path: str) -> str:
    return _fqn_compute(project, rel_path, None)


def _resolve_call_target(
    callee_name: str,
    *,
    qn_index: Mapping[str, str],
    name_index: Mapping[str, list[str]],
) -> Optional[str]:
    if not callee_name:
        return None

    if callee_name in qn_index:
        return qn_index[callee_name]

    if "." in callee_name:
        suffix = callee_name.split(".")[-1]
        matches = name_index.get(suffix, [])
        if len(matches) == 1:
            return matches[0]

        suffix_matches = [node_id for qn, node_id in qn_index.items() if qn.endswith(f".{suffix}")]
        if len(suffix_matches) == 1:
            return suffix_matches[0]

    matches = name_index.get(callee_name, [])
    if len(matches) == 1:
        return matches[0]
    return None


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


def build_snapshot_from_extract_summary(
    summary: Mapping[str, Any],
    *,
    repo_id: Optional[str] = None,
    revision_id: str = "working-tree",
) -> GraphSnapshot:
    repo_path = str(summary["repo_path"])
    repo_id = repo_id or _slugify(repo_path)
    generated_at = utc_now_iso()

    nodes: dict[str, CanonicalNode] = {}
    edges: dict[str, CanonicalEdge] = {}
    qn_index: dict[str, str] = {}
    name_index: dict[str, list[str]] = defaultdict(list)
    unresolved_calls = 0
    unresolved_imports = 0

    project_node = CanonicalNode(
        node_id=f"project:{repo_id}",
        repo_id=repo_id,
        kind="Project",
        name=repo_id,
        qualified_name=repo_id,
        file_path=None,
        properties={"root_path": repo_path},
    )
    nodes[project_node.node_id] = project_node

    directory_set: set[str] = set()
    for file_payload in summary.get("files", []):
        parent = _normalized_rel_path(str(PurePosixPath(str(file_payload["rel_path"])).parent))
        while parent not in {"", "."}:
            directory_set.add(parent)
            parent = _normalized_rel_path(str(PurePosixPath(parent).parent))

    directory_paths = sorted(directory_set, key=lambda path: (path.count("/"), path))

    folder_nodes_by_dir: dict[str, str] = {}
    for rel_dir in directory_paths:
        folder_qn = _fqn_folder(repo_id, rel_dir)
        folder_node_id = f"folder:{repo_id}:{folder_qn}"
        folder_node = CanonicalNode(
            node_id=folder_node_id,
            repo_id=repo_id,
            kind="Folder",
            name=PurePosixPath(rel_dir).name,
            qualified_name=folder_qn,
            file_path=rel_dir,
            properties={},
        )
        nodes[folder_node_id] = folder_node
        qn_index[folder_qn] = folder_node_id
        folder_nodes_by_dir[rel_dir] = folder_node_id

        parent_dir = _normalized_rel_path(str(PurePosixPath(rel_dir).parent))
        parent_id = project_node.node_id if parent_dir in {".", ""} else folder_nodes_by_dir[parent_dir]
        edge_id = _edge_id(repo_id, "CONTAINS_FOLDER", parent_id, folder_node_id)
        edges[edge_id] = CanonicalEdge(
            edge_id=edge_id,
            repo_id=repo_id,
            edge_type="CONTAINS_FOLDER",
            source_id=parent_id,
            target_id=folder_node_id,
            properties={},
        )

    for file_payload in summary.get("files", []):
        rel_path = _normalized_rel_path(str(file_payload["rel_path"]))
        file_qn = _fqn_compute(repo_id, rel_path, "__file__")
        file_node_id = f"file:{repo_id}:{file_qn}"
        file_node = CanonicalNode(
            node_id=file_node_id,
            repo_id=repo_id,
            kind="File",
            name=Path(rel_path).name,
            qualified_name=file_qn,
            file_path=rel_path,
            properties={
                "language": file_payload.get("language"),
                "supported": file_payload.get("supported", False),
                "has_error": file_payload.get("has_error", False),
                "error": file_payload.get("error"),
                "size": file_payload.get("size"),
                "is_test_file": file_payload.get("is_test_file", False),
                "module_qn": file_payload.get("module_qn"),
            },
        )
        nodes[file_node.node_id] = file_node
        qn_index[file_qn] = file_node_id

        file_parent_dir = _normalized_rel_path(str(PurePosixPath(rel_path).parent))
        file_parent_id = (
            project_node.node_id
            if file_parent_dir in {".", ""}
            else folder_nodes_by_dir[file_parent_dir]
        )
        contains_edge_id = _edge_id(repo_id, "CONTAINS_FILE", file_parent_id, file_node_id)
        edges[contains_edge_id] = CanonicalEdge(
            edge_id=contains_edge_id,
            repo_id=repo_id,
            edge_type="CONTAINS_FILE",
            source_id=file_parent_id,
            target_id=file_node_id,
            properties={},
        )

        for definition in file_payload.get("defs", []):
            kind = str(definition["label"])
            qualified_name = definition.get("qualified_name")
            if not qualified_name:
                continue

            definition_node_id = f"{kind.lower()}:{repo_id}:{qualified_name}"
            definition_node = CanonicalNode(
                node_id=definition_node_id,
                repo_id=repo_id,
                kind=kind,
                name=str(definition["name"]),
                qualified_name=str(qualified_name),
                file_path=rel_path,
                start_line=definition.get("start_line"),
                end_line=definition.get("end_line"),
                properties={
                    "signature": definition.get("signature"),
                    "complexity": definition.get("complexity"),
                    "lines": definition.get("lines"),
                    "is_test": definition.get("is_test", False),
                    "is_entry_point": definition.get("is_entry_point", False),
                    "parent_class": definition.get("parent_class"),
                },
            )
            nodes[definition_node_id] = definition_node
            qn_index[definition_node.qualified_name or ""] = definition_node_id
            name_index[definition_node.name].append(definition_node_id)

            defines_edge_id = _edge_id(repo_id, "DEFINES", file_node_id, definition_node_id)
            edges[defines_edge_id] = CanonicalEdge(
                edge_id=defines_edge_id,
                repo_id=repo_id,
                edge_type="DEFINES",
                source_id=file_node_id,
                target_id=definition_node_id,
                properties={},
            )

    for file_payload in summary.get("files", []):
        for definition in file_payload.get("defs", []):
            if definition.get("label") != "Method":
                continue
            qualified_name = definition.get("qualified_name")
            parent_class = definition.get("parent_class")
            if not qualified_name or not parent_class:
                continue

            method_id = qn_index.get(str(qualified_name))
            parent_id = qn_index.get(str(parent_class))
            if not method_id or not parent_id:
                continue

            defines_method_edge_id = _edge_id(repo_id, "DEFINES_METHOD", parent_id, method_id)
            edges[defines_method_edge_id] = CanonicalEdge(
                edge_id=defines_method_edge_id,
                repo_id=repo_id,
                edge_type="DEFINES_METHOD",
                source_id=parent_id,
                target_id=method_id,
                properties={},
            )

    for file_payload in summary.get("files", []):
        rel_path = _normalized_rel_path(str(file_payload["rel_path"]))
        file_qn = _fqn_compute(repo_id, rel_path, "__file__")
        file_node_id = qn_index[file_qn]

        for import_payload in file_payload.get("imports", []):
            module_path = import_payload.get("module_path")
            target_qn = _fqn_module(repo_id, str(module_path)) if module_path else None
            target_id = qn_index.get(target_qn) if target_qn else None
            if not target_id:
                unresolved_imports += 1
                continue

            edge = CanonicalEdge(
                edge_id=_edge_id(repo_id, "IMPORTS", file_node_id, target_id),
                repo_id=repo_id,
                edge_type="IMPORTS",
                source_id=file_node_id,
                target_id=target_id,
                properties={"local_name": import_payload.get("local_name")},
            )
            edges[edge.edge_id] = edge

        for call_payload in file_payload.get("calls", []):
            source_id = qn_index.get(str(call_payload.get("enclosing_func_qn") or "")) or file_node_id
            target_id = _resolve_call_target(
                str(call_payload.get("callee_name") or ""),
                qn_index=qn_index,
                name_index=name_index,
            )
            if not target_id:
                unresolved_calls += 1
                continue

            edge = CanonicalEdge(
                edge_id=_edge_id(repo_id, "CALLS", source_id, target_id),
                repo_id=repo_id,
                edge_type="CALLS",
                source_id=source_id,
                target_id=target_id,
                properties={"callee_name": call_payload.get("callee_name")},
            )
            edges[edge.edge_id] = edge

    metadata = {
        "source": "codebase-memory-mcp-extract-summary",
        "discovered_files": summary.get("discovered_files"),
        "processed_files": summary.get("totals", {}).get("processed"),
        "supported_files": summary.get("totals", {}).get("supported"),
        "unresolved_calls": unresolved_calls,
        "unresolved_imports": unresolved_imports,
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


def build_snapshot_from_graph_export(
    export_payload: Mapping[str, Any],
    *,
    repo_id: Optional[str] = None,
    revision_id: str = "working-tree",
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
        revision_id: str = "working-tree",
        limit: int = 1000000,
    ) -> GraphSnapshot:
        _ = limit
        export_payload = self.export_graph(repo_path, repo_id=repo_id)
        return build_snapshot_from_graph_export(
            export_payload,
            repo_id=repo_id,
            revision_id=revision_id,
        )
