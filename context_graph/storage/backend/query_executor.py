"""Minimal query execution primitives for the new backend layer."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Transaction(ABC):
    """Minimal transaction handle yielded by :meth:`BackendDriver.transaction`.

    Drivers with native transactions should wrap the provider transaction here.
    Drivers without that support may use a thin wrapper that executes queries
    immediately.
    """

    @abstractmethod
    async def run(self, query: str, **kwargs: Any) -> Any:
        """Execute one query inside the current transaction."""


class QueryExecutor(ABC):
    """Small shared protocol for graph query execution.

    Search and traversal operations depend only on this protocol so they can be
    reused across providers without importing a concrete driver class.
    """

    @abstractmethod
    async def execute_query(self, query: str, **kwargs: Any) -> Any:
        """Execute one backend query and return provider-normalized records."""
