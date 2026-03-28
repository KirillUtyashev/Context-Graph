"""Neptune traversal stub for the additive backend."""

from __future__ import annotations

from context_graph.storage.backend.errors import BackendFeatureNotImplementedError
from context_graph.storage.backend.traversal import TraversalOperations
from context_graph.storage.backend.types import TraversalHit, TraversalQuery


class NeptuneTraversalOperations(TraversalOperations):  # pragma: no cover
    """Expose the traversal interface while deferring traversal semantics."""

    async def expand(self, query: TraversalQuery) -> list[TraversalHit]:
        """Validate the request shape and defer graph expansion to a later pass."""

        raise BackendFeatureNotImplementedError(
            "graph traversal is not implemented for the Neptune backend in V1"
        )
