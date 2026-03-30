"""Typed structural planning for GitHub provenance records.

This planner keeps GitHub API collection separate from backend mutation
construction. The GitHub indexer builds a provenance bundle from live API
responses, then this module translates that bundle into provider-neutral
mutation records for the additive backend.
"""

from __future__ import annotations

import json
import re
from typing import Any

from context_graph.backend import (
    EdgeRecord,
    MutationPlan,
    NodeRecord,
    RecordKind,
    SearchDocumentRecord,
)

_RECORD_TYPE_RE = re.compile(r"[^a-z0-9]+")


class GitHubStructuralPlanner:
    """Translate GitHub structural provenance into backend mutation records."""

    def plan_bundle(self, bundle: Any) -> MutationPlan:
        """Plan GitHub entity/link upserts for one provenance bundle."""

        nodes: list[NodeRecord] = []
        edges: list[EdgeRecord] = []
        search_documents: list[SearchDocumentRecord] = []

        for entity in bundle.entities:
            nodes.append(_entity_record(entity))
            search_documents.append(_entity_search_document(entity))

        for link in bundle.project_links:
            edges.append(_project_link_record(bundle.repo_id, link))
        for link in bundle.commit_links:
            edges.append(_commit_link_record(bundle.repo_id, link))
        for link in bundle.entity_links:
            edges.append(_entity_link_record(bundle.repo_id, link))

        return MutationPlan(
            upsert_nodes=tuple(nodes),
            upsert_edges=tuple(edges),
            upsert_search_documents=tuple(search_documents),
        )


def _entity_record(entity: Any) -> NodeRecord:
    record_type = _entity_record_type(entity.kind)
    return NodeRecord(
        record_id=entity.node_id,
        record_type=record_type,
        repo_id=entity.repo_id,
        group_id=entity.repo_id,
        labels=("BackendRecord", "GitHubEntity", entity.kind),
        properties={
            "node_id": entity.node_id,
            "kind": entity.kind,
            "github_repo": entity.github_repo,
            "external_id": entity.external_id,
            "number": entity.number,
            "url": entity.url,
            "title": entity.title,
            "state": entity.state,
            "author_login": entity.author_login,
            "created_at": entity.created_at,
            "updated_at": entity.updated_at,
            "metadata_json": json.dumps(entity.metadata, sort_keys=True, separators=(",", ":")),
        },
    )


def _entity_search_document(entity: Any) -> SearchDocumentRecord:
    record_type = _entity_record_type(entity.kind)
    return SearchDocumentRecord(
        record_kind=RecordKind.NODE,
        record_id=entity.node_id,
        record_type=record_type,
        repo_id=entity.repo_id,
        group_id=entity.repo_id,
        search_text=_search_text(
            entity.kind,
            entity.title,
            f"#{entity.number}" if entity.number is not None else None,
            entity.state,
            entity.author_login,
            entity.github_repo,
        ),
        properties={
            "kind": entity.kind,
            "number": entity.number,
            "state": entity.state,
            "author_login": entity.author_login,
            "github_repo": entity.github_repo,
        },
    )


def _project_link_record(repo_id: str, link: Any) -> EdgeRecord:
    record_type = _relationship_record_type(link.relation_type)
    return EdgeRecord(
        record_id=f"{record_type}:project:{repo_id}:{link.target_node_id}",
        record_type=record_type,
        repo_id=repo_id,
        group_id=repo_id,
        relationship_type=link.relation_type,
        source_record_id=f"project:{repo_id}",
        target_record_id=link.target_node_id,
        properties={},
    )


def _commit_link_record(repo_id: str, link: Any) -> EdgeRecord:
    record_type = _relationship_record_type(link.relation_type)
    commit_record_id = f"commit:{repo_id}:{link.commit_sha}"
    return EdgeRecord(
        record_id=f"{record_type}:{commit_record_id}:{link.target_node_id}",
        record_type=record_type,
        repo_id=repo_id,
        group_id=repo_id,
        relationship_type=link.relation_type,
        source_record_id=commit_record_id,
        target_record_id=link.target_node_id,
        properties={},
    )


def _entity_link_record(repo_id: str, link: Any) -> EdgeRecord:
    record_type = _relationship_record_type(link.relation_type)
    return EdgeRecord(
        record_id=f"{record_type}:{link.source_node_id}:{link.target_node_id}",
        record_type=record_type,
        repo_id=repo_id,
        group_id=repo_id,
        relationship_type=link.relation_type,
        source_record_id=link.source_node_id,
        target_record_id=link.target_node_id,
        properties={},
    )


def _entity_record_type(kind: str) -> str:
    return f"github_{_slug(kind)}"


def _relationship_record_type(relation_type: str) -> str:
    return _slug(relation_type)


def _search_text(*parts: object) -> str:
    return " ".join(part for part in (_normalized_text(value) for value in parts) if part)


def _normalized_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _slug(value: str) -> str:
    snake_ready = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value.strip())
    normalized = _RECORD_TYPE_RE.sub("_", snake_ready.lower()).strip("_")
    return normalized or "record"
