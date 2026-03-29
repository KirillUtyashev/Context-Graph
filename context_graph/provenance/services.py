"""LLM-facing service boundaries for future provenance extraction stages."""

from __future__ import annotations

import asyncio
import re
import threading
from typing import Any, Mapping, Sequence

from context_graph.memory.prompts.models import Message
from pydantic import BaseModel, Field

from .canonicalization import canonicalize_claim_text
from .debug import emit_provenance_debug
from .models import (
    ClaimCanonicalizationResult,
    ClaimCanonicalizationGroup,
    ClaimDedupCandidate,
    ClaimEvidenceExtractionResponse,
    ClaimResolutionCandidate,
    ClaimResolutionResponse,
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
    ResearchArtifactCandidate,
    ResearchArtifactSynthesisResponse,
)
from .prompt_context import build_prompt_context
from .prompts import (
    ProvenancePromptRequest,
    extract_claims_and_evidence,
    normalize_claims,
    relate_claims,
    resolve_fact_conflicts,
    synthesize_research_artifact,
)
from .schema_registry import DEFAULT_PROVENANCE_SCHEMA_REGISTRY


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
        context = build_prompt_context(
            observation,
            code_context=code_context,
            source_context=source_context,
        )
        policy = DEFAULT_PROVENANCE_SCHEMA_REGISTRY.observation_policy(
            observation.observation_kind,
            source_kind=observation.source_kind,
        )
        prompt_profile = policy.prompt_profile if policy is not None else "generic_observation"
        metadata, builder = _claim_evidence_prompt_builder(prompt_profile)
        messages = builder(context)
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
        return _execute_provenance_request(
            llm_client=self.llm_client,
            messages=request.messages,
            response_model=request.response_model,
            group_id=observation.repo_id,
            prompt_name=request.prompt_name,
            debug_metadata={
                "observation_id": observation.observation_id,
                "observation_kind": observation.observation_kind,
                "repo_id": observation.repo_id,
                "source_kind": observation.source_kind,
                "source_id": observation.source_id,
            },
        )


EvidenceExtractionService = ClaimEvidenceExtractionService


def _claim_evidence_prompt_builder(prompt_profile: str | None):
    builders = {
        "commit_message": (
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_COMMIT_MESSAGE,
            extract_claims_and_evidence.from_commit_message,
        ),
        "github_pr_body": (
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_PR_BODY,
            extract_claims_and_evidence.from_github_pr_body,
        ),
        "github_issue_body": (
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_ISSUE_BODY,
            extract_claims_and_evidence.from_github_issue_body,
        ),
        "github_pr_review": (
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_PR_REVIEW,
            extract_claims_and_evidence.from_github_pr_review,
        ),
        "github_pr_review_comment": (
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_PR_REVIEW_COMMENT,
            extract_claims_and_evidence.from_github_pr_review_comment,
        ),
        "github_issue_comment": (
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_ISSUE_COMMENT,
            extract_claims_and_evidence.from_github_issue_comment,
        ),
        "generic_observation": (
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GENERIC_OBSERVATION,
            extract_claims_and_evidence.from_generic_observation,
        ),
    }
    return builders.get(
        prompt_profile or "generic_observation",
        (
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GENERIC_OBSERVATION,
            extract_claims_and_evidence.from_generic_observation,
        ),
    )


class ClaimProfileKeyGenerationResponse(BaseModel):
    retrieval_keys: list[str] = Field(default_factory=list)


class ClaimProfileKeyGenerationService:
    """Generate a few grounded semantic retrieval phrases for persisted claims."""

    def __init__(self, *, llm_client: Any | None = None) -> None:
        self.llm_client = llm_client

    def generate(
        self,
        *,
        repo_id: str,
        claim_text: str,
        claim_subkind: str,
        support_texts: Sequence[str] = (),
    ) -> tuple[str, ...]:
        normalized_claim_text = _normalize_profile_key_text(claim_text)
        normalized_support_texts = tuple(
            text
            for text in (
                _normalize_profile_key_text(text)
                for text in support_texts[:3]
            )
            if text
        )
        if not normalized_claim_text or self.llm_client is None:
            return ()
        try:
            response = _execute_provenance_request(
                llm_client=self.llm_client,
                messages=_claim_profile_key_prompt(
                    claim_text=normalized_claim_text,
                    claim_subkind=claim_subkind,
                    support_texts=normalized_support_texts,
                ),
                response_model=ClaimProfileKeyGenerationResponse,
                group_id=repo_id,
                prompt_name="context_graph.provenance.generate_claim_profile_keys",
                debug_metadata={
                    "repo_id": repo_id,
                    "claim_text": normalized_claim_text,
                    "claim_subkind": claim_subkind,
                    "support_texts": list(normalized_support_texts),
                },
            )
        except Exception:
            return ()
        return _filter_generated_claim_profile_keys(
            response.retrieval_keys,
            claim_text=normalized_claim_text,
            limit=3,
        )


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


class ClaimResolutionService:
    """Resolve one new claim against bounded candidate claims."""

    def __init__(self, *, llm_client: Any | None = None) -> None:
        self.llm_client = llm_client

    def build_request(
        self,
        *,
        new_claim: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
    ) -> ProvenancePromptRequest:
        claim_text = str(new_claim.get("claim_text", "")).strip()
        if not claim_text:
            raise ValueError("new_claim claim_text must not be empty")
        if not candidates:
            raise ValueError("candidates must not be empty")
        context = {
            "new_claim": dict(new_claim),
            "candidates": [dict(candidate) for candidate in candidates],
        }
        return ProvenancePromptRequest(
            metadata=relate_claims.RESOLVE_NEW_CLAIM_AGAINST_CANDIDATES,
            messages=tuple(relate_claims.resolve_new_claim_against_candidates(context)),
            response_model=ClaimResolutionResponse,
        )

    def resolve(
        self,
        *,
        repo_id: str,
        new_claim: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
    ) -> ClaimResolutionResponse:
        if self.llm_client is None:
            raise RuntimeError("claim resolution requires a configured llm_client")
        request = self.build_request(new_claim=new_claim, candidates=candidates)
        return _execute_provenance_request(
            llm_client=self.llm_client,
            messages=request.messages,
            response_model=request.response_model,
            group_id=repo_id,
            prompt_name=request.prompt_name,
            debug_metadata={
                "repo_id": repo_id,
                "new_claim": dict(new_claim),
                "candidate_count": len(candidates),
            },
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
        return _execute_provenance_request(
            llm_client=self.llm_client,
            messages=request.messages,
            response_model=request.response_model,
            group_id=repo_id,
            prompt_name=request.prompt_name,
            debug_metadata={
                "repo_id": repo_id,
                "new_fact": dict(new_fact),
                "current_fact_count": len(current_facts),
            },
        )


class ResearchArtifactSynthesisService:
    """Build bounded research artifact synthesis requests."""

    def __init__(self, *, llm_client: Any | None = None) -> None:
        self.llm_client = llm_client

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

    def synthesize(
        self,
        *,
        repo_id: str,
        repo_region: Mapping[str, Any] | None = None,
        code_context: Mapping[str, Any] | None = None,
        evidence: Sequence[EvidenceCandidate] | None = None,
        claims: Sequence[ClaimCandidate | ClaimRelationCandidate | ResearchArtifactCandidate] | None = None,
    ) -> ResearchArtifactSynthesisResponse:
        if self.llm_client is None:
            raise RuntimeError("research artifact synthesis requires a configured llm_client")
        request = self.build_request(
            repo_region=repo_region,
            code_context=code_context,
            evidence=evidence,
            claims=claims,
        )
        return _execute_provenance_request(
            llm_client=self.llm_client,
            messages=request.messages,
            response_model=request.response_model,
            group_id=repo_id,
            prompt_name=request.prompt_name,
            debug_metadata={
                "repo_id": repo_id,
                "repo_region": dict(repo_region or {}),
                "evidence_count": len(evidence or ()),
                "claim_count": len(claims or ()),
                "has_code_context": bool(code_context),
            },
        )
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
_PATH_LIKE_RE = re.compile(r"(?:^|[\s`])(?:[\w.-]+/)+[\w./-]+(?:$|[\s`])")
_DOTTED_NAME_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+\b")
_SNAKE_CASE_RE = re.compile(r"\b[A-Za-z_]+_[A-Za-z0-9_]+\b")
_UPPER_IDENTIFIER_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")


def _normalize_text_key(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value.strip()).lower()


def _normalize_profile_key_text(value: object) -> str:
    if value is None:
        return ""
    return _WHITESPACE_RE.sub(" ", str(value).strip())


def _claim_profile_key_prompt(
    *,
    claim_text: str,
    claim_subkind: str,
    support_texts: Sequence[str],
) -> list[Message]:
    support_lines = "\n".join(f"- {text}" for text in support_texts) or "- (none)"
    return [
        Message(
            role="system",
            content=(
                "You generate semantic retrieval phrases for one persisted software-provenance claim.\n"
                "Return only short noun phrases or compact search phrases.\n"
                "The phrases must stay grounded in the provided claim text and support text.\n"
                "Do not output code anchors, file paths, dotted names, SHAs, issue numbers, tags, or explanations.\n"
                "Do not invent causes, architecture changes, or rationale unless directly stated.\n"
                "Return JSON with exactly one key: \"retrieval_keys\"."
            ),
        ),
        Message(
            role="user",
            content=(
                f"Claim subkind: {claim_subkind}\n"
                f"Claim text: {claim_text}\n"
                "Support text excerpts:\n"
                f"{support_lines}\n\n"
                "Produce 1 to 3 grounded semantic retrieval phrases."
            ),
        ),
    ]


def _filter_generated_claim_profile_keys(
    values: Sequence[str],
    *,
    claim_text: str,
    limit: int,
) -> tuple[str, ...]:
    filtered: list[str] = []
    seen: set[str] = set()
    normalized_claim_text = _normalize_profile_key_text(claim_text)
    for value in values:
        normalized = _normalize_profile_key_text(value)
        if not normalized:
            continue
        if normalized.casefold() == normalized_claim_text.casefold():
            continue
        if _looks_like_code_anchor_phrase(normalized):
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        filtered.append(normalized)
        if len(filtered) >= limit:
            break
    return tuple(filtered)


def _looks_like_code_anchor_phrase(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    return any(
        pattern.search(stripped) is not None
        for pattern in (
            _PATH_LIKE_RE,
            _DOTTED_NAME_RE,
            _SNAKE_CASE_RE,
            _UPPER_IDENTIFIER_RE,
        )
    )


def _execute_provenance_request(
    *,
    llm_client: Any,
    messages: Sequence[Message],
    response_model: type[Any],
    group_id: str | None,
    prompt_name: str,
    debug_metadata: Mapping[str, Any],
) -> Any:
    copied_messages = [message.model_copy(deep=True) for message in messages]
    emit_provenance_debug(
        "llm_request",
        {
            "prompt_name": prompt_name,
            "group_id": group_id,
            "response_model": response_model.__name__,
            "messages": [message.model_dump() for message in copied_messages],
            **dict(debug_metadata),
        },
    )
    try:
        response = _run_coroutine_sync(
            llm_client.generate_response(
                copied_messages,
                response_model=response_model,
                group_id=group_id,
                prompt_name=prompt_name,
            )
        )
        emit_provenance_debug(
            "llm_response",
            {
                "prompt_name": prompt_name,
                "group_id": group_id,
                "response": response,
                **dict(debug_metadata),
            },
        )
        return response_model.model_validate(response)
    except Exception as exc:
        emit_provenance_debug(
            "llm_error",
            {
                "prompt_name": prompt_name,
                "group_id": group_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
                **dict(debug_metadata),
            },
        )
        raise
