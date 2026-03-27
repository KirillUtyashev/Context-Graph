"""Administrative backend operations for scoped cleanup and repo resets."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScopeResetRequest:
    """Describe one repo-scoped delete request against the graph backend."""

    repo_id: str
    node_record_types: tuple[str, ...] = ()
    edge_record_types: tuple[str, ...] = ()
    delete_search_documents: bool = True
    search_document_record_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScopeResetResult:
    """Counts returned after deleting one backend-owned record scope."""

    repo_id: str
    nodes_deleted: int
    edges_deleted: int
    search_documents_deleted: int

    @property
    def total_deleted(self) -> int:
        """Return the total number of deleted backend-owned records."""

        return self.nodes_deleted + self.edges_deleted + self.search_documents_deleted


@dataclass(frozen=True, slots=True)
class RepoResetResult:
    """Counts returned after deleting one repository's graph/search state."""

    repo_id: str
    nodes_deleted: int
    edges_deleted: int
    search_documents_deleted: int

    @property
    def total_deleted(self) -> int:
        """Return the total number of deleted backend-owned records."""

        return self.nodes_deleted + self.edges_deleted + self.search_documents_deleted


class MaintenanceOperations(ABC):
    """Administrative delete/reset operations owned by the backend."""

    @abstractmethod
    async def reset_repo(self, repo_id: str) -> RepoResetResult:
        """Delete all backend-owned graph/search records for one repository."""

    @abstractmethod
    async def reset_scope(self, request: ScopeResetRequest) -> ScopeResetResult:
        """Delete a repo-scoped subset of graph/search records."""
