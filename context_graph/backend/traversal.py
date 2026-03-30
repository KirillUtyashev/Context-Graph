"""Generic traversal interface for the additive backend."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .types import TraversalHit, TraversalQuery


class TraversalOperations(ABC):
    """Provider-neutral graph expansion entrypoint."""

    @abstractmethod
    async def expand(self, query: TraversalQuery) -> list[TraversalHit]:
        """Expand outward from the provided record references."""
