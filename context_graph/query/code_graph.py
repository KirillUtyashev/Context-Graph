"""Deterministic snapshot-backed query helpers for code-graph tooling."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
import fnmatch
from pathlib import Path
from typing import Any

from context_graph.models.graph import CanonicalEdge, CanonicalNode, GraphSnapshot

from .code_graph_types import (
    CallTraceStep,
    CodeGraphEdgeTypeCount,
    CodeGraphNodeRef,
    CodeGraphNodeTypeCount,
    CodeGraphRelationshipPattern,
    CodeGraphSchemaResponse,
    CodeGraphSearchMatch,
    CodeSnippetResponse,
    SearchCodeGraphResponse,
    TraceCallPathResponse,
)

DEFAULT_CALL_EDGE_TYPES = ("CALLS",)
SNIPPET_CONTEXT_LINES = 5


class CodeGraphQueryError(ValueError):
    """Raised when a deterministic code-graph query cannot be satisfied."""


def load_latest_snapshot(metadata_store: Any, repo_id: str) -> GraphSnapshot:
    """Load the latest indexed snapshot for one repository or raise a query error."""

    snapshot = metadata_store.load_latest_snapshot(repo_id)
    if snapshot is None:
        raise CodeGraphQueryError(f"No indexed snapshot found for repo_id={repo_id}")
    return snapshot


def get_code_graph_schema(snapshot: GraphSnapshot) -> CodeGraphSchemaResponse:
    """Summarize structural node kinds, edge types, and relationship patterns."""

    node_kind_counts = Counter(node.kind for node in snapshot.nodes)
    edge_type_counts = Counter(edge.edge_type for edge in snapshot.edges)
    node_map = snapshot.node_map()
    relationship_counts = Counter[
        tuple[str, str, str]
    ]()

    for edge in snapshot.edges:
        source = node_map.get(edge.source_id)
        target = node_map.get(edge.target_id)
        if source is None or target is None:
            continue
        relationship_counts[(source.kind, edge.edge_type, target.kind)] += 1

    node_type_counts: list[CodeGraphNodeTypeCount] = [
        CodeGraphNodeTypeCount(node_kind=node_kind, count=count)
        for node_kind, count in sorted(node_kind_counts.items())
    ]
    edge_type_counts_payload: list[CodeGraphEdgeTypeCount] = [
        CodeGraphEdgeTypeCount(edge_type=edge_type, count=count)
        for edge_type, count in sorted(edge_type_counts.items())
    ]
    relationship_patterns: list[CodeGraphRelationshipPattern] = [
        CodeGraphRelationshipPattern(
            source_kind=source_kind,
            edge_type=edge_type,
            target_kind=target_kind,
            count=count,
        )
        for (source_kind, edge_type, target_kind), count in sorted(relationship_counts.items())
    ]
    return CodeGraphSchemaResponse(
        repo_id=snapshot.repo_id,
        revision_id=snapshot.revision_id,
        generated_at=snapshot.generated_at,
        node_type_counts=node_type_counts,
        edge_type_counts=edge_type_counts_payload,
        relationship_patterns=relationship_patterns,
    )


def search_code_graph(
    snapshot: GraphSnapshot,
    *,
    query: str | None = None,
    node_kinds: list[str] | None = None,
    file_pattern: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> SearchCodeGraphResponse:
    """Search structural code nodes in one deterministic snapshot."""

    if limit < 1:
        raise CodeGraphQueryError("limit must be a positive integer")
    if offset < 0:
        raise CodeGraphQueryError("offset must be zero or greater")

    normalized_query = (query or "").strip()
    normalized_query_lower = normalized_query.lower()
    kind_filter = {kind.lower() for kind in node_kinds or ()}

    ranked_matches: list[tuple[tuple[Any, ...], CodeGraphSearchMatch]] = []
    for node in snapshot.nodes:
        if kind_filter and node.kind.lower() not in kind_filter:
            continue
        if file_pattern and not fnmatch.fnmatch(node.file_path or "", file_pattern):
            continue

        rank = _search_rank(node, normalized_query_lower)
        if rank is None:
            continue

        match = CodeGraphSearchMatch(**_node_ref(node))
        sort_key = (
            rank,
            (node.qualified_name or node.name).lower(),
            (node.file_path or "").lower(),
            node.start_line or 0,
            node.node_id,
        )
        ranked_matches.append((sort_key, match))

    ranked_matches.sort(key=lambda item: item[0])
    total = len(ranked_matches)
    page = [match for _, match in ranked_matches[offset:offset + limit]]
    return SearchCodeGraphResponse(
        repo_id=snapshot.repo_id,
        revision_id=snapshot.revision_id,
        matches=page,
        total=total,
    )


def get_code_snippet(
    snapshot: GraphSnapshot,
    *,
    qualified_name: str,
    include_neighbors: bool = False,
) -> CodeSnippetResponse:
    """Read source code for one exact qualified name from the snapshot root."""

    node = _require_exact_node(snapshot, qualified_name)
    if not node.file_path:
        raise CodeGraphQueryError(f"Indexed symbol '{qualified_name}' has no file path")

    absolute_path = _normalize_repo_file_path(snapshot.root_path, node.file_path)
    if absolute_path is None or not absolute_path.exists():
        raise CodeGraphQueryError(f"Source file not found for '{qualified_name}': {node.file_path}")

    lines = absolute_path.read_text(encoding="utf-8", errors="replace").splitlines()
    raw_start = node.start_line or 1
    raw_end = node.end_line or raw_start
    if include_neighbors:
        start_line = max(1, raw_start - SNIPPET_CONTEXT_LINES)
        end_line = min(len(lines), raw_end + SNIPPET_CONTEXT_LINES)
    else:
        start_line = raw_start
        end_line = min(len(lines), raw_end)

    snippet = "\n".join(lines[start_line - 1:end_line])
    return CodeSnippetResponse(
        repo_id=snapshot.repo_id,
        revision_id=snapshot.revision_id,
        node=_node_ref(node),
        file_path=node.file_path,
        start_line=start_line,
        end_line=end_line,
        snippet=snippet,
    )


def trace_call_path(
    snapshot: GraphSnapshot,
    *,
    qualified_name: str,
    direction: str = "both",
    depth: int = 3,
) -> TraceCallPathResponse:
    """Trace inbound and outbound ``CALLS`` edges from one exact qualified name."""

    normalized_direction = direction.lower()
    if normalized_direction not in {"inbound", "outbound", "both"}:
        raise CodeGraphQueryError("direction must be one of: inbound, outbound, both")
    if depth < 1 or depth > 5:
        raise CodeGraphQueryError("depth must be between 1 and 5")

    start_node = _require_exact_node(snapshot, qualified_name)
    node_map = snapshot.node_map()
    node_sort_names = {
        node.node_id: (node.qualified_name or node.name).lower()
        for node in snapshot.nodes
    }
    outbound: dict[str, list[tuple[tuple[str, str, str, str], CanonicalEdge]]] = defaultdict(list)
    inbound: dict[str, list[tuple[tuple[str, str, str, str], CanonicalEdge]]] = defaultdict(list)
    allowed_types = {edge_type.upper() for edge_type in DEFAULT_CALL_EDGE_TYPES}

    for edge in snapshot.edges:
        if edge.edge_type.upper() not in allowed_types:
            continue
        outbound[edge.source_id].append(
            (_edge_sort_key(edge, node_sort_names, traversal_direction="outbound"), edge)
        )
        inbound[edge.target_id].append(
            (_edge_sort_key(edge, node_sort_names, traversal_direction="inbound"), edge)
        )

    for bucket in outbound.values():
        if len(bucket) > 1:
            bucket.sort(key=lambda item: item[0])
    for bucket in inbound.values():
        if len(bucket) > 1:
            bucket.sort(key=lambda item: item[0])

    steps: list[CallTraceStep] = []
    visited_edges: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(start_node.node_id, 0)])

    while queue:
        current_node_id, current_depth = queue.popleft()
        if current_depth >= depth:
            continue

        adjacent: list[tuple[str, CanonicalEdge]] = []
        if normalized_direction in {"outbound", "both"}:
            adjacent.extend(("outbound", edge) for _, edge in outbound.get(current_node_id, ()))
        if normalized_direction in {"inbound", "both"}:
            adjacent.extend(("inbound", edge) for _, edge in inbound.get(current_node_id, ()))

        for traversal_direction, edge in adjacent:
            if edge.edge_id in visited_edges:
                continue
            visited_edges.add(edge.edge_id)

            if traversal_direction == "outbound":
                source_id = edge.source_id
                target_id = edge.target_id
            else:
                source_id = edge.target_id
                target_id = edge.source_id

            source_node = node_map.get(source_id)
            target_node = node_map.get(target_id)
            if source_node is None or target_node is None:
                continue

            steps.append(
                CallTraceStep(
                    depth=current_depth + 1,
                    edge_type=edge.edge_type,
                    source_node_id=source_node.node_id,
                    source_name=source_node.name,
                    source_qualified_name=source_node.qualified_name,
                    target_node_id=target_node.node_id,
                    target_name=target_node.name,
                    target_qualified_name=target_node.qualified_name,
                )
            )
            queue.append((target_id, current_depth + 1))

    return TraceCallPathResponse(
        repo_id=snapshot.repo_id,
        revision_id=snapshot.revision_id,
        start_node=_node_ref(start_node),
        steps=steps,
    )


def _require_exact_node(snapshot: GraphSnapshot, qualified_name: str) -> CanonicalNode:
    """Resolve one node by exact qualified name or raise a query error."""

    match = next((node for node in snapshot.nodes if node.qualified_name == qualified_name), None)
    if match is None:
        raise CodeGraphQueryError(
            f"No indexed symbol found for '{qualified_name}' in repo '{snapshot.repo_id}'"
        )
    return match


def _search_rank(node: CanonicalNode, normalized_query_lower: str) -> int | None:
    """Return a deterministic ranking bucket for one node search match."""

    if not normalized_query_lower:
        return 4

    qualified_name = (node.qualified_name or "").lower()
    name = node.name.lower()
    if qualified_name == normalized_query_lower:
        return 0
    if name == normalized_query_lower:
        return 1
    if qualified_name.endswith(f".{normalized_query_lower}") or qualified_name.endswith(normalized_query_lower):
        return 2
    if normalized_query_lower in qualified_name or normalized_query_lower in name:
        return 3
    return None


def _normalize_repo_file_path(root_path: str, file_path: str | None) -> Path | None:
    """Resolve one repo-relative file path from the snapshot root."""

    if not file_path:
        return None
    return (Path(root_path) / file_path).resolve()


def _node_ref(node: CanonicalNode) -> CodeGraphNodeRef:
    """Project one canonical node into the shared response shape."""

    return CodeGraphNodeRef(
        node_id=node.node_id,
        kind=node.kind,
        name=node.name,
        qualified_name=node.qualified_name,
        file_path=node.file_path,
        start_line=node.start_line,
        end_line=node.end_line,
    )


def _edge_sort_key(
    edge: CanonicalEdge,
    node_sort_names: dict[str, str],
    *,
    traversal_direction: str,
) -> tuple[str, str, str, str]:
    """Build a stable sort key for BFS traversal adjacency lists."""

    if traversal_direction == "outbound":
        source_id = edge.source_id
        target_id = edge.target_id
    else:
        source_id = edge.target_id
        target_id = edge.source_id

    return (
        edge.edge_type,
        node_sort_names.get(source_id, ""),
        node_sort_names.get(target_id, ""),
        edge.edge_id,
    )
