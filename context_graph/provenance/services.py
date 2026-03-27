"""LLM-facing service boundaries for future provenance extraction stages."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import threading
from typing import Any, Mapping, Sequence

from .models import (
    ClaimCandidate,
    ClaimDerivationResponse,
    ClaimRelationCandidate,
    ClaimRelationResponse,
    EvidenceCandidate,
    EvidenceExtractionResponse,
    Observation,
    ObservationKind,
    ResearchArtifactCandidate,
    ResearchArtifactSynthesisResponse,
)
from .prompts import (
    ProvenancePromptRequest,
    extract_evidence,
    normalize_claims,
    relate_claims,
    synthesize_research_artifact,
)


class EvidenceExtractionService:
    """Validate one observation and execute a typed evidence extraction request."""

    def __init__(self, *, llm_client: Any | None = None) -> None:
        self.llm_client = llm_client

    def build_request(
        self,
        observation: Observation,
        *,
        code_context: Mapping[str, Any] | None = None,
        source_context: Mapping[str, Any] | None = None,
    ) -> ProvenancePromptRequest:
        if not observation.text.strip():
            raise ValueError("observation text must not be empty")
        context = {
            "observation": _observation_payload(observation),
            "code_context": dict(code_context or {}),
            "source_context": dict(source_context or {}),
        }
        if observation.observation_kind == ObservationKind.GIT_COMMIT_MESSAGE:
            metadata = extract_evidence.EXTRACT_EVIDENCE_FROM_COMMIT_MESSAGE
            messages = extract_evidence.from_commit_message(context)
        elif observation.observation_kind == ObservationKind.GITHUB_PR_BODY:
            metadata = extract_evidence.EXTRACT_EVIDENCE_FROM_GITHUB_PR_BODY
            messages = extract_evidence.from_github_pr_body(context)
        elif observation.observation_kind == ObservationKind.GITHUB_ISSUE_BODY:
            metadata = extract_evidence.EXTRACT_EVIDENCE_FROM_GITHUB_ISSUE_BODY
            messages = extract_evidence.from_github_issue_body(context)
        elif observation.observation_kind == ObservationKind.GITHUB_PR_REVIEW:
            metadata = extract_evidence.EXTRACT_EVIDENCE_FROM_GITHUB_PR_REVIEW
            messages = extract_evidence.from_github_pr_review(context)
        elif observation.observation_kind == ObservationKind.GITHUB_PR_REVIEW_COMMENT:
            metadata = extract_evidence.EXTRACT_EVIDENCE_FROM_GITHUB_PR_REVIEW_COMMENT
            messages = extract_evidence.from_github_pr_review_comment(context)
        elif observation.observation_kind == ObservationKind.GITHUB_ISSUE_COMMENT:
            metadata = extract_evidence.EXTRACT_EVIDENCE_FROM_GITHUB_ISSUE_COMMENT
            messages = extract_evidence.from_github_issue_comment(context)
        else:
            metadata = extract_evidence.EXTRACT_EVIDENCE_FROM_GENERIC_OBSERVATION
            messages = extract_evidence.from_generic_observation(context)
        return ProvenancePromptRequest(
            metadata=metadata,
            messages=tuple(messages),
            response_model=EvidenceExtractionResponse,
        )

    def extract(
        self,
        observation: Observation,
        *,
        code_context: Mapping[str, Any] | None = None,
        source_context: Mapping[str, Any] | None = None,
    ) -> EvidenceExtractionResponse:
        if self.llm_client is None:
            raise RuntimeError("evidence extraction requires a configured llm_client")
        request = self.build_request(
            observation,
            code_context=code_context,
            source_context=source_context,
        )
        messages = [message.model_copy(deep=True) for message in request.messages]
        response = _run_coroutine_sync(
            self.llm_client.generate_response(
                messages,
                response_model=request.response_model,
                group_id=observation.repo_id,
                prompt_name=request.prompt_name,
            )
        )
        return request.response_model.model_validate(response)


class ClaimDerivationService:
    """Validate bounded evidence input and build a claim normalization request."""

    def build_request(
        self,
        evidence: Sequence[EvidenceCandidate],
        *,
        topic: Mapping[str, Any] | None = None,
        target_context: Mapping[str, Any] | None = None,
    ) -> ProvenancePromptRequest:
        if not evidence:
            raise ValueError("evidence must not be empty")
        context = {
            "topic": dict(topic or {}),
            "evidence": [candidate.model_dump() for candidate in evidence],
            "target_context": dict(target_context or {}),
        }
        return ProvenancePromptRequest(
            metadata=normalize_claims.NORMALIZE_CLAIMS_FROM_EVIDENCE_SET,
            messages=tuple(normalize_claims.from_evidence_set(context)),
            response_model=ClaimDerivationResponse,
        )


class ClaimRelationService:
    """Build bounded claim-relation comparison requests."""

    def build_request(
        self,
        claims: Sequence[ClaimCandidate],
    ) -> ProvenancePromptRequest:
        if len(claims) < 2:
            raise ValueError("claim comparison requires at least two claims")
        context = {"claims": [candidate.model_dump() for candidate in claims]}
        return ProvenancePromptRequest(
            metadata=relate_claims.RELATE_CLAIMS_COMPARE,
            messages=tuple(relate_claims.compare_claims(context)),
            response_model=ClaimRelationResponse,
        )


class ResearchArtifactSynthesisService:
    """Build bounded research artifact synthesis requests."""

    def build_request(
        self,
        *,
        repo_region: Mapping[str, Any] | None = None,
        code_context: Mapping[str, Any] | None = None,
        evidence: Sequence[EvidenceCandidate] | None = None,
        claims: Sequence[ClaimCandidate | ClaimRelationCandidate | ResearchArtifactCandidate] | None = None,
    ) -> ProvenancePromptRequest:
        if not code_context and not evidence and not claims:
            raise ValueError("research artifact synthesis needs code context, evidence, or claims")
        context = {
            "repo_region": dict(repo_region or {}),
            "code_context": dict(code_context or {}),
            "evidence": [candidate.model_dump() for candidate in (evidence or ())],
            "claims": [candidate.model_dump() for candidate in (claims or ())],
        }
        return ProvenancePromptRequest(
            metadata=synthesize_research_artifact.SYNTHESIZE_RESEARCH_ARTIFACT_FROM_CODE_AND_EVIDENCE,
            messages=tuple(synthesize_research_artifact.from_code_and_evidence(context)),
            response_model=ResearchArtifactSynthesisResponse,
        )


def _observation_payload(observation: Observation) -> dict[str, Any]:
    payload = asdict(observation)
    payload["observation_kind"] = observation.observation_kind.value
    payload["metadata"] = dict(observation.metadata)
    return payload


def _run_coroutine_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive handoff
            error.append(exc)

    thread = threading.Thread(target=_runner, daemon=True, name="context-graph-provenance-llm")
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result.get("value")
