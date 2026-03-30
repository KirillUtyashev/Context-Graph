"""Mutable runtime state for one MCP app instance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config.settings import MCPServerConfig
    from .runtime.backend import MCPBackendRuntime
    from .runtime.metadata import MetadataSnapshotService


@dataclass(slots=True)
class ServerState:
    """Shared runtime state captured by registered MCP tool handlers."""

    config: MCPServerConfig | None = None
    backend_runtime: MCPBackendRuntime | None = None
    metadata_snapshot_service: MetadataSnapshotService | None = None

    def require_config(self) -> "MCPServerConfig":
        if self.config is None:
            raise RuntimeError("MCP server configuration is not initialized")
        return self.config

    def require_backend_runtime(self) -> "MCPBackendRuntime":
        if self.backend_runtime is None:
            raise RuntimeError("MCP backend runtime is not configured")
        return self.backend_runtime

    def require_metadata_snapshot_service(self) -> "MetadataSnapshotService":
        if self.metadata_snapshot_service is None:
            raise RuntimeError("Metadata snapshot service is not configured")
        return self.metadata_snapshot_service
