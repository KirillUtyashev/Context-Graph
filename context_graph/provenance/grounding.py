"""Deterministic grounding helpers for provenance objects."""

from __future__ import annotations

from collections.abc import Mapping

from context_graph.code_identity import SnapshotCodeResolver
from context_graph.models import GraphSnapshot

from .hint_enrichment import EnrichedAboutHint, infer_hint_kind, normalize_hint_value

_STATE_METADATA_KEY = "deterministic_mutation_state"


def resolve_about_record_ids(
    snapshot: GraphSnapshot | None,
    hints: list[str | EnrichedAboutHint] | tuple[str | EnrichedAboutHint, ...],
) -> tuple[str, ...]:
    if snapshot is None:
        return ()
    resolver = SnapshotCodeResolver(snapshot)
    return tuple(
        _active_version_record_id(snapshot, node.node_id)
        for node in resolver.resolve_about_hints(_resolver_hints(hints))
    )


def resolve_mention_record_ids(
    snapshot: GraphSnapshot | None,
    hints: list[str | EnrichedAboutHint] | tuple[str | EnrichedAboutHint, ...],
) -> tuple[str, ...]:
    if snapshot is None:
        return ()
    resolver = SnapshotCodeResolver(snapshot)
    return tuple(node.node_id for node in resolver.resolve_about_hints(_resolver_hints(hints)))


def resolve_about_record_id(snapshot: GraphSnapshot | None, hint: str | EnrichedAboutHint) -> str | None:
    if snapshot is None:
        return None
    resolver = SnapshotCodeResolver(snapshot)
    node = resolver.resolve_about_hint(_resolver_hint(hint))
    if node is None:
        return None
    return _active_version_record_id(snapshot, node.node_id)


def _resolver_hints(
    hints: list[str | EnrichedAboutHint] | tuple[str | EnrichedAboutHint, ...],
) -> tuple[str | tuple[str, str | None], ...]:
    return tuple(_resolver_hint(hint) for hint in hints)


def _resolver_hint(hint: str | EnrichedAboutHint) -> str | tuple[str, str | None]:
    if isinstance(hint, EnrichedAboutHint):
        return (hint.value, hint.hint_kind.value)
    normalized_hint = normalize_hint_value(str(hint))
    hint_kind = infer_hint_kind(normalized_hint)
    if hint_kind is None:
        return normalized_hint
    return (normalized_hint, hint_kind.value)


def _active_version_record_id(snapshot: GraphSnapshot, node_id: str) -> str:
    raw_state = snapshot.metadata.get(_STATE_METADATA_KEY)
    if isinstance(raw_state, Mapping):
        raw_active_versions = raw_state.get("active_node_versions")
        if isinstance(raw_active_versions, Mapping):
            active_version_id = raw_active_versions.get(node_id)
            if active_version_id:
                return str(active_version_id)
    return f"{node_id}@{snapshot.revision_id}"
