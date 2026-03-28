"""Conflict resolution and invalidation for provisional evidence-backed facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Mapping, Sequence

from context_graph.models import GraphSnapshot
from context_graph.storage.backend import BackendDriver, MutationPlan, merge_mutation_plans

from .canonicalization import canonical_claim_key
from .evidence_planner import EvidenceFactState, EvidenceMutationPlanner
from .models import EvidenceRecord, Observation, PersistedEvidenceFact, PersistedEvidenceRecord
from .services import FactConflictResolutionService, _run_coroutine_sync
from .validation import ClaimValidationResult, ValidatedClaimBundle

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
        if self.backend_driver is None or not about_record_ids:
            return ()
        try:
            rows = _run_coroutine_sync(
                self.backend_driver.execute_query(
                    _CURRENT_EVIDENCE_QUERY,
                    repo_id=repo_id,
                    about_record_ids=list(dict.fromkeys(about_record_ids)),
                )
            )
        except Exception:
            return ()

        excluded = set(excluded_fact_keys)
        records = [
            record
            for row in rows
            for record in (_parse_persisted_evidence_record(row),)
            if record is not None
            and record.evidence.claim_kind == claim_kind
            and record.evidence.is_current
            and set(record.about_record_ids).intersection(about_record_ids)
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
    """Resolve contradictions against persisted current evidence before insertion."""

    def __init__(
        self,
        *,
        backend_driver: BackendDriver | None,
        evidence_planner: EvidenceMutationPlanner | None = None,
        current_evidence_reader: CurrentEvidenceReader | None = None,
        fact_conflict_resolution_service: FactConflictResolutionService | None = None,
    ) -> None:
        self.backend_driver = backend_driver
        self.evidence_planner = evidence_planner or EvidenceMutationPlanner()
        self.current_evidence_reader = current_evidence_reader or CurrentEvidenceReader(
            backend_driver=backend_driver
        )
        self.fact_conflict_resolution_service = fact_conflict_resolution_service

    def plan_validated_claims(
        self,
        *,
        observation: Observation,
        validation: ClaimValidationResult,
        snapshot: GraphSnapshot | None = None,
    ) -> FactInvalidationPlanResult:
        plans: list[MutationPlan] = []
        invalidated_count = 0
        for bundle in validation.validated_claims:
            result = self.plan_validated_claim_bundle(
                observation=observation,
                bundle=bundle,
                snapshot=snapshot,
            )
            plans.append(result.plan)
            invalidated_count += result.invalidated_count
        return FactInvalidationPlanResult(
            plan=merge_mutation_plans(*plans),
            invalidated_count=invalidated_count,
        )

    def plan_validated_claim_bundle(
        self,
        *,
        observation: Observation,
        bundle: ValidatedClaimBundle,
        snapshot: GraphSnapshot | None = None,
    ) -> FactInvalidationPlanResult:
        prepared_records = self.evidence_planner.prepare_validated_claim_bundle(
            observation=observation,
            bundle=bundle,
            snapshot=snapshot,
        )
        if not prepared_records:
            return FactInvalidationPlanResult(plan=MutationPlan(), invalidated_count=0)

        new_fact_key = canonical_claim_key(
            claim_kind=bundle.claim_kind,
            claim_text=bundle.claim_text,
        )
        about_record_ids = tuple(
            dict.fromkeys(
                target_record_id
                for prepared in prepared_records
                for target_record_id in prepared.about_record_ids
            )
        )
        current_facts = self.current_evidence_reader.load_current_facts(
            repo_id=observation.repo_id,
            claim_kind=bundle.claim_kind,
            about_record_ids=about_record_ids,
            excluded_fact_keys=(new_fact_key,),
        )
        contradicted_facts = self._resolve_contradicted_facts(
            observation=observation,
            bundle=bundle,
            current_facts=current_facts,
            about_record_ids=about_record_ids,
            fact_key=new_fact_key,
        )
        new_fact_state = EvidenceFactState(
            fact_key=new_fact_key,
            claim_text=bundle.claim_text,
            claim_kind=bundle.claim_kind,
            claim_confidence=bundle.confidence,
            claim_derivation_kind=bundle.derivation_kind,
            observation_id=observation.observation_id,
            observed_at=observation.observed_at,
        )
        if not contradicted_facts:
            return FactInvalidationPlanResult(
                plan=self.evidence_planner.plan_prepared_records(
                    observation=observation,
                    prepared_records=prepared_records,
                    fact_state=new_fact_state,
                ),
                invalidated_count=0,
            )

        winner = _newest_fact(contradicted_facts)
        if winner is not None and _is_strictly_newer(winner.observed_at, observation.observed_at):
            losing_state = EvidenceFactState(
                fact_key=new_fact_key,
                claim_text=bundle.claim_text,
                claim_kind=bundle.claim_kind,
                claim_confidence=bundle.confidence,
                claim_derivation_kind=bundle.derivation_kind,
                observation_id=observation.observation_id,
                observed_at=observation.observed_at,
                is_current=False,
                invalidated_at=winner.observed_at,
                invalidated_by_fact_key=winner.fact_key,
                invalidated_by_observation_id=winner.observation_id,
            )
            return FactInvalidationPlanResult(
                plan=self.evidence_planner.plan_prepared_records(
                    observation=observation,
                    prepared_records=prepared_records,
                    fact_state=losing_state,
                    superseded_by_record_ids=_evidence_ids(winner),
                ),
                invalidated_count=0,
            )

        new_plan = self.evidence_planner.plan_prepared_records(
            observation=observation,
            prepared_records=prepared_records,
            fact_state=new_fact_state,
            supersedes_record_ids=tuple(
                evidence_id
                for fact in contradicted_facts
                for evidence_id in _evidence_ids(fact)
            ),
        )
        invalidation_updates = [
            self.evidence_planner.plan_existing_record_state_update(
                record=record,
                invalidated_at=observation.observed_at,
                invalidated_by_fact_key=new_fact_key,
                invalidated_by_observation_id=observation.observation_id,
            )
            for fact in contradicted_facts
            for record in fact.evidence_records
        ]
        invalidated_count = sum(len(fact.evidence_records) for fact in contradicted_facts)
        return FactInvalidationPlanResult(
            plan=merge_mutation_plans(new_plan, *invalidation_updates),
            invalidated_count=invalidated_count,
        )

    def _resolve_contradicted_facts(
        self,
        *,
        observation: Observation,
        bundle: ValidatedClaimBundle,
        current_facts: Sequence[PersistedEvidenceFact],
        about_record_ids: Sequence[str],
        fact_key: str,
    ) -> tuple[PersistedEvidenceFact, ...]:
        if not current_facts or self.fact_conflict_resolution_service is None:
            return ()
        try:
            resolution = self.fact_conflict_resolution_service.resolve(
                repo_id=observation.repo_id,
                new_fact={
                    "fact_key": fact_key,
                    "claim_text": bundle.claim_text,
                    "claim_kind": bundle.claim_kind,
                    "observed_at": observation.observed_at,
                    "about_record_ids": list(about_record_ids),
                },
                current_facts=[
                    {
                        "candidate_id": fact.fact_key,
                        "fact_key": fact.fact_key,
                        "claim_text": fact.claim_text,
                        "claim_kind": fact.claim_kind,
                        "observed_at": fact.observed_at,
                        "about_record_ids": list(fact.about_record_ids),
                    }
                    for fact in current_facts
                ],
            )
        except Exception:
            return ()
        fact_by_id = {fact.fact_key: fact for fact in current_facts}
        return tuple(
            fact_by_id[candidate_id]
            for candidate_id in resolution.contradicted_fact_ids
            if candidate_id in fact_by_id
        )


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
            fact_key=_string_or_none(properties.get("fact_key")) or canonical_claim_key(
                claim_kind=claim_kind,
                claim_text=claim_text,
            ),
            claim_text=claim_text,
            claim_kind=claim_kind,
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
    fact_key: str | None,
    claim_text: str | None,
    claim_kind: str | None,
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
        fact_key=fact_key,
        claim_text=claim_text,
        claim_kind=claim_kind,
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
