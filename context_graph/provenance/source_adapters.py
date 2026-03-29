"""Observation source adapters for provenance ingestion."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Sequence

from context_graph.ingestion.git.git_metadata import CommitInfo
from context_graph.models import GraphSnapshot

from .models import (
    ObservationKind,
    ObservationLink,
    markdown_document_observation_id,
    observation_kind_value,
)
from .planner import ObservationMutationPlanner, PlannedObservation

_MARKDOWN_TITLE_RE = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*$", re.MULTILINE)


def build_commit_observation(
    *,
    planner: ObservationMutationPlanner,
    repo_id: str,
    commit: CommitInfo,
) -> PlannedObservation | None:
    return planner.build_commit_message_observation(repo_id=repo_id, commit=commit)


def build_github_observations(
    *,
    planner: ObservationMutationPlanner,
    bundle: Any,
) -> tuple[PlannedObservation, ...]:
    return planner.build_github_observations(bundle=bundle)


@dataclass(frozen=True, slots=True)
class AgentObservationPayload:
    repo_id: str
    observation_text: str
    observation_kind: str
    title: str | None = None
    source_kind: str = "agent"
    source_id: str | None = None
    context_kind: str | None = None
    context_id: str | None = None
    observed_at: str | None = None
    author: str | None = None
    task_id: str | None = None
    metadata: Mapping[str, Any] | None = None


class MarkdownObservationAdapter:
    """Build markdown document observations from a deterministic snapshot checkout."""

    def build_observations(
        self,
        *,
        planner: ObservationMutationPlanner,
        snapshot: GraphSnapshot,
        checkout_root: str,
    ) -> tuple[PlannedObservation, ...]:
        planned: list[PlannedObservation] = []
        root_path = Path(checkout_root)
        for node in snapshot.nodes:
            if node.kind != "File" or not node.file_path:
                continue
            relative_path = str(node.file_path).strip()
            if not relative_path.lower().endswith(".md"):
                continue
            file_path = root_path / relative_path
            if not file_path.is_file():
                continue
            try:
                raw_text = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raw_text = file_path.read_text(encoding="utf-8", errors="ignore")
            body = raw_text.strip()
            if not body:
                continue
            title = _markdown_title(body) or Path(relative_path).stem
            planned_observation = planner.build_observation(
                observation_id=markdown_document_observation_id(
                    snapshot.repo_id,
                    snapshot.revision_id,
                    relative_path,
                ),
                repo_id=snapshot.repo_id,
                observation_kind=ObservationKind.MARKDOWN_DOCUMENT.value,
                source_kind="document",
                source_id=node.node_id,
                source_record_type="code_node",
                observed_at=snapshot.generated_at,
                title=title,
                text=body,
                metadata={
                    "document_path": relative_path,
                    "file_path": relative_path,
                    "revision_id": snapshot.revision_id,
                    "source_node_id": node.node_id,
                },
                context_kind="document",
                context_id=relative_path,
                about_links=(
                    ObservationLink(
                        relation_type="ABOUT",
                        target_record_id=node.node_id,
                        target_record_type="code_node",
                        properties={"source": "markdown_document"},
                    ),
                ),
                display_name=relative_path,
            )
            if planned_observation is not None:
                planned.append(planned_observation)
        return tuple(planned)


class AgentObservationAdapter:
    """Build agent-originated observations for persisted MCP writes."""

    def build_observation(
        self,
        *,
        planner: ObservationMutationPlanner,
        payload: AgentObservationPayload,
    ) -> PlannedObservation:
        normalized_kind = observation_kind_value(payload.observation_kind)
        if normalized_kind == "agent_observation":
            normalized_kind = ObservationKind.AGENT_NOTE.value
        resolved_context_id = payload.context_id or payload.task_id or payload.source_id
        resolved_source_id = payload.source_id or _agent_source_id(
            repo_id=payload.repo_id,
            source_kind=payload.source_kind,
            context_id=resolved_context_id,
            observation_kind=normalized_kind,
        )
        planned = planner.build_observation(
            observation_id=None,
            repo_id=payload.repo_id,
            observation_kind=normalized_kind,
            source_kind=payload.source_kind,
            source_id=resolved_source_id,
            source_record_type="observation_source",
            observed_at=payload.observed_at,
            author=payload.author,
            title=payload.title,
            text=payload.observation_text,
            metadata={
                **dict(payload.metadata or {}),
                **({"task_id": payload.task_id} if payload.task_id else {}),
            },
            context_kind=payload.context_kind,
            context_id=resolved_context_id,
            display_name=payload.title or payload.task_id or resolved_source_id,
            ensure_source_node=True,
            source_metadata={
                "source_kind": payload.source_kind,
                "context_kind": payload.context_kind,
                "context_id": resolved_context_id,
                **dict(payload.metadata or {}),
            },
        )
        if planned is None:
            raise ValueError("agent observations require non-empty observation_text")
        return planned


def _markdown_title(text: str) -> str | None:
    match = _MARKDOWN_TITLE_RE.search(text)
    if match is None:
        return None
    title = " ".join(match.group("title").split()).strip()
    return title or None


def _agent_source_id(
    *,
    repo_id: str,
    source_kind: str,
    context_id: str | None,
    observation_kind: str,
) -> str:
    core = context_id or observation_kind or "agent"
    return f"observation_source:{source_kind}:{repo_id}:{core}"
