"""LLM-facing service boundaries for future provenance extraction stages."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import re
import threading
from typing import Any, Mapping, Sequence

from .canonicalization import canonicalize_claim_text
from .models import (
    ClaimCanonicalizationResult,
    ClaimCanonicalizationGroup,
    ClaimDedupCandidate,
    ClaimEvidenceExtractionResponse,
    ClaimWithEvidenceCandidate,
    ClaimCandidate,
    ClaimDerivationResponse,
    ClaimRelationCandidate,
    ClaimRelationResponse,
    EvidenceDedupCandidate,
    EvidenceDedupGroup,
    EvidenceDedupResult,
    EvidenceCandidate,
    FactConflictResolutionResponse,
    Observation,
    ObservationKind,
    ResearchArtifactCandidate,
    ResearchArtifactSynthesisResponse,
)
from .prompts import (
    ProvenancePromptRequest,
    extract_claims_and_evidence,
    normalize_claims,
    relate_claims,
    resolve_fact_conflicts,
    synthesize_research_artifact,
)


class ClaimEvidenceExtractionService:
    """Validate one observation and execute a joint claim+evidence extraction request."""

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
            metadata = extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_COMMIT_MESSAGE
            messages = extract_claims_and_evidence.from_commit_message(context)
        elif observation.observation_kind == ObservationKind.GITHUB_PR_BODY:
            metadata = extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_PR_BODY
            messages = extract_claims_and_evidence.from_github_pr_body(context)
        elif observation.observation_kind == ObservationKind.GITHUB_ISSUE_BODY:
            metadata = extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_ISSUE_BODY
            messages = extract_claims_and_evidence.from_github_issue_body(context)
        elif observation.observation_kind == ObservationKind.GITHUB_PR_REVIEW:
            metadata = extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_PR_REVIEW
            messages = extract_claims_and_evidence.from_github_pr_review(context)
        elif observation.observation_kind == ObservationKind.GITHUB_PR_REVIEW_COMMENT:
            metadata = extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_PR_REVIEW_COMMENT
            messages = extract_claims_and_evidence.from_github_pr_review_comment(context)
        elif observation.observation_kind == ObservationKind.GITHUB_ISSUE_COMMENT:
            metadata = extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_ISSUE_COMMENT
            messages = extract_claims_and_evidence.from_github_issue_comment(context)
        else:
            metadata = extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GENERIC_OBSERVATION
            messages = extract_claims_and_evidence.from_generic_observation(context)
        return ProvenancePromptRequest(
            metadata=metadata,
            messages=tuple(messages),
            response_model=ClaimEvidenceExtractionResponse,
        )

    def extract(
        self,
        observation: Observation,
        *,
        code_context: Mapping[str, Any] | None = None,
        source_context: Mapping[str, Any] | None = None,
    ) -> ClaimEvidenceExtractionResponse:
        if self.llm_client is None:
            raise RuntimeError("claim+evidence extraction requires a configured llm_client")
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


EvidenceExtractionService = ClaimEvidenceExtractionService


class EvidenceDedupService:
    """Deterministically pregroup grounded evidence for a future dedup pass."""

    def pregroup(
        self,
        candidates: Sequence[EvidenceDedupCandidate],
    ) -> EvidenceDedupResult:
        grouped: dict[str, list[EvidenceDedupCandidate]] = {}
        for candidate in candidates:
            key = self._canonical_key(candidate)
            grouped.setdefault(key, []).append(candidate)
        return EvidenceDedupResult(
            groups=[
                EvidenceDedupGroup(canonical_key=key, candidates=list(grouped[key]))
                for key in sorted(grouped)
            ]
        )

    def _canonical_key(self, candidate: EvidenceDedupCandidate) -> str:
        return "|".join(
            [
                candidate.observation_id,
                candidate.evidence_kind,
                candidate.support_kind,
                _normalize_text_key(candidate.quote_text),
                str(candidate.span_start),
                str(candidate.span_end),
                ",".join(sorted(candidate.about_record_ids)),
                candidate.claim_kind or "",
                canonicalize_claim_text(candidate.claim_text or ""),
            ]
        )


class ClaimCanonicalizationService:
    """Deterministically pregroup validated claims for a future canonical layer."""

    def pregroup(
        self,
        candidates: Sequence[ClaimDedupCandidate],
    ) -> ClaimCanonicalizationResult:
        grouped: dict[str, list[ClaimDedupCandidate]] = {}
        for candidate in candidates:
            key = self._canonical_key(candidate)
            grouped.setdefault(key, []).append(candidate)
        return ClaimCanonicalizationResult(
            groups=[
                ClaimCanonicalizationGroup(canonical_key=key, candidates=list(grouped[key]))
                for key in sorted(grouped)
            ]
        )

    def _canonical_key(self, candidate: ClaimDedupCandidate) -> str:
        return "|".join(
            [
                candidate.claim_kind,
                canonicalize_claim_text(candidate.claim_text),
                ",".join(sorted(candidate.about_record_ids)),
            ]
        )


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


class FactConflictResolutionService:
    """Compare one new fact against current fact candidates."""

    def __init__(self, *, llm_client: Any | None = None) -> None:
        self.llm_client = llm_client

    def build_request(
        self,
        *,
        new_fact: Mapping[str, Any],
        current_facts: Sequence[Mapping[str, Any]],
    ) -> ProvenancePromptRequest:
        claim_text = str(new_fact.get("claim_text", "")).strip()
        if not claim_text:
            raise ValueError("new fact claim_text must not be empty")
        if not current_facts:
            raise ValueError("current_facts must not be empty")
        context = {
            "new_fact": dict(new_fact),
            "current_facts": [dict(candidate) for candidate in current_facts],
        }
        return ProvenancePromptRequest(
            metadata=resolve_fact_conflicts.RESOLVE_FACT_CONFLICTS_COMPARE,
            messages=tuple(resolve_fact_conflicts.compare_against_current(context)),
            response_model=FactConflictResolutionResponse,
        )

    def resolve(
        self,
        *,
        repo_id: str,
        new_fact: Mapping[str, Any],
        current_facts: Sequence[Mapping[str, Any]],
    ) -> FactConflictResolutionResponse:
        if self.llm_client is None:
            raise RuntimeError("fact conflict resolution requires a configured llm_client")
        request = self.build_request(new_fact=new_fact, current_facts=current_facts)
        messages = [message.model_copy(deep=True) for message in request.messages]
        response = _run_coroutine_sync(
            self.llm_client.generate_response(
                messages,
                response_model=request.response_model,
                group_id=repo_id,
                prompt_name=request.prompt_name,
            )
        )
        return request.response_model.model_validate(response)


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


_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_text_key(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value.strip()).lower()
