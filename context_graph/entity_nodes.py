"""Lightweight entity-node helpers used by deterministic storage layers.

This module provides the minimal node shape needed by metadata storage,
profile generation, and related deterministic logic without importing the
archived Graphiti-backed memory models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class EntityNode:
    """Compact code/provenance node representation used outside Graphiti.

    The archived Graphiti compatibility layer exposes a richer Pydantic model,
    but the deterministic storage and profile layers only need a stable subset
    of fields: identity, display name, labels, and arbitrary attributes.
    """

    uuid: str
    name: str
    group_id: str
    labels: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)


def make_entity_node(
    *,
    node_id: str,
    repo_id: str,
    kind: str,
    name: str,
    qualified_name: str | None = None,
    file_path: str | None = None,
    aliases: Sequence[str] = (),
    metadata: Mapping[str, Any] | None = None,
) -> EntityNode:
    """Build a lightweight entity node from the historical field names."""

    attrs: dict[str, Any] = {}
    if qualified_name is not None:
        attrs["qualified_name"] = qualified_name
    if file_path is not None:
        attrs["file_path"] = file_path
    if aliases:
        attrs["aliases"] = list(aliases)
    if metadata:
        attrs.update(metadata)
    labels = (kind,) if kind and kind != "Entity" else ()
    return EntityNode(
        uuid=node_id,
        name=name,
        group_id=repo_id,
        labels=labels,
        attributes=attrs,
    )


def entity_node_kind(node: EntityNode) -> str:
    """Return the primary semantic label for a stored entity node."""

    for label in node.labels:
        if label != "Entity":
            return label
    return "Entity"


def entity_node_qualified_name(node: EntityNode) -> str | None:
    """Return the qualified symbol name when present."""

    return _string_or_none(node.attributes.get("qualified_name"))


def entity_node_file_path(node: EntityNode) -> str | None:
    """Return the associated file path when present."""

    return _string_or_none(node.attributes.get("file_path"))


def entity_node_aliases(node: EntityNode) -> tuple[str, ...]:
    """Return any alternate names stored on the node."""

    aliases = node.attributes.get("aliases", [])
    if isinstance(aliases, (list, tuple)):
        return tuple(str(alias) for alias in aliases if str(alias).strip())
    return ()


def entity_node_searchable_terms(node: EntityNode) -> tuple[str, ...]:
    """Build the lexical terms used when profiling or searching a node."""

    values = {
        node.name,
        entity_node_qualified_name(node) or "",
        entity_node_file_path(node) or "",
        *entity_node_aliases(node),
    }
    file_path = entity_node_file_path(node)
    if file_path:
        values.add(file_path.rsplit("/", 1)[-1])
    return tuple(sorted(value for value in values if value))


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
