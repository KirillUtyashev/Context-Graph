"""Shared constants and helpers for MCP tool handlers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from context_graph.models import GraphSnapshot
from context_graph.provenance.models import ObservationKind
from context_graph.query import code_graph as code_graph_queries

from ..schemas.common import ErrorResponse
from ..state import ServerState

if TYPE_CHECKING:
    from ..runtime.backend import MCPBackendRuntime

TraceDirectionValues = tuple[str, ...]

TRACE_DIRECTION_VALUES: TraceDirectionValues = ("inbound", "outbound", "both")
CODE_CONTEXT_TARGET_KIND_VALUES = ("symbol", "function", "class", "module", "file")
BUILT_IN_OBSERVATION_KIND_VALUES = tuple(kind.value for kind in ObservationKind)

REPO_ID_FIELD_DESCRIPTION = (
    "Stable repository identifier used at indexing time. Example: 'owner-repo'."
)
QUALIFIED_NAME_FIELD_DESCRIPTION = (
    "Exact fully qualified code symbol. Example: 'pkg.module.func'."
)
NODE_ID_FIELD_DESCRIPTION = (
    "Exact indexed graph node identifier. Examples: 'project:owner-repo', "
    "'file:owner-repo:path/to/file.py', or 'symbol:owner-repo:pkg.module.func'."
)
TARGET_ID_FIELD_DESCRIPTION = (
    "Exact provenance graph identifier. Examples: 'claim:123', 'observation:456', "
    "or 'evidence:789'."
)
TRACE_DIRECTION_FIELD_DESCRIPTION = (
    "Call traversal direction. Supported values: inbound, outbound, both."
)
SEARCH_CONTEXT_SOURCES_FIELD_DESCRIPTION = (
    "Optional source filters for provenance retrieval. Supported values: claims, "
    "observations, relations, code."
)
CODE_CONTEXT_TARGET_KIND_FIELD_DESCRIPTION = (
    "Optional target disambiguation when resolving code context. Supported values: "
    "symbol, function, class, module, file."
)
OBSERVATION_KIND_FIELD_DESCRIPTION = (
    "Observation kind to persist. Built-in kinds: "
    f"{', '.join(BUILT_IN_OBSERVATION_KIND_VALUES)}. Custom values are allowed but "
    "intended for advanced callers."
)
METADATA_FIELD_DESCRIPTION = (
    "Optional JSON metadata stored with the observation. This is pass-through context "
    "and does not control routing or retrieval behavior."
)
NODE_ID_REQUIRED_ERROR = (
    "node_id is required and must be an exact id such as project:owner-repo, "
    "file:owner-repo:path/to/file.py, or symbol:owner-repo:pkg.module.func"
)


class ParameterValidationError(ValueError):
    """Raised when one MCP parameter uses an unsupported public value."""


def _current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_repo_file_path(root_path: str, file_path: str | None) -> Path | None:
    if not file_path:
        return None
    return (Path(root_path) / file_path).resolve()


def _supported_values_error(parameter_name: str, supported_values: tuple[str, ...]) -> str:
    return f"{parameter_name} must be one of: {', '.join(supported_values)}"


def _normalize_supported_value(
    value: str | None,
    *,
    parameter_name: str,
    supported_values: tuple[str, ...],
) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in supported_values:
        raise ParameterValidationError(_supported_values_error(parameter_name, supported_values))
    return normalized


def _normalize_supported_list(
    values: list[str] | None,
    *,
    parameter_name: str,
    supported_values: tuple[str, ...],
) -> list[str] | None:
    if values is None:
        return None

    normalized_values: list[str] = []
    seen: set[str] = set()
    invalid_values: list[str] = []
    for raw_value in values:
        normalized = (raw_value or "").strip().lower()
        if normalized not in supported_values:
            invalid_values.append(str(raw_value))
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        normalized_values.append(normalized)

    if invalid_values:
        raise ParameterValidationError(
            f"{parameter_name} must only contain: {', '.join(supported_values)}"
        )
    return normalized_values


async def _get_backend_runtime(state: ServerState) -> "MCPBackendRuntime":
    runtime = state.require_backend_runtime()
    await runtime.ensure_initialized()
    return runtime


async def _get_metadata_snapshot(state: ServerState, repo_id: str) -> GraphSnapshot:
    snapshot_service = state.require_metadata_snapshot_service()
    ready = await asyncio.to_thread(snapshot_service.initialize_metadata_store)
    metadata_store = snapshot_service.metadata_store
    if not ready or metadata_store is None:
        if snapshot_service.metadata_init_error:
            raise code_graph_queries.CodeGraphQueryError(
                f"Metadata store is unavailable: {snapshot_service.metadata_init_error}"
            )
        raise code_graph_queries.CodeGraphQueryError(
            f"No indexed snapshot found for repo_id={repo_id}"
        )

    snapshot = await asyncio.to_thread(
        code_graph_queries.load_latest_snapshot,
        metadata_store,
        repo_id,
    )
    if snapshot is None:
        raise code_graph_queries.CodeGraphQueryError(
            f"No indexed snapshot found for repo_id={repo_id}"
        )
    return snapshot


def error_response(message: str) -> ErrorResponse:
    return ErrorResponse(error=message)
