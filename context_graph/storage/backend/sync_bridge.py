"""Synchronous bridge for async backend operations.

Deterministic ingestion and the CLI are still synchronous. This bridge is the
only place that runs backend coroutines from sync code.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from .driver import BackendDriver
from .maintenance import RepoResetResult, ScopeResetRequest, ScopeResetResult
from .mutations import MutationApplyResult, MutationPlan


class BackendSyncBridge:
    """Run async backend lifecycle and mutation calls from sync code."""

    def __init__(self, driver: BackendDriver):
        self.driver = driver
        self._prepared_for_write = False

    def health_check(self) -> None:
        """Validate that the backend is reachable."""

        _run_coroutine_sync(self.driver.health_check())

    def build_indices(self, *, delete_existing: bool = False) -> None:
        """Build any provider indices needed for writes and lexical search."""

        _run_coroutine_sync(self.driver.build_indices(delete_existing=delete_existing))

    def ensure_write_ready(self) -> None:
        """Run health checks and bootstrap indices once per bridge instance."""

        if self._prepared_for_write:
            return
        self.health_check()
        self.build_indices()
        self._prepared_for_write = True

    def apply(self, plan: MutationPlan) -> MutationApplyResult:
        """Apply a typed mutation plan synchronously."""

        self.ensure_write_ready()
        return _run_coroutine_sync(self.driver.mutations.apply(plan))

    def reset_repo(self, repo_id: str) -> RepoResetResult:
        """Delete one repository's graph/search state synchronously."""

        self.ensure_write_ready()
        return _run_coroutine_sync(self.driver.maintenance.reset_repo(repo_id))

    def reset_scope(self, request: ScopeResetRequest) -> ScopeResetResult:
        """Delete one scoped subset of graph/search state synchronously."""

        self.ensure_write_ready()
        return _run_coroutine_sync(self.driver.maintenance.reset_scope(request))

    def close(self) -> None:
        """Close the underlying async backend driver."""

        _run_coroutine_sync(self.driver.close())


class _PersistentLoop:
    """One background event loop shared by synchronous backend callers."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure_running(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._loop.is_running():
            return self._loop
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                daemon=True,
                name="context-graph-backend-loop",
            )
            thread.start()
            self._loop = loop
            self._thread = thread
            return loop

    def run(self, coro: Any) -> Any:
        loop = self._ensure_running()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()


_persistent_loop = _PersistentLoop()


def _run_coroutine_sync(coro: Any) -> Any:
    return _persistent_loop.run(coro)


def run_backend_coroutine_sync(coro: Any) -> Any:
    """Public helper for sync callers that must share the backend event loop."""

    return _run_coroutine_sync(coro)
