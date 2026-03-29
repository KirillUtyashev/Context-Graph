"""Conflict resolution and invalidation for provisional evidence-backed facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import inspect
import json
from typing import Any

from context_graph.models import GraphSnapshot
from context_graph.profiles import ProfileRecord
from context_graph.storage.backend import (
    BackendDriver,
    MutationPlan,
    merge_mutation_plans,
    run_backend_coroutine_sync,
)

from .canonicalization import canonical_claim_key
from .claim_planner import ClaimMutationPlanner, ClaimPlanningSession
from .evidence_planner import EvidenceFactState, EvidenceMutationPlanner
from .models import EvidenceRecord, Observation, PersistedEvidenceFact, PersistedEvidenceRecord
from .services import (
    ClaimProfileKeyGenerationService,
    ClaimResolutionService,
    FactConflictResolutionService,
)
from .validation import ClaimValidationResult

_CURRENT_EVIDENCE_QUERY = """
MATCH (e:BackendRecord)-[:ABOUT]->(matched_about:BackendRecord)
WHERE e.record_type = "evidence_record"
  AND e.repo_id = $repo_id
  AND matched_about.record_id IN $about_record_ids
  AND coalesce(e.is_current, true) = true
WITH DISTINCT e
OPTIONAL MATCH (e)-[:ABOUT]->(about:BackendRecord)
OPTIONAL MATCH (e)-[:DERIVED_FROM]->(observation:BackendRecord)
RETURN
    e.record_id AS evidence_id,
    properties(e) AS evidence_properties,
    collect(DISTINCT about.record_id) AS about_record_ids,
    collect(DISTINCT observation.record_id) AS observation_record_ids,
    collect(DISTINCT properties(observation)) AS observation_properties
"""


@dataclass(frozen=True, slots=True)
class FactInvalidationPlanResult:
    """One mutation plan plus aggregate invalidation counts."""

    plan: MutationPlan
    invalidated_count: int = 0
    profiles: tuple[ProfileRecord, ...] = ()


class CurrentEvidenceReader:
    """Load current evidence records that may be invalidated by a new fact."""

    def __init__(self, *, backend_driver: BackendDriver | None) -> None:
        self.backend_driver = backend_driver

    def load_current_facts(
        self,
        *,
        repo_id: str,
        claim_kind: str,
        about_record_ids: Sequence[str],
        excluded_fact_keys: Sequence[str] = (),
        limit: int = 10,
    ) -> tuple[PersistedEvidenceFact, ...]:
        ordered_about_record_ids = tuple(dict.fromkeys(about_record_ids))
        if self.backend_driver is None or not ordered_about_record_ids:
            return ()
        about_record_id_set = set(ordered_about_record_ids)
        query_coro = self.backend_driver.execute_query(
            _CURRENT_EVIDENCE_QUERY,
            repo_id=repo_id,
            about_record_ids=list(ordered_about_record_ids),
        )
        try:
            rows = run_backend_coroutine_sync(query_coro)
        except Exception:
            _close_unstarted_coroutine(query_coro)
            return ()
        _close_unstarted_coroutine(query_coro)

        excluded = set(excluded_fact_keys)
        records = [
            record
            for row in rows
            for record in (_parse_persisted_evidence_record(row),)
            if record is not None
            and record.evidence.claim_kind == claim_kind
            and record.evidence.is_current
            and any(record_id in about_record_id_set for record_id in record.about_record_ids)
            and record.evidence.fact_key not in excluded
        ]
        grouped: dict[str, list[PersistedEvidenceRecord]] = {}
        for record in records:
            fact_key = record.evidence.fact_key
            if not fact_key:
                continue
            grouped.setdefault(fact_key, []).append(record)

        facts = [
            _collapse_fact_group(record_group)
            for record_group in grouped.values()
            if record_group
        ]
        facts.sort(key=_fact_sort_key, reverse=True)
        return tuple(facts[:limit])


class EvidenceInvalidationPlanner:
    """Compatibility wrapper around the claim-centric provenance planner."""

    def __init__(
        self,
        *,
        backend_driver: BackendDriver | None,
        evidence_planner: EvidenceMutationPlanner | None = None,
        claim_mutation_planner: ClaimMutationPlanner | None = None,
        claim_candidate_retriever=None,
        claim_profile_key_generation_service: ClaimProfileKeyGenerationService | None = None,
        claim_resolution_service: ClaimResolutionService | None = None,
        current_evidence_reader: CurrentEvidenceReader | None = None,
        fact_conflict_resolution_service: FactConflictResolutionService | None = None,
    ) -> None:
        self.backend_driver = backend_driver
        self.evidence_planner = evidence_planner or EvidenceMutationPlanner()
        self.current_evidence_reader = current_evidence_reader or CurrentEvidenceReader(
            backend_driver=backend_driver
        )
        self.claim_mutation_planner = claim_mutation_planner or ClaimMutationPlanner(
            backend_driver=backend_driver,
            evidence_planner=self.evidence_planner,
            claim_candidate_retriever=claim_candidate_retriever,
            claim_profile_key_generation_service=claim_profile_key_generation_service,
            claim_resolution_service=claim_resolution_service,
            fact_conflict_resolution_service=fact_conflict_resolution_service,
        )
        self.fact_conflict_resolution_service = fact_conflict_resolution_service

    def plan_validated_claims(
        self,
        *,
        observation: Observation,
        validation: ClaimValidationResult,
        snapshot: GraphSnapshot | None = None,
        session: ClaimPlanningSession | None = None,
    ) -> FactInvalidationPlanResult:
        result = self.claim_mutation_planner.plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=snapshot,
            session=session,
        )
        return FactInvalidationPlanResult(
            plan=result.plan,
            invalidated_count=result.invalidated_count,
            profiles=result.profiles,
        )

    def begin_claim_session(self, *, repo_id: str) -> ClaimPlanningSession:
        return self.claim_mutation_planner.begin_session(repo_id=repo_id)


def _parse_persisted_evidence_record(row: Mapping[str, Any]) -> PersistedEvidenceRecord | None:
    properties = dict(row.get("evidence_properties") or {})
    metadata = _parse_json_object(properties.get("metadata_json"))
    observation_properties = [
        dict(item)
        for item in row.get("observation_properties", ())
        if isinstance(item, Mapping)
    ]
    observation = observation_properties[0] if observation_properties else {}
    observation_ids = tuple(
        str(value)
        for value in row.get("observation_record_ids", ())
        if value is not None
    )
    claim_text = _string_or_none(properties.get("claim_text")) or _string_or_none(metadata.get("claim_text"))
    claim_kind = _string_or_none(properties.get("claim_kind")) or _string_or_none(metadata.get("claim_kind"))
    if not claim_text or not claim_kind:
        return None
    evidence = PersistedEvidenceRecord(
        evidence=_build_evidence_record(
            evidence_id=_string_or_none(properties.get("evidence_id")) or _string_or_none(row.get("evidence_id")) or "",
            repo_id=_string_or_none(properties.get("repo_id")) or "",
            evidence_kind=_string_or_none(properties.get("evidence_kind")) or "",
            quote_text=_string_or_none(properties.get("quote_text")) or "",
            summary_text=_string_or_none(properties.get("summary_text")) or "",
            span_start=_int_or_none(properties.get("span_start")),
            span_end=_int_or_none(properties.get("span_end")),
            support_kind=_string_or_none(properties.get("support_kind")) or "",
            support_source=_string_or_none(properties.get("support_source"))
            or _string_or_none(metadata.get("support_source")),
            fact_key=_string_or_none(properties.get("fact_key")) or canonical_claim_key(
                claim_kind=claim_kind,
                claim_text=claim_text,
            ),
            claim_text=claim_text,
            claim_kind=claim_kind,
            claim_subkind=_string_or_none(properties.get("claim_subkind"))
            or _string_or_none(metadata.get("claim_subkind")),
            assertion_status=_string_or_none(properties.get("assertion_status"))
            or _string_or_none(metadata.get("assertion_status")),
            claim_frame=_string_or_none(properties.get("claim_frame"))
            or _string_or_none(metadata.get("claim_frame")),
            claim_confidence=_string_or_none(properties.get("claim_confidence"))
            or _string_or_none(metadata.get("claim_confidence")),
            claim_derivation_kind=_string_or_none(properties.get("claim_derivation_kind"))
            or _string_or_none(metadata.get("claim_derivation_kind")),
            observation_id=_string_or_none(properties.get("observation_id"))
            or (observation_ids[0] if observation_ids else None)
            or _string_or_none(observation.get("observation_id"))
            or _string_or_none(observation.get("record_id")),
            observed_at=_string_or_none(properties.get("observed_at"))
            or _string_or_none(observation.get("observed_at")),
            is_current=_coerce_bool(properties.get("is_current"), default=True),
            invalidated_at=_string_or_none(properties.get("invalidated_at")),
            invalidated_by_fact_key=_string_or_none(properties.get("invalidated_by_fact_key")),
            invalidated_by_observation_id=_string_or_none(properties.get("invalidated_by_observation_id")),
            metadata=metadata,
        ),
        about_record_ids=tuple(
            dict.fromkeys(
                str(record_id)
                for record_id in row.get("about_record_ids", ())
                if record_id is not None
            )
        ),
    )
    return evidence


def _build_evidence_record(
    *,
    evidence_id: str,
    repo_id: str,
    evidence_kind: str,
    quote_text: str,
    summary_text: str,
    span_start: int | None,
    span_end: int | None,
    support_kind: str,
    support_source: str | None,
    fact_key: str | None,
    claim_text: str | None,
    claim_kind: str | None,
    claim_subkind: str | None,
    assertion_status: str | None,
    claim_frame: str | None,
    claim_confidence: str | None,
    claim_derivation_kind: str | None,
    observation_id: str | None,
    observed_at: str | None,
    is_current: bool,
    invalidated_at: str | None,
    invalidated_by_fact_key: str | None,
    invalidated_by_observation_id: str | None,
    metadata: Mapping[str, Any],
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        repo_id=repo_id,
        evidence_kind=evidence_kind,
        quote_text=quote_text,
        summary_text=summary_text,
        span_start=span_start,
        span_end=span_end,
        support_kind=support_kind,
        support_source=support_source,
        fact_key=fact_key,
        claim_text=claim_text,
        claim_kind=claim_kind,
        claim_subkind=claim_subkind,
        assertion_status=assertion_status,
        claim_frame=claim_frame,
        claim_confidence=claim_confidence,
        claim_derivation_kind=claim_derivation_kind,
        observation_id=observation_id,
        observed_at=observed_at,
        is_current=is_current,
        invalidated_at=invalidated_at,
        invalidated_by_fact_key=invalidated_by_fact_key,
        invalidated_by_observation_id=invalidated_by_observation_id,
        metadata=metadata,
    )


def _collapse_fact_group(records: Sequence[PersistedEvidenceRecord]) -> PersistedEvidenceFact:
    ordered_records = sorted(records, key=_record_sort_key, reverse=True)
    representative = ordered_records[0]
    return PersistedEvidenceFact(
        fact_key=representative.evidence.fact_key or "",
        claim_text=representative.evidence.claim_text or "",
        claim_kind=representative.evidence.claim_kind or "",
        observation_id=representative.evidence.observation_id,
        observed_at=representative.evidence.observed_at,
        about_record_ids=tuple(
            dict.fromkeys(
                record_id
                for record in ordered_records
                for record_id in record.about_record_ids
            )
        ),
        evidence_records=tuple(ordered_records),
    )


def _record_sort_key(record: PersistedEvidenceRecord) -> tuple[bool, datetime, str]:
    observed_at = _parse_datetime(record.evidence.observed_at)
    return (
        observed_at is not None,
        observed_at or datetime.min,
        record.evidence.evidence_id,
    )


def _fact_sort_key(fact: PersistedEvidenceFact) -> tuple[bool, datetime, str]:
    observed_at = _parse_datetime(fact.observed_at)
    return (
        observed_at is not None,
        observed_at or datetime.min,
        fact.fact_key,
    )


def _newest_fact(facts: Sequence[PersistedEvidenceFact]) -> PersistedEvidenceFact | None:
    if not facts:
        return None
    return sorted(facts, key=_fact_sort_key, reverse=True)[0]


def _is_strictly_newer(existing_observed_at: str | None, new_observed_at: str | None) -> bool:
    existing = _parse_datetime(existing_observed_at)
    new = _parse_datetime(new_observed_at)
    if existing is None or new is None:
        return False
    return existing > new


def _evidence_ids(fact: PersistedEvidenceFact) -> tuple[str, ...]:
    return tuple(record.evidence.evidence_id for record in fact.evidence_records)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _parse_json_object(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _coerce_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return bool(value)


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _close_unstarted_coroutine(value: object) -> None:
    if inspect.iscoroutine(value) and inspect.getcoroutinestate(value) == inspect.CORO_CREATED:
        value.close()
