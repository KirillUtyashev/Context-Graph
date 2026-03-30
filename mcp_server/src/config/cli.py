"""CLI parsing and configuration override helpers for the MCP app."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from .settings import MCPServerConfig


def build_parser() -> argparse.ArgumentParser:
    """Build the standalone MCP server CLI parser."""

    parser = argparse.ArgumentParser(
        description="Run the Context Graph MCP server with YAML configuration support"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to YAML configuration file (default: $CONFIG_PATH env var, or config/config.yaml)",
    )
    parser.add_argument(
        "--transport",
        choices=["sse", "stdio", "http"],
        help="Transport to use: http (recommended, default), stdio, or sse (deprecated)",
    )
    parser.add_argument("--host", help="Host to bind the MCP server to")
    parser.add_argument("--port", type=int, help="Port to bind the MCP server to")
    parser.add_argument(
        "--llm-provider",
        choices=["openai", "bedrock"],
        help="LLM provider to use",
    )
    parser.add_argument(
        "--embedder-provider",
        choices=["openai", "bedrock", "bedrock_titan"],
        help="Embedder provider to use",
    )
    parser.add_argument(
        "--database-provider",
        choices=["neo4j", "neptune"],
        help="Database provider to use",
    )
    parser.add_argument("--model", help="Model name to use with the LLM client")
    parser.add_argument("--small-model", help="Small model name to use with the LLM client")
    parser.add_argument(
        "--temperature",
        type=float,
        help="Temperature setting for the LLM (0.0-2.0)",
    )
    parser.add_argument("--embedder-model", help="Model name to use with the embedder")
    parser.add_argument(
        "--group-id",
        help="Namespace group id for backend-scoped MCP operations",
    )
    parser.add_argument("--user-id", help="User id for backend-scoped MCP operations")
    return parser


def apply_cli_overrides(config: MCPServerConfig, args: argparse.Namespace) -> MCPServerConfig:
    """Apply parsed CLI overrides to an instantiated settings object."""

    if getattr(args, "transport", None):
        config.server.transport = args.transport
    if getattr(args, "host", None):
        config.server.host = args.host
    if getattr(args, "port", None) is not None:
        config.server.port = args.port

    if getattr(args, "llm_provider", None):
        config.llm.provider = args.llm_provider
    if getattr(args, "model", None):
        config.llm.model = args.model
    if getattr(args, "small_model", None):
        config.llm.small_model = args.small_model
    if getattr(args, "temperature", None) is not None:
        config.llm.temperature = args.temperature

    if getattr(args, "embedder_provider", None):
        config.embedder.provider = args.embedder_provider
    if getattr(args, "embedder_model", None):
        config.embedder.model = args.embedder_model

    if getattr(args, "database_provider", None):
        config.database.provider = args.database_provider

    if getattr(args, "group_id", None):
        config.namespace.group_id = args.group_id
    if getattr(args, "user_id", None):
        config.namespace.user_id = args.user_id

    return config


def load_server_config(args: argparse.Namespace) -> MCPServerConfig:
    """Load server configuration using the configured YAML/env sources plus CLI overrides."""

    if getattr(args, "config", None):
        os.environ["CONFIG_PATH"] = str(args.config)

    config = MCPServerConfig()
    return apply_cli_overrides(config, args)


__all__ = [
    "apply_cli_overrides",
    "build_parser",
    "load_server_config",
]
