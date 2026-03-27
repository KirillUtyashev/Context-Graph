"""Neo4j traversal stub for the additive backend."""

from __future__ import annotations

from context_graph.storage.backend.errors import BackendFeatureNotImplementedError
from context_graph.storage.backend.traversal import TraversalOperations
from context_graph.storage.backend.types import TraversalHit, TraversalQuery


class Neo4jTraversalOperations(TraversalOperations):
    """Expose the traversal interface while keeping V1 intentionally narrow."""

    async def expand(self, query: TraversalQuery) -> list[TraversalHit]:
        """Validate the request shape and defer full traversal semantics."""

        # `TraversalQuery` already validates depth, direction, and origins in its
        # dataclass initializer, so reaching this method means the request shape
        # is good enough for a later implementation.
        raise BackendFeatureNotImplementedError(
            "graph traversal is not implemented for the Neo4j backend in V1"
        )
