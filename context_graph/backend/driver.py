"""Base driver abstractions shared by concrete backend implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from .query_executor import QueryExecutor, Transaction
from .maintenance import MaintenanceOperations
from .mutations import MutationOperations
from .search import SearchOperations
from .traversal import TraversalOperations
from .types import BackendCapabilities, BackendProvider


class BackendSession(ABC):
    """Minimal session wrapper exposed by concrete backend drivers."""

    provider: BackendProvider

    async def __aenter__(self) -> "BackendSession":
        return self

    @abstractmethod
    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Close or release the underlying provider session."""

    @abstractmethod
    async def run(self, query: str, **kwargs: Any) -> Any:
        """Execute one query on the current session."""

    @abstractmethod
    async def close(self) -> None:
        """Close the session."""

    @abstractmethod
    async def execute_write(self, func, *args, **kwargs) -> Any:
        """Run a provider-native write helper when one exists."""


class BackendDriver(QueryExecutor, ABC):
    """Common lifecycle and capability surface for graph backends."""

    provider: BackendProvider
    capabilities: BackendCapabilities

    @property
    @abstractmethod
    def search(self) -> SearchOperations:
        """Return the provider search implementation."""

    @property
    @abstractmethod
    def mutations(self) -> MutationOperations:
        """Return the provider mutation implementation."""

    @property
    @abstractmethod
    def maintenance(self) -> MaintenanceOperations:
        """Return the provider maintenance/reset implementation."""

    @property
    @abstractmethod
    def traversal(self) -> TraversalOperations:
        """Return the provider traversal implementation."""

    @abstractmethod
    async def execute_query(self, query: str, **kwargs: Any) -> Any:
        """Execute one provider query and return normalized records."""

    @abstractmethod
    def session(self) -> BackendSession:
        """Return a backend session wrapper."""

    @abstractmethod
    async def close(self) -> None:
        """Close the backend connection."""

    @abstractmethod
    async def build_indices(self, delete_existing: bool = False) -> None:
        """Create or refresh provider search and lookup indices."""

    @abstractmethod
    async def health_check(self) -> None:
        """Raise when the backend is not reachable."""

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        """Provide a best-effort transaction wrapper.

        Drivers with native transaction support should override this. The default
        implementation falls back to a session-backed wrapper where queries are
        executed immediately.
        """

        session = self.session()
        try:
            yield _SessionTransaction(session)
        finally:
            await session.close()


class _SessionTransaction(Transaction):
    """Fallback transaction wrapper used by drivers without native transactions."""

    def __init__(self, session: BackendSession):
        self._session = session

    async def run(self, query: str, **kwargs: Any) -> Any:
        return await self._session.run(query, **kwargs)
