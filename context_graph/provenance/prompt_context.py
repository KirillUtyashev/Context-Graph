"""Sanitized prompt-context builders for provenance extraction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from context_graph.code_identity import SnapshotCodeResolver
from context_graph.models import CanonicalNode, GraphSnapshot

from .hint_enrichment import collect_observation_grounding_hints
from .models import Observation

_MAX_CHANGED_FILES = 10
_MAX_CONTEXT_ENTITIES = 8
_CHANGED_FILE_METADATA_KEYS = frozenset({"changed_files", "commit_changed_files", "paths", "path"})


def build_prompt_context(
    observation: Observation,
    *,
    code_context: Mapping[str, Any] | None = None,
    source_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "observation": build_prompt_observation(observation),
        "code_context": sanitize_prompt_code_context(code_context or {}),
        "source_context": build_prompt_source_context(observation, source_context=source_context),
    }


def build_prompt_observation(observation: Observation) -> dict[str, Any]:
    payload = {
        "source_type": observation.observation_kind.value,
        "text": _normalize_text(observation.text),
    }
    if observation.title:
        payload["title"] = _normalize_text(observation.title)
    if observation.observed_at:
        payload["observed_at"] = observation.observed_at
    return payload


def build_prompt_source_context(
    observation: Observation,
    *,
    source_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source_context = dict(source_context or {})
    metadata = dict(observation.metadata)
    payload: dict[str, Any] = {}

    changed_files = _normalize_changed_files(
        source_context.get("changed_files")
        or metadata.get("changed_files")
        or metadata.get("commit_changed_files")
    )
    if observation.source_kind == "commit":
        if changed_files:
            payload["changed_files"] = changed_files
        return payload

    pull_request_number = _coerce_int(
        metadata.get("pull_request_number") or source_context.get("pull_request_number")
    )
    issue_number = _coerce_int(metadata.get("issue_number") or source_context.get("issue_number"))
    pr_title = _normalize_text(source_context.get("pr_title") or observation.title)
    issue_title = _normalize_text(source_context.get("issue_title") or observation.title)

    if pull_request_number is not None:
        payload["pr_number"] = pull_request_number
        if pr_title:
            payload["pr_title"] = pr_title
    if issue_number is not None:
        payload["issue_number"] = issue_number
        if issue_title:
            payload["issue_title"] = issue_title
    return payload


def build_deterministic_code_context(
    observation: Observation,
    *,
    snapshot: GraphSnapshot | None,
) -> dict[str, Any]:
    changed_files = _normalize_changed_files(_observation_changed_files(observation))
    if snapshot is None:
        payload: dict[str, Any] = {}
        if changed_files:
            payload["changed_files"] = changed_files
        return payload

    resolver = SnapshotCodeResolver(snapshot)
    mentioned_entities = [
        _serialize_node(node)
        for node in resolver.resolve_about_hints(
            tuple(
                (hint.value, hint.hint_kind.value)
                for hint in collect_observation_grounding_hints(observation=observation)
            )
        )[:_MAX_CONTEXT_ENTITIES]
    ]
    touched_entities = [
        _serialize_node(node)
        for node in resolver.resolve_changed_files(changed_files)[:_MAX_CONTEXT_ENTITIES]
    ]
    payload = sanitize_prompt_code_context(
        {
            "changed_files": changed_files,
            "mentioned_entities": mentioned_entities,
            "touched_entities": touched_entities,
        }
    )
    return payload


def sanitize_prompt_code_context(code_context: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    changed_files = _normalize_changed_files(code_context.get("changed_files"))
    if changed_files:
        payload["changed_files"] = changed_files

    mentioned_entities = _normalize_entities(code_context.get("mentioned_entities"))
    if mentioned_entities:
        payload["mentioned_entities"] = mentioned_entities

    touched_entities = _normalize_entities(code_context.get("touched_entities"))
    if touched_entities:
        payload["touched_entities"] = touched_entities
    return payload


def _normalize_entities(value: Any) -> list[dict[str, str]]:
    entities: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value or ():
        if not isinstance(item, Mapping):
            continue
        name = _normalize_text(item.get("name"))
        kind = _normalize_text(item.get("kind"))
        file_path = _normalize_text(item.get("file_path"))
        if not name or not kind:
            continue
        key = (name, kind, file_path)
        if key in seen:
            continue
        seen.add(key)
        entity = {"name": name, "kind": kind}
        if file_path:
            entity["file_path"] = file_path
        entities.append(entity)
        if len(entities) >= _MAX_CONTEXT_ENTITIES:
            break
    return entities


def _serialize_node(node: CanonicalNode) -> dict[str, str]:
    payload = {
        "name": _normalize_text(node.name),
        "kind": _normalize_text(node.kind),
    }
    if node.file_path:
        payload["file_path"] = _normalize_text(node.file_path)
    return payload


def _observation_changed_files(observation: Observation) -> Sequence[str]:
    for key in _CHANGED_FILE_METADATA_KEYS:
        value = observation.metadata.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return tuple(str(item) for item in value if item is not None)
    value = observation.metadata.get("file_path") or observation.metadata.get("path")
    if value is None:
        return ()
    return (str(value),)


def _normalize_changed_files(value: Any) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    values: Sequence[Any]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = value
    elif value is None:
        values = ()
    else:
        values = (value,)
    for item in values:
        text = _normalize_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
        if len(normalized) >= _MAX_CHANGED_FILES:
            break
    return normalized


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()
