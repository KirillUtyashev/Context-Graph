from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Mapping, Sequence

from .episodes import ContextNodeRef, FactInvalidation, FactRecord
from .models import canonical_json


SEMANTIC_FACT_PROFILE_KIND = "semantic_fact_profile"
FACT_EDGE_TARGET_KIND = "FactEdge"


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
    keys: tuple[str, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    is_active: bool = True
    valid_from_revision: str | None = None
    valid_to_revision: str | None = None
    updated_at: str = ""

    def keys_json(self) -> str:
        return json.dumps(list(self.keys), sort_keys=False, separators=(",", ":"))

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


def semantic_fact_profile_from_fact(
    fact: FactRecord,
    *,
    source: ContextNodeRef | None,
    target: ContextNodeRef | None,
) -> ProfileRecord:
    source_name = source.name if source else fact.source_node_id
    target_name = target.name if target else fact.target_node_id
    source_kind = source.kind if source else _string_or_none(fact.metadata.get("source_kind"))
    target_kind = target.kind if target else _string_or_none(fact.metadata.get("target_kind"))
    relation_label = _relation_label(fact.relation_type)
    value_text = (
        f"{source_kind or 'Entity'} {source_name} {relation_label} "
        f"{target_kind or 'Entity'} {target_name}. {fact.fact}"
    )
    metadata = {
        "fact_id": fact.fact_id,
        "episode_ids": list(fact.episode_ids),
        "source_name": source_name,
        "target_name": target_name,
        "source_kind": source_kind,
        "target_kind": target_kind,
        "created_at": fact.created_at,
        "valid_at": fact.valid_at,
        "invalid_at": fact.invalid_at,
        "expired_at": fact.expired_at,
    }
    if source and source.file_path:
        metadata["source_file_path"] = source.file_path
    if target and target.file_path:
        metadata["target_file_path"] = target.file_path
    return ProfileRecord(
        profile_id=f"profile:{fact.repo_id}:{SEMANTIC_FACT_PROFILE_KIND}:{fact.fact_id}",
        repo_id=fact.repo_id,
        profile_kind=SEMANTIC_FACT_PROFILE_KIND,
        profile_target_kind=FACT_EDGE_TARGET_KIND,
        profile_target_id=fact.fact_id,
        relation_type=fact.relation_type,
        source_node_id=fact.source_node_id,
        source_node_kind=source_kind,
        target_node_id=fact.target_node_id,
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
        updated_at=fact.expired_at or fact.created_at,
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
    fact: FactRecord,
    source: ContextNodeRef | None,
    target: ContextNodeRef | None,
) -> tuple[str, ...]:
    values: list[str] = [
        fact.relation_type,
        _relation_label(fact.relation_type),
        fact.fact,
    ]
    if source:
        values.extend(source.searchable_terms())
    else:
        values.append(fact.source_node_id)
    if target:
        values.extend(target.searchable_terms())
    else:
        values.append(fact.target_node_id)
    deduped: dict[str, str] = {}
    for value in values:
        normalized = " ".join(str(value).strip().split())
        if not normalized:
            continue
        deduped.setdefault(normalized.casefold(), normalized)
    return tuple(deduped.values())


def _relation_label(value: str | None) -> str:
    if not value:
        return "relates to"
    return value.replace("_", " ").strip().lower()


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
