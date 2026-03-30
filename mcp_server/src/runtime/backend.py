"""Lazy backend runtime used by MCP tool handlers."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ..config.settings import MCPServerConfig

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from context_graph.backend import BackendDriver
    from context_graph.provenance import ClaimResolutionService


class MCPBackendRuntime:
    """Lazy backend runtime used by semantic MCP tools."""

    def __init__(self, config: MCPServerConfig, *, semaphore_limit: int = 10) -> None:
        self.config = config
        self.semaphore = asyncio.Semaphore(semaphore_limit)
        self.driver: BackendDriver | None = None
        self.llm_client: Any | None = None
        self.embedder_client: Any | None = None
        self.claim_resolution_service: ClaimResolutionService | None = None
        self._initialized = False
        self._init_error: str | None = None

    async def ensure_initialized(self) -> None:
        if self._initialized:
            return
        if self._init_error:
            raise RuntimeError(self._init_error)

        try:
            from context_graph.backend import create_driver
            from context_graph.provenance import ClaimResolutionService
            from context_graph.runtime.providers.factories import (
                DatabaseDriverFactory,
                EmbedderFactory,
                LLMClientFactory,
            )

            try:
                self.llm_client = LLMClientFactory.create(self.config.llm)
            except Exception as exc:
                logger.warning("Backend runtime LLM client unavailable: %s", exc)
                self.llm_client = None

            try:
                self.embedder_client = EmbedderFactory.create(self.config.embedder)
            except Exception as exc:
                logger.warning("Backend runtime embedder unavailable: %s", exc)
                self.embedder_client = None

            backend_config = DatabaseDriverFactory.create_backend_config(self.config.database)
            current_loop = asyncio.get_running_loop()
            self.driver = create_driver(backend_config)
            setattr(self.driver, "_context_graph_loop", current_loop)
            await self.driver.build_indices()
            self.claim_resolution_service = ClaimResolutionService(
                llm_client=self.llm_client,
            )
            self._initialized = True
        except Exception as exc:
            self._init_error = str(exc)
            logger.error("Failed to initialize MCP backend runtime: %s", exc)
            raise

    async def get_driver(self) -> "BackendDriver":
        await self.ensure_initialized()
        if self.driver is None:
            raise RuntimeError("backend driver failed to initialize")
        return self.driver
