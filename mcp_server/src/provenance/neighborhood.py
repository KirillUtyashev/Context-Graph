"""Exact-node neighborhood queries for provenance and code records."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from context_graph.backend import BackendDriver

from ..schemas.provenance import NeighborhoodEdge, NeighborhoodNode, NodeNeighborhoodResponse
from .models import IMPLEMENTED_STATUS
from .shared import (
    _expand_backend_neighborhood_async,
    _neighborhood_edge_payload,
    _neighborhood_node_payload,
    _normalize_text,
    _run_async,
)


class NeighborhoodQueryService:
    """Exact-node neighborhood expansion boundary."""

    def __init__(
        self,
        *,
        driver: BackendDriver | None = None,
        snapshot: Any | None = None,
    ) -> None:
        self.driver = driver
        self.snapshot = snapshot

    def get_node_neighborhood(
        self,
        *,
        repo_id: str | None,
        node_id: str | None,
        hops: int = 2,
        edge_types: Sequence[str] | None = None,
    ) -> NodeNeighborhoodResponse:
        return _run_async(
            self.get_node_neighborhood_async(
                repo_id=repo_id,
                node_id=node_id,
                hops=hops,
                edge_types=edge_types,
            )
        )

    async def get_node_neighborhood_async(
        self,
        *,
        repo_id: str | None,
        node_id: str | None,
        hops: int = 2,
        edge_types: Sequence[str] | None = None,
    ) -> NodeNeighborhoodResponse:
        if not node_id:
            raise ValueError("get_node_neighborhood requires an exact node_id")
        if hops <= 0:
            raise ValueError("hops must be a positive integer")
        if self.driver is None:
            raise RuntimeError("backend driver is required for neighborhood queries")

        if self._is_snapshot_code_node(node_id):
            if self.snapshot is None:
                raise ValueError(
                    "repo_id with an indexed snapshot is required for file/project/symbol neighborhood queries"
                )
            return self._get_snapshot_neighborhood(
                repo_id=repo_id or self.snapshot.repo_id,
                node_id=node_id,
                hops=hops,
                edge_types=edge_types,
            )

        center_record, node_records, edge_records = await _expand_backend_neighborhood_async(
            driver=self.driver,
            node_id=node_id,
            repo_id=repo_id,
            hops=hops,
            edge_types=edge_types,
        )
        nodes = tuple(
            sorted(
                (
                    _neighborhood_node_payload(record)
                    for record in (center_record, *node_records)
                ),
                key=lambda item: (item["record_id"] != node_id, item["record_type"], item["record_id"]),
            )
        )
        edges = tuple(
            sorted(
                (_neighborhood_edge_payload(record) for record in edge_records),
                key=lambda item: (
                    item["edge_type"],
                    item["source_record_id"],
                    item["target_record_id"],
                ),
            )
        )
        return NodeNeighborhoodResponse(
            status=IMPLEMENTED_STATUS,
            message="Expanded the exact node neighborhood from the indexed graph.",
            repo_id=repo_id,
            center_record_id=node_id,
            nodes=list(nodes),
            edges=list(edges),
        )

    def _is_snapshot_code_node(self, node_id: str) -> bool:
        return node_id.startswith(("project:", "file:", "symbol:"))

    def _get_snapshot_neighborhood(
        self,
        *,
        repo_id: str,
        node_id: str,
        hops: int,
        edge_types: Sequence[str] | None,
    ) -> NodeNeighborhoodResponse:
        if self.snapshot is None:
            raise ValueError("snapshot is required for deterministic code neighborhood queries")

        node_map = self.snapshot.node_map()
        center = node_map.get(node_id)
        if center is None:
            raise ValueError(f"No indexed code node found for node_id={node_id}")

        normalized_edge_types = {
            _normalize_text(edge_type).upper()
            for edge_type in edge_types or ()
            if _normalize_text(edge_type)
        }
        adjacency: dict[str, list[Any]] = {}
        for edge in self.snapshot.edges:
            if normalized_edge_types and edge.edge_type.upper() not in normalized_edge_types:
                continue
            adjacency.setdefault(edge.source_id, []).append(edge)
            adjacency.setdefault(edge.target_id, []).append(edge)

        distances: dict[str, int] = {center.node_id: 0}
        queue: list[str] = [center.node_id]
        collected_edges: dict[str, Any] = {}
        while queue:
            current = queue.pop(0)
            current_distance = distances[current]
            if current_distance >= hops:
                continue
            for edge in adjacency.get(current, ()):
                collected_edges[edge.edge_id] = edge
                neighbor_id = edge.target_id if edge.source_id == current else edge.source_id
                if neighbor_id not in distances:
                    distances[neighbor_id] = current_distance + 1
                    queue.append(neighbor_id)

        nodes = sorted(
            (
                NeighborhoodNode(
                    record_id=node.node_id,
                    record_type=node.kind,
                    display_name=node.qualified_name or node.name,
                    summary=node.file_path or None,
                    observed_at=None,
                    qualified_name=node.qualified_name,
                )
                for node_id_key, distance in distances.items()
                if (node := node_map.get(node_id_key)) is not None and distance <= hops
            ),
            key=lambda item: (item["record_id"] != center.node_id, item["record_type"], item["record_id"]),
        )
        edges = sorted(
            (
                NeighborhoodEdge(
                    edge_type=edge.edge_type,
                    source_record_id=edge.source_id,
                    target_record_id=edge.target_id,
                    observed_at=None,
                )
                for edge in collected_edges.values()
            ),
            key=lambda item: (item["edge_type"], item["source_record_id"], item["target_record_id"]),
        )
        return NodeNeighborhoodResponse(
            status=IMPLEMENTED_STATUS,
            message="Expanded the exact code-node neighborhood from the indexed snapshot.",
            repo_id=repo_id,
            center_record_id=center.node_id,
            nodes=list(nodes),
            edges=list(edges),
        )


def get_node_neighborhood(
    *,
    service: NeighborhoodQueryService,
    repo_id: str | None,
    node_id: str | None,
    hops: int,
    edge_types: Sequence[str] | None,
) -> NodeNeighborhoodResponse:
    return service.get_node_neighborhood(
        repo_id=repo_id,
        node_id=node_id,
        hops=hops,
        edge_types=edge_types,
    )


async def get_node_neighborhood_async(
    *,
    service: NeighborhoodQueryService,
    repo_id: str | None,
    node_id: str | None,
    hops: int,
    edge_types: Sequence[str] | None,
) -> NodeNeighborhoodResponse:
    return await service.get_node_neighborhood_async(
        repo_id=repo_id,
        node_id=node_id,
        hops=hops,
        edge_types=edge_types,
    )
