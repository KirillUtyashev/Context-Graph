"""Deterministic code-context assembly helpers."""

from __future__ import annotations

from typing import Any

from ..schemas.provenance import CodeContextResponse
from .models import IMPLEMENTED_STATUS
from .shared import _context_snippets, _node_ref, _related_nodes, _resolve_primary_node


def get_code_context(
    *,
    snapshot: Any,
    repo_id: str,
    target: str,
    target_kind: str | None,
    include_neighbors: bool,
    include_references: bool,
    max_related_symbols: int,
) -> CodeContextResponse:
    primary = _resolve_primary_node(snapshot, target=target, target_kind=target_kind)
    related_nodes = _related_nodes(
        snapshot,
        primary=primary,
        include_references=include_references,
        limit=max_related_symbols,
    )
    snippets = _context_snippets(
        snapshot,
        primary=primary,
        related_nodes=related_nodes,
        include_neighbors=include_neighbors,
    )
    return CodeContextResponse(
        status=IMPLEMENTED_STATUS,
        message="Built a deterministic code-context slice from the indexed snapshot.",
        repo_id=repo_id,
        target=target,
        target_kind=target_kind,
        primary_symbol=_node_ref(primary),
        related_symbols=[_node_ref(node) for node in related_nodes],
        snippets=snippets,
    )
