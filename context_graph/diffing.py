from __future__ import annotations

from .models import EdgeChange, GraphSnapshot, NodeChange, SnapshotDiff


def diff_snapshots(previous: GraphSnapshot | None, current: GraphSnapshot) -> SnapshotDiff:
    if previous is None:
        return SnapshotDiff(
            repo_id=current.repo_id,
            previous_revision_id=None,
            new_revision_id=current.revision_id,
            added_nodes=current.node_map(),
            changed_nodes={},
            removed_nodes={},
            added_edges=current.edge_map(),
            changed_edges={},
            removed_edges={},
        )

    previous_nodes = previous.node_map()
    current_nodes = current.node_map()
    previous_edges = previous.edge_map()
    current_edges = current.edge_map()

    added_nodes = {
        node_id: node for node_id, node in current_nodes.items() if node_id not in previous_nodes
    }
    removed_nodes = {
        node_id: node for node_id, node in previous_nodes.items() if node_id not in current_nodes
    }
    changed_nodes = {
        node_id: NodeChange(old=previous_nodes[node_id], new=node)
        for node_id, node in current_nodes.items()
        if node_id in previous_nodes and node != previous_nodes[node_id]
    }

    added_edges = {
        edge_id: edge for edge_id, edge in current_edges.items() if edge_id not in previous_edges
    }
    removed_edges = {
        edge_id: edge for edge_id, edge in previous_edges.items() if edge_id not in current_edges
    }
    changed_edges = {
        edge_id: EdgeChange(old=previous_edges[edge_id], new=edge)
        for edge_id, edge in current_edges.items()
        if edge_id in previous_edges and edge != previous_edges[edge_id]
    }

    return SnapshotDiff(
        repo_id=current.repo_id,
        previous_revision_id=previous.revision_id,
        new_revision_id=current.revision_id,
        added_nodes=added_nodes,
        changed_nodes=changed_nodes,
        removed_nodes=removed_nodes,
        added_edges=added_edges,
        changed_edges=changed_edges,
        removed_edges=removed_edges,
    )
