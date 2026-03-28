"""Deterministic grounding helpers for provenance objects."""

from __future__ import annotations

from context_graph.code_identity import SnapshotCodeResolver
from context_graph.models import GraphSnapshot

from .hint_enrichment import EnrichedAboutHint, infer_hint_kind, normalize_hint_value


def resolve_about_record_ids(  # pragma: no cover
    snapshot: GraphSnapshot | None,
    hints: list[str | EnrichedAboutHint] | tuple[str | EnrichedAboutHint, ...],
) -> tuple[str, ...]:
    if snapshot is None:
        return ()
    resolver = SnapshotCodeResolver(snapshot)
    return resolver.resolve_about_record_ids(_resolver_hints(hints))


def resolve_about_record_id(  # pragma: no cover
    snapshot: GraphSnapshot | None, hint: str | EnrichedAboutHint
) -> str | None:
    if snapshot is None:
        return None
    resolver = SnapshotCodeResolver(snapshot)
    return resolver.resolve_about_record_id(_resolver_hint(hint))


def _resolver_hints(  # pragma: no cover
    hints: list[str | EnrichedAboutHint] | tuple[str | EnrichedAboutHint, ...],
) -> tuple[str | tuple[str, str | None], ...]:
    return tuple(_resolver_hint(hint) for hint in hints)


def _resolver_hint(hint: str | EnrichedAboutHint) -> str | tuple[str, str | None]:  # pragma: no cover
    if isinstance(hint, EnrichedAboutHint):
        return (hint.value, hint.hint_kind.value)
    normalized_hint = normalize_hint_value(str(hint))
    hint_kind = infer_hint_kind(normalized_hint)
    if hint_kind is None:
        return normalized_hint
    return (normalized_hint, hint_kind.value)
