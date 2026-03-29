#!/usr/bin/env python3
"""Context Graph MCP server for snapshot-backed code inspection and provenance tools."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import fnmatch
import logging
import os
import re
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from context_graph.models import GraphSnapshot
from context_graph.provenance.models import ObservationKind
from context_graph.server.mcp import code_graph as code_graph_queries
from context_graph.server.mcp import provenance_tools
from context_graph.server.mcp.backend_runtime import (
    MCPBackendRuntime,
    MetadataSnapshotService,
)
from context_graph.server.mcp.config import MCPServerConfig, ServerConfig
from context_graph.server.mcp.request_types import ObservationWriteRequest
from context_graph.server.mcp.response_types import (
    CodeGraphSchemaResponse,
    CodeContextResponse,
    CodeSnippetResponse,
    ErrorResponse,
    NodeNeighborhoodResponse,
    RecordAgentObservationResponse,
    SearchCodeGraphResponse,
    SearchCodeResponse,
    SearchContextResponse,
    TimelineResponse,
    TraceCallPathResponse,
)

# Load .env file from the server package when present.
mcp_server_dir = Path(__file__).parent.parent
env_file = mcp_server_dir / ".env"
if env_file.exists():
    load_dotenv(env_file)
else:
    load_dotenv()


# This bounds concurrent backend/LLM work for provenance-oriented tools.
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


def configure_uvicorn_logging() -> None:
    """Configure uvicorn loggers to match the server logging format."""

    for logger_name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers.clear()
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        uvicorn_logger.addHandler(handler)
        uvicorn_logger.propagate = False


logger = logging.getLogger(__name__)

config: MCPServerConfig
backend_runtime: MCPBackendRuntime | None = None
metadata_snapshot_service: MetadataSnapshotService | None = None


TraceDirection = Literal["inbound", "outbound", "both"]
SearchContextSource = Literal["claims", "observations", "relations", "code"]
CodeContextTargetKind = Literal["symbol", "function", "class", "module", "file"]

TRACE_DIRECTION_VALUES = ("inbound", "outbound", "both")
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


CONTEXT_GRAPH_MCP_INSTRUCTIONS = f"""
Context Graph exposes a small, opinionated MCP surface for repository analysis.

Tool selection guide:
- Use search_code when you need text or regex matches in indexed repository files.
- Use get_code_graph_schema when you need the structural node kinds and edge types in the latest snapshot.
- Use search_code_graph when you are discovering symbols, files, or modules and do not yet know an exact qualified_name.
- Use trace_call_path when you already know an exact qualified_name and need bounded inbound or outbound CALLS edges.
- Use get_code_snippet when you already know an exact qualified_name and need the source text for that symbol.
- Use get_code_context when you want a deterministic summary around a file or symbol target and can optionally narrow with target_kind.
- Use search_context when you need provenance, claim, observation, relation, or code retrieval and do not yet know an exact graph anchor.
- Use get_node_neighborhood when you already know an exact node_id and need immediate graph neighbors around that anchor.
- Use get_timeline when you already know an exact target_id and need claim-history events around that anchor.
- Use record_agent_observation when you want to persist an agent note or trace into the provenance pipeline.

Identifier primer:
- repo_id: stable repository identifier used at indexing time, for example `owner-repo`.
- qualified_name: fully qualified code symbol such as `pkg.module.func`.
- node_id: exact indexed graph id such as `project:owner-repo`, `file:owner-repo:path/to/file.py`, or `symbol:owner-repo:pkg.module.func`.
- target_id: exact provenance graph id such as `claim:123`, `observation:456`, or `evidence:789`.

Recovery rule:
- If an exact id is unknown, start with a search tool, inspect the returned ids, then switch to the exact-id tool.

Code tools operate on the latest stored GraphSnapshot for a repo_id.
search_context runs separate node and relation provenance retrieval pipelines, with optional historical semantic retrieval.
get_node_neighborhood requires an exact node_id and uses deterministic snapshot expansion for code nodes.
get_timeline requires an exact target_id and returns claim-only historical events around that graph anchor.
"""

mcp = FastMCP(
    "Context Graph MCP",
    instructions=CONTEXT_GRAPH_MCP_INSTRUCTIONS,
)


def _require_metadata_snapshot_service() -> MetadataSnapshotService:
    if metadata_snapshot_service is None:
        raise code_graph_queries.CodeGraphQueryError("Metadata snapshot service is not configured")
    return metadata_snapshot_service


def _require_backend_runtime() -> MCPBackendRuntime:
    if backend_runtime is None:
        raise RuntimeError("MCP backend runtime is not configured")
    return backend_runtime


async def _get_backend_runtime() -> MCPBackendRuntime:
    runtime = _require_backend_runtime()
    await runtime.ensure_initialized()
    return runtime


async def _persist_agent_observation_request(
    request: ObservationWriteRequest,
) -> RecordAgentObservationResponse:
    from context_graph.provenance import (
        ClaimBundleValidator,
        ClaimEvidenceExtractionService,
        ClaimProfileKeyGenerationService,
        ClaimResolutionCandidateRetriever,
        ClaimResolutionService,
        ClaimSemanticRetrievalService,
        EvidenceInvalidationPlanner,
        EvidenceMutationPlanner,
        FactConflictResolutionService,
        ObservationMutationPlanner,
    )
    from context_graph.provenance.observation_ingestion import (
        ObservationIngestionItem,
        ObservationIngestionService,
    )
    from context_graph.provenance.source_adapters import (
        AgentObservationAdapter,
        AgentObservationPayload,
    )
    from context_graph.query_parser import QueryParser
    from context_graph.server.mcp.provenance_tools import IMPLEMENTED_STATUS

    snapshot_service = _require_metadata_snapshot_service()
    runtime = await _get_backend_runtime()
    ready = await asyncio.to_thread(snapshot_service.initialize_metadata_store)
    metadata_store = snapshot_service.metadata_store
    if not ready or metadata_store is None:
        message = snapshot_service.metadata_init_error or "metadata store is unavailable"
        raise RuntimeError(message)

    await asyncio.to_thread(snapshot_service.initialize_profile_search)
    snapshot = await asyncio.to_thread(metadata_store.load_latest_snapshot, request.repo_id)

    observation_planner = ObservationMutationPlanner()
    claim_extraction_service = (
        ClaimEvidenceExtractionService(llm_client=runtime.llm_client)
        if runtime.llm_client is not None
        else None
    )
    evidence_planner = EvidenceMutationPlanner()
    claim_profile_key_generation_service = (
        ClaimProfileKeyGenerationService(llm_client=runtime.llm_client)
        if runtime.llm_client is not None
        else None
    )
    claim_resolution_service = runtime.claim_resolution_service or (
        ClaimResolutionService(llm_client=runtime.llm_client)
        if runtime.llm_client is not None
        else None
    )
    fact_conflict_resolution_service = (
        FactConflictResolutionService(llm_client=runtime.llm_client)
        if runtime.llm_client is not None
        else None
    )
    semantic_claim_search_service = (
        ClaimSemanticRetrievalService(
            metadata_store=metadata_store,
            vector_store=snapshot_service.profile_indexer.vector_store,
            embedding_provider=snapshot_service.profile_indexer.embedding_provider,
            query_parser=QueryParser(llm_client=runtime.llm_client),
        )
        if snapshot_service.profile_indexer is not None
        else None
    )
    claim_candidate_retriever = ClaimResolutionCandidateRetriever(
        backend_driver=runtime.driver,
        semantic_claim_search_service=semantic_claim_search_service,
    )
    evidence_invalidation_planner = EvidenceInvalidationPlanner(
        backend_driver=runtime.driver,
        evidence_planner=evidence_planner,
        claim_candidate_retriever=claim_candidate_retriever,
        claim_profile_key_generation_service=claim_profile_key_generation_service,
        claim_resolution_service=claim_resolution_service,
        fact_conflict_resolution_service=fact_conflict_resolution_service,
    )
    observation_ingestion_service = ObservationIngestionService(
        metadata_store=metadata_store,
        backend_driver=runtime.driver,
        observation_planner=observation_planner,
        claim_extraction_service=claim_extraction_service,
        claim_validator=ClaimBundleValidator(),
        evidence_invalidation_planner=evidence_invalidation_planner,
        profile_indexer=snapshot_service.profile_indexer,
    )
    planned_observation = AgentObservationAdapter().build_observation(
        planner=observation_planner,
        payload=AgentObservationPayload(
            repo_id=request.repo_id,
            observation_text=request.observation_text,
            observation_kind=request.observation_kind,
            title=request.title,
            source_kind=request.source_kind,
            source_id=request.source_id,
            context_kind=request.context_kind,
            context_id=request.context_id,
            observed_at=request.observed_at,
            task_id=request.task_id,
            metadata=request.metadata,
        ),
    )
    apply_result = await asyncio.to_thread(
        observation_ingestion_service.apply_ingestion,
        items=(
            ObservationIngestionItem(
                planned_observation=planned_observation,
                source_context={
                    "document_path": planned_observation.observation.metadata.get("document_path"),
                },
            ),
        ),
        snapshot=snapshot,
        execute=True,
    )
    return {
        "status": IMPLEMENTED_STATUS,
        "message": "Persisted the observation and indexed it for provenance retrieval.",
        "capability": "context_graph.backend.ingestion.observation_write",
        "repo_id": request.repo_id,
        "observation_id": planned_observation.observation.observation_id,
        "task_id": request.task_id,
        "claim_ids": list(apply_result.result.claim_ids),
        "accepted": True,
        "persisted": True,
    }


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


async def _get_metadata_snapshot(repo_id: str) -> GraphSnapshot:
    snapshot_service = _require_metadata_snapshot_service()
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


def _iter_file_matches(
    *,
    snapshot: GraphSnapshot,
    query: str,
    file_pattern: str | None,
    limit: int,
    regex: bool,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    matcher = re.compile(query) if regex else None
    seen_files: set[str] = set()
    matches: list[dict[str, Any]] = []

    for node in snapshot.nodes:
        file_path = node.file_path
        if not file_path or file_path in seen_files:
            continue
        seen_files.add(file_path)

        if file_pattern and not fnmatch.fnmatch(file_path, file_pattern):
            continue

        absolute_path = _normalize_repo_file_path(snapshot.root_path, file_path)
        if absolute_path is None or not absolute_path.exists():
            continue

        try:
            lines = absolute_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for index, line in enumerate(lines, start=1):
            is_match = bool(matcher.search(line)) if matcher else query.lower() in line.lower()
            if not is_match:
                continue
            matches.append(
                {
                    "file_path": file_path,
                    "line_number": index,
                    "line_text": line.strip(),
                }
            )
            if len(matches) >= limit:
                return matches

    return matches


@mcp.tool()
async def search_code(
    repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
    query: Annotated[str, Field(description="Text or regex pattern to search for in indexed files.")],
    file_pattern: Annotated[
        str | None,
        Field(description="Optional glob filter such as '*.py' or 'src/**/*.ts'."),
    ] = None,
    limit: Annotated[int, Field(description="Maximum number of line matches to return.")] = 20,
    regex: Annotated[bool, Field(description="Treat query as a regular expression when true.")] = False,
) -> SearchCodeResponse | ErrorResponse:
    """Search indexed repository files by text or regex."""

    try:
        if limit <= 0:
            return ErrorResponse(error="limit must be a positive integer")

        snapshot = await _get_metadata_snapshot(repo_id)
        matches = await asyncio.to_thread(
            _iter_file_matches,
            snapshot=snapshot,
            query=query,
            file_pattern=file_pattern,
            limit=limit,
            regex=regex,
        )
        if not matches:
            return SearchCodeResponse(
                message=f"No code matches found for '{query}' in repo '{repo_id}'",
                matches=[],
            )

        return SearchCodeResponse(
            message=(
                f"Found {len(matches)} code match{'es' if len(matches) != 1 else ''} "
                f"for '{query}' in repo '{repo_id}'"
            ),
            matches=matches,
        )
    except code_graph_queries.CodeGraphQueryError as exc:
        return ErrorResponse(error=str(exc))
    except re.error as exc:
        return ErrorResponse(error=f"Invalid regex: {exc}")
    except Exception as exc:
        logger.error("Error in search_code: %s", exc)
        return ErrorResponse(error=f"Error in search_code: {exc}")


@mcp.tool()
async def get_code_graph_schema(
    repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
) -> CodeGraphSchemaResponse | ErrorResponse:
    """Return structural node kinds, edge types, and relationship patterns for the latest snapshot."""

    try:
        snapshot = await _get_metadata_snapshot(repo_id)
        return await asyncio.to_thread(code_graph_queries.get_code_graph_schema, snapshot)
    except code_graph_queries.CodeGraphQueryError as exc:
        return ErrorResponse(error=str(exc))
    except Exception as exc:
        logger.error("Error in get_code_graph_schema: %s", exc)
        return ErrorResponse(error=f"Error in get_code_graph_schema: {exc}")


@mcp.tool()
async def search_code_graph(
    repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
    query: Annotated[
        str | None,
        Field(description="Optional search text for symbol names, qualified names, or file paths."),
    ] = None,
    node_kinds: Annotated[
        list[str] | None,
        Field(description="Optional structural node kinds such as Function, Class, Module, or File."),
    ] = None,
    file_pattern: Annotated[
        str | None,
        Field(description="Optional glob filter applied to indexed file paths."),
    ] = None,
    limit: Annotated[int, Field(description="Maximum number of ranked matches to return.")] = 20,
    offset: Annotated[int, Field(description="Pagination offset into the ranked match set.")] = 0,
) -> SearchCodeGraphResponse | ErrorResponse:
    """Search structural code nodes in the latest indexed snapshot when exact ids are not yet known."""

    try:
        snapshot = await _get_metadata_snapshot(repo_id)
        return await asyncio.to_thread(
            code_graph_queries.search_code_graph,
            snapshot,
            query=query,
            node_kinds=node_kinds,
            file_pattern=file_pattern,
            limit=limit,
            offset=offset,
        )
    except code_graph_queries.CodeGraphQueryError as exc:
        return ErrorResponse(error=str(exc))
    except Exception as exc:
        logger.error("Error in search_code_graph: %s", exc)
        return ErrorResponse(error=f"Error in search_code_graph: {exc}")


@mcp.tool()
async def trace_call_path(
    repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
    qualified_name: Annotated[str, Field(description=QUALIFIED_NAME_FIELD_DESCRIPTION)],
    direction: Annotated[
        TraceDirection,
        Field(description=TRACE_DIRECTION_FIELD_DESCRIPTION),
    ] = "both",
    depth: Annotated[int, Field(description="Bounded traversal depth from 1 to 5.")] = 3,
) -> TraceCallPathResponse | ErrorResponse:
    """Requires exact qualified_name. Trace bounded BFS call relationships from one indexed symbol."""

    try:
        normalized_direction = _normalize_supported_value(
            direction,
            parameter_name="direction",
            supported_values=TRACE_DIRECTION_VALUES,
        )
        snapshot = await _get_metadata_snapshot(repo_id)
        return await asyncio.to_thread(
            code_graph_queries.trace_call_path,
            snapshot,
            qualified_name=qualified_name,
            direction=normalized_direction,
            depth=depth,
        )
    except ParameterValidationError as exc:
        return ErrorResponse(error=str(exc))
    except code_graph_queries.CodeGraphQueryError as exc:
        return ErrorResponse(error=str(exc))
    except Exception as exc:
        logger.error("Error in trace_call_path: %s", exc)
        return ErrorResponse(error=f"Error in trace_call_path: {exc}")


@mcp.tool()
async def get_code_snippet(
    repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
    qualified_name: Annotated[str, Field(description=QUALIFIED_NAME_FIELD_DESCRIPTION)],
    include_neighbors: Annotated[
        bool,
        Field(description="Include a small amount of surrounding source context when true."),
    ] = False,
) -> CodeSnippetResponse | ErrorResponse:
    """Requires exact qualified_name. Return source code for one indexed symbol in the latest snapshot."""

    try:
        snapshot = await _get_metadata_snapshot(repo_id)
        return await asyncio.to_thread(
            code_graph_queries.get_code_snippet,
            snapshot,
            qualified_name=qualified_name,
            include_neighbors=include_neighbors,
        )
    except code_graph_queries.CodeGraphQueryError as exc:
        return ErrorResponse(error=str(exc))
    except Exception as exc:
        logger.error("Error in get_code_snippet: %s", exc)
        return ErrorResponse(error=f"Error in get_code_snippet: {exc}")


@mcp.tool()
async def search_context(
    repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
    query: Annotated[str, Field(description="Natural-language provenance query text.")],
    group_id: Annotated[
        str | None,
        Field(description="Optional group or namespace override. Defaults to the server namespace."),
    ] = None,
    sources: Annotated[
        list[SearchContextSource] | None,
        Field(description=SEARCH_CONTEXT_SOURCES_FIELD_DESCRIPTION),
    ] = None,
    limit: Annotated[int, Field(description="Maximum number of provenance hits to return.")] = 10,
    include_historical: Annotated[
        bool,
        Field(description="Include inactive semantic profiles for historical provenance inspection."),
    ] = False,
) -> SearchContextResponse | ErrorResponse:
    """Search indexed provenance and code records when you do not yet have an exact target_id or node_id."""

    try:
        if limit <= 0:
            return ErrorResponse(error="limit must be a positive integer")
        normalized_sources = _normalize_supported_list(
            sources,
            parameter_name="sources",
            supported_values=provenance_tools.SEARCH_CONTEXT_SOURCES,
        )

        runtime = await _get_backend_runtime()
        driver = await runtime.get_driver()
        effective_group_id = group_id or config.namespace.group_id
        requested_sources = normalized_sources or list(provenance_tools.SEARCH_CONTEXT_SOURCES)
        semantic_search = (
            metadata_snapshot_service.search_profiles_sync
            if metadata_snapshot_service is not None
            else None
        )
        return await provenance_tools.search_context_async(
            driver=driver,
            semantic_search=semantic_search,
            repo_id=repo_id,
            query=query,
            group_id=effective_group_id,
            sources=requested_sources,
            limit=limit,
            include_historical=include_historical,
        )
    except ParameterValidationError as exc:
        return ErrorResponse(error=str(exc))
    except ValueError as exc:
        return ErrorResponse(error=str(exc))
    except Exception as exc:
        logger.error("Error in search_context: %s", exc)
        return ErrorResponse(error=f"Error in search_context: {exc}")


@mcp.tool()
async def get_node_neighborhood(
    node_id: Annotated[str, Field(description=NODE_ID_FIELD_DESCRIPTION)],
    repo_id: Annotated[
        str | None,
        Field(description="Optional repo_id used to load a snapshot for code-node expansion."),
    ] = None,
    hops: Annotated[int, Field(description="Positive hop count for neighborhood expansion.")] = 2,
    edge_types: Annotated[
        list[str] | None,
        Field(description="Optional edge-type filters such as CALLS or DEFINES."),
    ] = None,
) -> NodeNeighborhoodResponse | ErrorResponse:
    """Requires exact node_id. Return the neighborhood around one indexed graph node."""

    try:
        if not node_id:
            return ErrorResponse(error=NODE_ID_REQUIRED_ERROR)
        if hops <= 0:
            return ErrorResponse(error="hops must be a positive integer")

        runtime = await _get_backend_runtime()
        driver = await runtime.get_driver()
        snapshot = None
        if repo_id and node_id.startswith(("project:", "file:", "symbol:")):
            try:
                snapshot = await _get_metadata_snapshot(repo_id)
            except code_graph_queries.CodeGraphQueryError:
                snapshot = None

        return await provenance_tools.get_node_neighborhood_async(
            service=provenance_tools.NeighborhoodQueryService(
                driver=driver,
                snapshot=snapshot,
            ),
            repo_id=repo_id,
            node_id=node_id,
            hops=hops,
            edge_types=edge_types,
        )
    except Exception as exc:
        logger.error("Error in get_node_neighborhood: %s", exc)
        return ErrorResponse(error=f"Error in get_node_neighborhood: {exc}")


@mcp.tool()
async def get_timeline(
    target_id: Annotated[str, Field(description=TARGET_ID_FIELD_DESCRIPTION)],
    repo_id: Annotated[
        str | None,
        Field(description="Optional repo_id used to scope historical provenance assembly."),
    ] = None,
    limit: Annotated[int, Field(description="Maximum number of historical events to return.")] = 50,
) -> TimelineResponse | ErrorResponse:
    """Requires exact target_id. Return a historical provenance timeline around one graph anchor."""

    try:
        if limit <= 0:
            return ErrorResponse(error="limit must be a positive integer")

        runtime = await _get_backend_runtime()
        driver = await runtime.get_driver()
        return await provenance_tools.get_timeline_async(
            service=provenance_tools.TimelineQueryService(
                driver=driver,
            ),
            repo_id=repo_id,
            target_id=target_id,
            limit=limit,
        )
    except Exception as exc:
        logger.error("Error in get_timeline: %s", exc)
        return ErrorResponse(error=f"Error in get_timeline: {exc}")


@mcp.tool()
async def get_code_context(
    repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
    target: Annotated[
        str,
        Field(
            description=(
                "Code target to resolve. This can be an exact qualified_name, exact node_id, "
                "file path, or a search-like string when target_kind is provided."
            )
        ),
    ],
    target_kind: Annotated[
        CodeContextTargetKind | None,
        Field(description=CODE_CONTEXT_TARGET_KIND_FIELD_DESCRIPTION),
    ] = None,
    include_neighbors: Annotated[
        bool,
        Field(description="Include nearby sibling snippets for the resolved primary symbol."),
    ] = True,
    include_references: Annotated[
        bool,
        Field(description="Include directly related referenced symbols in the response."),
    ] = True,
    max_related_symbols: Annotated[
        int,
        Field(description="Maximum number of related symbols to include in the context slice."),
    ] = 10,
) -> CodeContextResponse | ErrorResponse:
    """Return a deterministic code-context slice for a file, symbol, class, or module."""

    try:
        if max_related_symbols <= 0:
            return ErrorResponse(error="max_related_symbols must be a positive integer")
        normalized_target_kind = _normalize_supported_value(
            target_kind,
            parameter_name="target_kind",
            supported_values=CODE_CONTEXT_TARGET_KIND_VALUES,
        )

        snapshot = await _get_metadata_snapshot(repo_id)
        return await asyncio.to_thread(
            provenance_tools.get_code_context,
            snapshot=snapshot,
            repo_id=repo_id,
            target=target,
            target_kind=normalized_target_kind,
            include_neighbors=include_neighbors,
            include_references=include_references,
            max_related_symbols=max_related_symbols,
        )
    except ParameterValidationError as exc:
        return ErrorResponse(error=str(exc))
    except code_graph_queries.CodeGraphQueryError as exc:
        return ErrorResponse(error=str(exc))
    except Exception as exc:
        logger.error("Error in get_code_context: %s", exc)
        return ErrorResponse(error=f"Error in get_code_context: {exc}")


@mcp.tool()
async def record_agent_observation(
    repo_id: Annotated[str, Field(description=REPO_ID_FIELD_DESCRIPTION)],
    observation_text: Annotated[
        str,
        Field(description="Free-form agent note or trace text to persist."),
    ],
    observation_kind: Annotated[
        str,
        Field(description=OBSERVATION_KIND_FIELD_DESCRIPTION),
    ] = "agent_note",
    title: Annotated[
        str | None,
        Field(description="Optional short title for the observation."),
    ] = None,
    task_id: Annotated[
        str | None,
        Field(description="Optional task or workflow identifier for caller-side correlation."),
    ] = None,
    metadata: Annotated[
        dict[str, Any] | None,
        Field(description=METADATA_FIELD_DESCRIPTION),
    ] = None,
) -> RecordAgentObservationResponse | ErrorResponse:
    """Persist an agent-originated observation through the provenance pipeline."""

    try:
        if not observation_text.strip():
            return ErrorResponse(error="observation_text must not be empty")

        runtime = _require_backend_runtime()
        request = ObservationWriteRequest(
            repo_id=repo_id,
            group_id=config.namespace.group_id,
            observation_text=observation_text,
            observation_kind=observation_kind,
            title=title,
            source_kind="agent",
            source_id=None,
            context_kind=None,
            context_id=task_id,
            observed_at=_current_timestamp(),
            task_id=task_id,
            metadata=metadata or {},
        )
        return await _persist_agent_observation_request(request)
    except Exception as exc:
        logger.error("Error in record_agent_observation: %s", exc)
        return ErrorResponse(error=f"Error in record_agent_observation: {exc}")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request) -> JSONResponse:
    """Health check endpoint for Docker and load balancers."""

    return JSONResponse({"status": "healthy", "service": "context-graph-mcp"})


async def initialize_server() -> ServerConfig:
    """Parse CLI arguments and initialize the Context Graph MCP server."""

    global config, backend_runtime, metadata_snapshot_service

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

    args = parser.parse_args()

    if args.config:
        os.environ["CONFIG_PATH"] = str(args.config)

    config = MCPServerConfig()
    config.apply_cli_overrides(args)

    logger.info("Using configuration:")
    logger.info("  - LLM: %s / %s", config.llm.provider, config.llm.model)
    logger.info("  - Embedder: %s / %s", config.embedder.provider, config.embedder.model)
    logger.info("  - Database: %s", config.database.provider)
    logger.info("  - Namespace Group ID: %s", config.namespace.group_id)
    logger.info("  - Transport: %s", config.server.transport)
    logger.info("  - Runtime: context_graph storage backend + metadata snapshots")

    backend_runtime = MCPBackendRuntime(config, semaphore_limit=SEMAPHORE_LIMIT)
    metadata_snapshot_service = MetadataSnapshotService(config)

    if config.server.host:
        mcp.settings.host = config.server.host
        if config.server.host == "0.0.0.0":
            mcp.settings.transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            )
    if config.server.port:
        mcp.settings.port = config.server.port

    return config.server


async def run_mcp_server() -> None:
    """Run the MCP server in the current event loop."""

    mcp_config = await initialize_server()
    logger.info("Starting MCP server with transport: %s", mcp_config.transport)
    if mcp_config.transport == "stdio":
        await mcp.run_stdio_async()
    elif mcp_config.transport == "sse":
        logger.info(
            "Running MCP server with SSE transport on %s:%s",
            mcp.settings.host,
            mcp.settings.port,
        )
        logger.info("Access the server at: http://%s:%s/sse", mcp.settings.host, mcp.settings.port)
        await mcp.run_sse_async()
    elif mcp_config.transport == "http":
        display_host = "localhost" if mcp.settings.host == "0.0.0.0" else mcp.settings.host
        logger.info(
            "Running MCP server with streamable HTTP transport on %s:%s",
            mcp.settings.host,
            mcp.settings.port,
        )
        logger.info("=" * 60)
        logger.info("MCP Server Access Information:")
        logger.info("  Base URL: http://%s:%s/", display_host, mcp.settings.port)
        logger.info("  MCP Endpoint: http://%s:%s/mcp/", display_host, mcp.settings.port)
        logger.info("  Transport: HTTP (streamable)")
        if os.environ.get("BROWSER", "1") == "1":
            logger.info("  FalkorDB Browser UI: http://%s:3000/", display_host)
        logger.info("=" * 60)
        logger.info("For MCP clients, connect to the /mcp/ endpoint above")
        configure_uvicorn_logging()
        await mcp.run_streamable_http_async()
    else:
        raise ValueError(
            f'Unsupported transport: {mcp_config.transport}. Use "sse", "stdio", or "http"'
        )


def main() -> None:
    """Main function to run the Context Graph MCP server."""

    try:
        asyncio.run(run_mcp_server())
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    except Exception as exc:
        logger.error("Error initializing Context Graph MCP server: %s", exc)
        raise


if __name__ == "__main__":
    main()
