"""MCP tool registration for agent-originated provenance writes."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from ..schemas import ErrorResponse, ObservationWriteRequest, RecordAgentObservationResponse
from ..state import ServerState
from .common import (
    METADATA_FIELD_DESCRIPTION,
    OBSERVATION_KIND_FIELD_DESCRIPTION,
    REPO_ID_FIELD_DESCRIPTION,
    _current_timestamp,
    _get_backend_runtime,
    error_response,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


async def _persist_agent_observation_request(
    state: ServerState,
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
    from context_graph.query.parser import QueryParser

    snapshot_service = state.require_metadata_snapshot_service()
    runtime = await _get_backend_runtime(state)
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
        "status": "implemented",
        "message": "Persisted the observation and indexed it for provenance retrieval.",
        "capability": "context_graph.backend.ingestion.observation_write",
        "repo_id": request.repo_id,
        "observation_id": planned_observation.observation.observation_id,
        "task_id": request.task_id,
        "claim_ids": list(apply_result.result.claim_ids),
        "accepted": True,
        "persisted": True,
    }


async def record_agent_observation(
    state: ServerState,
    repo_id: str,
    observation_text: str,
    observation_kind: str = "agent_note",
    title: str | None = None,
    task_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> RecordAgentObservationResponse | ErrorResponse:
    """Persist an agent-originated observation through the provenance pipeline."""

    try:
        if not observation_text.strip():
            return error_response("observation_text must not be empty")

        state.require_backend_runtime()
        request = ObservationWriteRequest(
            repo_id=repo_id,
            group_id=state.require_config().namespace.group_id,
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
        return await _persist_agent_observation_request(state, request)
    except Exception as exc:
        logger.error("Error in record_agent_observation: %s", exc)
        return error_response(f"Error in record_agent_observation: {exc}")


def register_observation_tools(mcp: "FastMCP", state: ServerState) -> None:
    """Register observation-writing tools on one FastMCP instance."""

    @mcp.tool(name="record_agent_observation")
    async def record_agent_observation_tool(
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

        return await record_agent_observation(
            state,
            repo_id,
            observation_text,
            observation_kind=observation_kind,
            title=title,
            task_id=task_id,
            metadata=metadata,
        )
