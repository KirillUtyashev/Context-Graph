from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from .entity_nodes import (
    EntityNode,
    entity_node_file_path,
    entity_node_kind,
    entity_node_searchable_terms,
)
from .graph import canonical_json

if TYPE_CHECKING:
    from context_graph.provenance.models import ClaimRecord, Observation
else:  # pragma: no cover - runtime uses duck typing for these richer graph objects
    ClaimRecord = Any
    Observation = Any

EntityEdge = Any
FactInvalidation = Any


SEMANTIC_FACT_PROFILE_KIND = "semantic_fact_profile"
SEMANTIC_CLAIM_PROFILE_KIND = "semantic_claim_profile"
SEMANTIC_OBSERVATION_PROFILE_KIND = "semantic_observation_profile"
SEMANTIC_RELATION_PROFILE_KIND = "semantic_relation_profile"
FACT_EDGE_TARGET_KIND = "FactEdge"
CLAIM_TARGET_KIND = "Claim"
OBSERVATION_TARGET_KIND = "Observation"
RELATION_TARGET_KIND = "Relation"
PROFILE_KEY_ORIGIN_RAW_TEXT = "raw_text"
PROFILE_KEY_ORIGIN_GENERATED_SEMANTIC = "generated_semantic"
PROFILE_KEY_ORIGIN_LEGACY_KEYWORD = "legacy_keyword"


@dataclass(frozen=True, slots=True)
class ProfileKeyDefinition:
    text: str
    origin: str

    def as_dict(self) -> dict[str, str]:
        return {
            "text": self.text,
            "origin": self.origin,
        }

    @classmethod
    def from_json_value(cls, value: object) -> "ProfileKeyDefinition | None":
        if isinstance(value, str):
            normalized = _normalize_text(value)
            if not normalized:
                return None
            return cls(
                text=normalized,
                origin=PROFILE_KEY_ORIGIN_LEGACY_KEYWORD,
            )
        if not isinstance(value, Mapping):
            return None
        normalized = _normalize_text(value.get("text"))
        if not normalized:
            return None
        origin = _normalize_text(value.get("origin")) or PROFILE_KEY_ORIGIN_LEGACY_KEYWORD
        return cls(text=normalized, origin=origin)


@dataclass(frozen=True, slots=True)
class ProfileRecord:
    profile_id: str
    repo_id: str
    profile_kind: str
    profile_target_kind: str
    profile_target_id: str
    relation_type: str | None
    source_node_id: str | None
    source_node_kind: str | None
    target_node_id: str | None
    target_node_kind: str | None
    target_revision_id: str | None
    value_text: str
    keys: tuple[ProfileKeyDefinition, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    is_active: bool = True
    valid_from_revision: str | None = None
    valid_to_revision: str | None = None
    updated_at: str = ""

    def keys_json(self) -> str:
        serialized_keys: list[dict[str, str]] = []
        for key in self.keys:
            if isinstance(key, ProfileKeyDefinition):
                serialized_keys.append(key.as_dict())
                continue
            normalized = ProfileKeyDefinition.from_json_value(key)
            if normalized is not None:
                serialized_keys.append(normalized.as_dict())
        return json.dumps(
            serialized_keys,
            sort_keys=False,
            separators=(",", ":"),
        )

    def metadata_json(self) -> str:
        return canonical_json(self.metadata)


@dataclass(frozen=True, slots=True)
class ProfileDeactivation:
    repo_id: str
    profile_kind: str
    relation_type: str | None
    source_node_id: str | None
    target_node_id: str | None
    observed_at: str
    valid_to_revision: str | None = None
    replacement_profile_id: str | None = None


@dataclass(frozen=True, slots=True)
class RelationProfileSpec:
    repo_id: str
    profile_target_id: str
    profile_target_kind: str = RELATION_TARGET_KIND
    relation_type: str | None = None
    source_record_id: str | None = None
    source_record_type: str | None = None
    target_record_id: str | None = None
    target_record_type: str | None = None
    value_text: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    is_active: bool = True
    updated_at: str = ""


def semantic_fact_profile_from_fact(
    fact: EntityEdge,
    *,
    source: EntityNode | None,
    target: EntityNode | None,
) -> ProfileRecord:
    source_name = source.name if source else fact.source_node_uuid
    target_name = target.name if target else fact.target_node_uuid
    source_kind = entity_node_kind(source) if source else _string_or_none(fact.attributes.get("source_kind"))
    target_kind = entity_node_kind(target) if target else _string_or_none(fact.attributes.get("target_kind"))
    relation_label = _relation_label(fact.name)
    value_text = _normalize_text(fact.fact)
    metadata = {
        "fact_id": fact.uuid,
        "episode_ids": list(fact.episodes),
        "source_name": source_name,
        "target_name": target_name,
        "source_kind": source_kind,
        "target_kind": target_kind,
        "created_at": str(fact.created_at),
        "valid_at": str(fact.valid_at) if fact.valid_at else None,
        "invalid_at": str(fact.invalid_at) if fact.invalid_at else None,
        "expired_at": str(fact.expired_at) if fact.expired_at else None,
    }
    if source and entity_node_file_path(source):
        metadata["source_file_path"] = entity_node_file_path(source)
    if target and entity_node_file_path(target):
        metadata["target_file_path"] = entity_node_file_path(target)
    return ProfileRecord(
        profile_id=f"profile:{fact.group_id}:{SEMANTIC_FACT_PROFILE_KIND}:{fact.uuid}",
        repo_id=fact.group_id,
        profile_kind=SEMANTIC_FACT_PROFILE_KIND,
        profile_target_kind=FACT_EDGE_TARGET_KIND,
        profile_target_id=fact.uuid,
        relation_type=fact.name,
        source_node_id=fact.source_node_uuid,
        source_node_kind=source_kind,
        target_node_id=fact.target_node_uuid,
        target_node_kind=target_kind,
        target_revision_id=None,
        value_text=value_text,
        keys=_profile_keys_for_fact(
            fact=fact,
            source=source,
            target=target,
        ),
        metadata=metadata,
        is_active=fact.expired_at is None,
        valid_from_revision=None,
        valid_to_revision=None,
        updated_at=str(fact.expired_at) if fact.expired_at else str(fact.created_at),
    )


def semantic_claim_profile_from_claim(
    claim: ClaimRecord,
    *,
    about_record_ids: Sequence[str] = (),
    support_texts: Sequence[str] = (),
    generated_keys: Sequence[str] = (),
) -> ProfileRecord:
    about_ids = tuple(dict.fromkeys(str(record_id) for record_id in about_record_ids if record_id))
    normalized_support_texts = tuple(
        dict.fromkeys(
            text
            for text in (_normalize_text(value) for value in support_texts)
            if text
        )
    )
    normalized_claim_text = _normalize_text(claim.claim_text)
    metadata = {
        "claim_id": claim.claim_id,
        "repo_id": claim.repo_id,
        "claim_text": normalized_claim_text,
        "canonical_key": claim.canonical_key,
        "claim_kind": claim.claim_kind,
        "claim_subkind": claim.claim_subkind,
        "context_kind": claim.context_kind,
        "context_id": claim.context_id,
        "thread_kind": claim.thread_kind,
        "thread_id": claim.thread_id,
        "about_record_ids": list(about_ids),
        "support_texts": list(normalized_support_texts),
        "is_current": claim.is_current,
        "valid_from": claim.valid_from,
        "valid_to": claim.valid_to,
    }
    return ProfileRecord(
        profile_id=f"profile:{claim.repo_id}:{SEMANTIC_CLAIM_PROFILE_KIND}:{claim.claim_id}",
        repo_id=claim.repo_id,
        profile_kind=SEMANTIC_CLAIM_PROFILE_KIND,
        profile_target_kind=CLAIM_TARGET_KIND,
        profile_target_id=claim.claim_id,
        relation_type=None,
        source_node_id=None,
        source_node_kind=None,
        target_node_id=None,
        target_node_kind=None,
        target_revision_id=None,
        value_text=normalized_claim_text,
        keys=_claim_profile_keys(
            claim_text=normalized_claim_text,
            generated_keys=generated_keys,
        ),
        metadata=metadata,
        is_active=claim.is_current,
        valid_from_revision=None,
        valid_to_revision=None,
        updated_at=claim.valid_to or claim.latest_observed_at or claim.ingested_at or "",
    )


def semantic_relation_profile_from_spec(
    spec: RelationProfileSpec,
) -> ProfileRecord | None:
    normalized_text = _normalize_text(spec.value_text)
    if not normalized_text:
        return None
    metadata = {
        **dict(spec.metadata),
        "relation_record_id": spec.profile_target_id,
        "relation_type": _string_or_none(spec.relation_type),
        "text_field": "value_text",
        "source_record_id": _string_or_none(spec.source_record_id),
        "source_record_type": _string_or_none(spec.source_record_type),
        "target_record_id": _string_or_none(spec.target_record_id),
        "target_record_type": _string_or_none(spec.target_record_type),
    }
    return ProfileRecord(
        profile_id=f"profile:{spec.repo_id}:{SEMANTIC_RELATION_PROFILE_KIND}:{spec.profile_target_id}",
        repo_id=spec.repo_id,
        profile_kind=SEMANTIC_RELATION_PROFILE_KIND,
        profile_target_kind=spec.profile_target_kind,
        profile_target_id=spec.profile_target_id,
        relation_type=_string_or_none(spec.relation_type),
        source_node_id=_string_or_none(spec.source_record_id),
        source_node_kind=_string_or_none(spec.source_record_type),
        target_node_id=_string_or_none(spec.target_record_id),
        target_node_kind=_string_or_none(spec.target_record_type),
        target_revision_id=None,
        value_text=normalized_text,
        keys=(
            ProfileKeyDefinition(
                text=normalized_text,
                origin=PROFILE_KEY_ORIGIN_RAW_TEXT,
            ),
        ),
        metadata=metadata,
        is_active=bool(spec.is_active),
        valid_from_revision=None,
        valid_to_revision=None,
        updated_at=spec.updated_at,
    )


def semantic_observation_profile_from_observation(
    observation: Observation,
) -> ProfileRecord:
    value_text = observation_profile_text(observation)
    metadata = {
        "observation_id": observation.observation_id,
        "observation_kind": observation.observation_kind,
        "source_kind": observation.source_kind,
        "source_id": observation.source_id,
        "context_kind": observation.context_kind,
        "context_id": observation.context_id,
        "thread_kind": observation.thread_kind,
        "thread_id": observation.thread_id,
        "observed_at": observation.observed_at,
        "ingested_at": observation.ingested_at,
    }
    return ProfileRecord(
        profile_id=f"profile:{observation.repo_id}:{SEMANTIC_OBSERVATION_PROFILE_KIND}:{observation.observation_id}",
        repo_id=observation.repo_id,
        profile_kind=SEMANTIC_OBSERVATION_PROFILE_KIND,
        profile_target_kind=OBSERVATION_TARGET_KIND,
        profile_target_id=observation.observation_id,
        relation_type=None,
        source_node_id=None,
        source_node_kind=None,
        target_node_id=None,
        target_node_kind=None,
        target_revision_id=None,
        value_text=value_text,
        keys=(
            ProfileKeyDefinition(
                text=value_text,
                origin=PROFILE_KEY_ORIGIN_RAW_TEXT,
            ),
        ),
        metadata=metadata,
        is_active=True,
        valid_from_revision=None,
        valid_to_revision=None,
        updated_at=observation.ingested_at or observation.observed_at or "",
    )


def profile_deactivation_from_fact_invalidation(
    invalidation: FactInvalidation,
) -> ProfileDeactivation:
    return ProfileDeactivation(
        repo_id=invalidation.repo_id,
        profile_kind=SEMANTIC_FACT_PROFILE_KIND,
        relation_type=invalidation.relation_type,
        source_node_id=invalidation.source_node_id,
        target_node_id=invalidation.target_node_id,
        observed_at=invalidation.observed_at,
        valid_to_revision=None,
        replacement_profile_id=(
            f"profile:{invalidation.repo_id}:{SEMANTIC_FACT_PROFILE_KIND}:{invalidation.replacement_fact_id}"
            if invalidation.replacement_fact_id
            else None
        ),
    )


def _profile_keys_for_fact(
    *,
    fact: EntityEdge,
    source: EntityNode | None,
    target: EntityNode | None,
) -> tuple[ProfileKeyDefinition, ...]:
    if fact.relationship_keywords:
        return _legacy_profile_keys(
            fact.relationship_keywords,
        )

    values: list[str] = [
        fact.fact,
        fact.name,
        _relation_label(fact.name),
    ]
    if source:
        values.extend(entity_node_searchable_terms(source))
    else:
        values.append(fact.source_node_uuid)
    if target:
        values.extend(entity_node_searchable_terms(target))
    else:
        values.append(fact.target_node_uuid)
    return _legacy_profile_keys(values)


def _relation_label(value: str | None) -> str:
    if not value:
        return "relates to"
    return value.replace("_", " ").strip().lower()


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _claim_profile_keys(
    *,
    claim_text: str,
    generated_keys: Sequence[str] = (),
) -> tuple[ProfileKeyDefinition, ...]:
    keys: list[ProfileKeyDefinition] = []
    normalized_claim_text = _normalize_text(claim_text)
    if normalized_claim_text:
        keys.append(
            ProfileKeyDefinition(
                text=normalized_claim_text,
                origin=PROFILE_KEY_ORIGIN_RAW_TEXT,
            )
        )
    for value in generated_keys:
        normalized = _normalize_text(value)
        if not normalized:
            continue
        if normalized.casefold() == normalized_claim_text.casefold():
            continue
        keys.append(
            ProfileKeyDefinition(
                text=normalized,
                origin=PROFILE_KEY_ORIGIN_GENERATED_SEMANTIC,
            )
        )
    return _dedupe_profile_keys(keys)


def observation_profile_text(observation: Observation, *, max_length: int = 4000) -> str:
    title = _normalize_text(observation.title)
    body = _normalize_text(observation.text)
    if title and body:
        combined = f"{title}\n\n{body}"
    else:
        combined = title or body
    return combined[:max_length]


def _legacy_profile_keys(values: Sequence[str]) -> tuple[ProfileKeyDefinition, ...]:
    keys: list[ProfileKeyDefinition] = []
    for index, value in enumerate(values):
        normalized = _normalize_text(value)
        if not normalized:
            continue
        origin = PROFILE_KEY_ORIGIN_RAW_TEXT if index == 0 else PROFILE_KEY_ORIGIN_LEGACY_KEYWORD
        keys.append(ProfileKeyDefinition(text=normalized, origin=origin))
    return _dedupe_profile_keys(keys)


def _dedupe_profile_keys(values: Sequence[ProfileKeyDefinition]) -> tuple[ProfileKeyDefinition, ...]:
    deduped: dict[tuple[str, str], ProfileKeyDefinition] = {}
    for value in values:
        normalized_text = _normalize_text(value.text)
        normalized_origin = _normalize_text(value.origin)
        if not normalized_text or not normalized_origin:
            continue
        key = (normalized_text.casefold(), normalized_origin.casefold())
        deduped.setdefault(
            key,
            ProfileKeyDefinition(
                text=normalized_text,
                origin=normalized_origin,
            ),
        )
    return tuple(deduped.values())


def _normalize_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())
