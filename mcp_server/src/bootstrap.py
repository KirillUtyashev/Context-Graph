"""CLI bootstrap and runtime initialization for the MCP server."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv

from .app import DEFAULT_STATE, get_default_app
from .config import build_parser, load_server_config
from .state import ServerState

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from .runtime.backend import MCPBackendRuntime
    from .runtime.metadata import MetadataSnapshotService

mcp_server_dir = Path(__file__).resolve().parents[2]
env_file = mcp_server_dir / ".env"
if env_file.exists():
    load_dotenv(env_file)
else:
    load_dotenv()

SEMAPHORE_LIMIT = int(os.getenv("SEMAPHORE_LIMIT", 10))

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT,
    stream=sys.stderr,
)

logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("mcp.server.streamable_http_manager").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def configure_uvicorn_logging() -> None:
    """Configure uvicorn loggers to match the server logging format."""

    for logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers.clear()
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        uvicorn_logger.addHandler(handler)
        uvicorn_logger.propagate = False


def initialize_server(
    argv: Sequence[str] | None = None,
    *,
    state: ServerState = DEFAULT_STATE,
    mcp_app: Any | None = None,
    backend_runtime_cls: type["MCPBackendRuntime"] | None = None,
    metadata_snapshot_service_cls: type["MetadataSnapshotService"] | None = None,
):
    """Parse CLI arguments and initialize runtime state for the MCP server."""

    args = build_parser().parse_args(list(argv) if argv is not None else None)
    config = load_server_config(args)

    if mcp_app is None:
        mcp_app = get_default_app()

    logger.info("Using configuration:")
    logger.info("  - LLM: %s / %s", config.llm.provider, config.llm.model)
    logger.info("  - Embedder: %s / %s", config.embedder.provider, config.embedder.model)
    logger.info("  - Database: %s", config.database.provider)
    logger.info("  - Namespace Group ID: %s", config.namespace.group_id)
    logger.info("  - Transport: %s", config.server.transport)
    logger.info("  - Runtime: context_graph storage backend + metadata snapshots")

    if backend_runtime_cls is None or metadata_snapshot_service_cls is None:
        from .runtime.backend import MCPBackendRuntime
        from .runtime.metadata import MetadataSnapshotService

        backend_runtime_cls = backend_runtime_cls or MCPBackendRuntime
        metadata_snapshot_service_cls = metadata_snapshot_service_cls or MetadataSnapshotService

    state.config = config
    state.backend_runtime = backend_runtime_cls(config, semaphore_limit=SEMAPHORE_LIMIT)
    state.metadata_snapshot_service = metadata_snapshot_service_cls(config)

    if config.server.host:
        mcp_app.settings.host = config.server.host
        if config.server.host == "0.0.0.0":
            from mcp.server.transport_security import TransportSecuritySettings

            mcp_app.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            )
    if config.server.port:
        mcp_app.settings.port = config.server.port

    return config.server


async def run_mcp_server(
    argv: Sequence[str] | None = None,
    *,
    state: ServerState = DEFAULT_STATE,
    mcp_app: Any | None = None,
    backend_runtime_cls: type["MCPBackendRuntime"] | None = None,
    metadata_snapshot_service_cls: type["MetadataSnapshotService"] | None = None,
) -> None:
    """Run the MCP server in the current event loop."""

    mcp_config = initialize_server(
        argv,
        state=state,
        mcp_app=mcp_app,
        backend_runtime_cls=backend_runtime_cls,
        metadata_snapshot_service_cls=metadata_snapshot_service_cls,
    )
    logger.info("Starting MCP server with transport: %s", mcp_config.transport)
    if mcp_config.transport == "stdio":
        await mcp_app.run_stdio_async()
    elif mcp_config.transport == "sse":
        logger.info(
            "Running MCP server with SSE transport on %s:%s",
            mcp_app.settings.host,
            mcp_app.settings.port,
        )
        logger.info(
            "Access the server at: http://%s:%s/sse",
            mcp_app.settings.host,
            mcp_app.settings.port,
        )
        await mcp_app.run_sse_async()
    elif mcp_config.transport == "http":
        display_host = "localhost" if mcp_app.settings.host == "0.0.0.0" else mcp_app.settings.host
        logger.info(
            "Running MCP server with streamable HTTP transport on %s:%s",
            mcp_app.settings.host,
            mcp_app.settings.port,
        )
        logger.info("=" * 60)
        logger.info("MCP Server Access Information:")
        logger.info("  Base URL: http://%s:%s/", display_host, mcp_app.settings.port)
        logger.info("  MCP Endpoint: http://%s:%s/mcp/", display_host, mcp_app.settings.port)
        logger.info("  Transport: HTTP (streamable)")
        if os.environ.get("BROWSER", "1") == "1":
            logger.info("  FalkorDB Browser UI: http://%s:3000/", display_host)
        logger.info("=" * 60)
        logger.info("For MCP clients, connect to the /mcp/ endpoint above")
        configure_uvicorn_logging()
        await mcp_app.run_streamable_http_async()
    else:
        raise ValueError(
            f'Unsupported transport: {mcp_config.transport}. Use "sse", "stdio", or "http"'
        )


def main(argv: Sequence[str] | None = None) -> None:
    """Main function to run the Context Graph MCP server."""

    try:
        asyncio.run(run_mcp_server(argv))
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    except Exception as exc:
        logger.error("Error initializing Context Graph MCP server: %s", exc)
        raise
