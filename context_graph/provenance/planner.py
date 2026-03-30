"""Mutation planning for provenance observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any

from context_graph.ingestion.git.git_metadata import CommitInfo
from context_graph.models.graph import GraphSnapshot
from context_graph.backend import (
    EdgeRecord,
    MutationPlan,
    NodeRecord,
    RecordKind,
    SearchDocumentRecord,
    merge_mutation_plans,
)

from .grounding import resolve_mention_record_ids
from .hint_enrichment import collect_observation_grounding_hints
from .models import (
    Observation,
    ObservationKind,
    ObservationLink,
    ObservationSourceRef,
    general_observation_id,
    git_commit_message_observation_id,
    github_issue_body_observation_id,
    github_issue_closed_observation_id,
    github_issue_comment_observation_id,
    github_pr_body_observation_id,
    github_pr_review_comment_observation_id,
    github_pr_review_observation_id,
    observation_kind_value,
)
from .schema_registry import DEFAULT_PROVENANCE_SCHEMA_REGISTRY


@dataclass(frozen=True, slots=True)
class PlannedObservation:
    """One observation plus the deterministic links it should emit."""

    observation: Observation
    source: ObservationSourceRef
    about_links: tuple[ObservationLink, ...] = ()


class ObservationMutationPlanner:
    """Translate provenance observations into backend mutation records."""

    def build_observation(
        self,
        *,
        observation_id: str | None,
        repo_id: str,
        observation_kind: str,
        source_kind: str,
        source_id: str,
        source_record_type: str,
        text: str,
        observed_at: str | None = None,
        author: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        context_kind: str | None = None,
        context_id: str | None = None,
        thread_kind: str | None = None,
        thread_id: str | None = None,
        about_links: tuple[ObservationLink, ...] = (),
        display_name: str | None = None,
        ensure_source_node: bool = False,
        source_metadata: dict[str, Any] | None = None,
    ) -> PlannedObservation | None:
        body = _normalized_text(text)
        if not body:
            return None
        policy = DEFAULT_PROVENANCE_SCHEMA_REGISTRY.observation_policy(
            observation_kind,
            source_kind=source_kind,
        )
        resolved_context_kind = (
            _string_or_none(context_kind)
            or (policy.default_context_kind if policy is not None else None)
            or _string_or_none(thread_kind)
        )
        resolved_context_id = _string_or_none(context_id) or _string_or_none(thread_id)
        observation = Observation(
            observation_id=observation_id
            or general_observation_id(
                repo_id=repo_id,
                observation_kind=observation_kind,
                source_id=source_id,
                context_id=resolved_context_id,
                observed_at=observed_at,
                title=title,
                text=body,
            ),
            repo_id=repo_id,
            observation_kind=observation_kind,
            source_kind=source_kind,
            source_id=source_id,
            observed_at=observed_at,
            author=_string_or_none(author),
            title=_string_or_none(title),
            text=body,
            metadata=dict(metadata or {}),
            ingested_at=_current_timestamp(),
            context_kind=resolved_context_kind,
            context_id=resolved_context_id,
            thread_kind=thread_kind,
            thread_id=thread_id,
        )
        source = ObservationSourceRef(
            source_kind=source_kind,
            source_id=source_id,
            source_record_type=source_record_type,
            repo_id=repo_id,
            display_name=display_name or _string_or_none(title) or source_id,
            ensure_source_node=ensure_source_node,
            source_metadata=dict(source_metadata or {}),
        )
        return PlannedObservation(
            observation=observation,
            source=source,
            about_links=about_links,
        )

    def build_commit_message_observation(
        self,
        *,
        repo_id: str,
        commit: CommitInfo,
    ) -> PlannedObservation | None:
        full_text = _full_commit_message(commit.subject, commit.body)
        if not full_text:
            return None
        return self.build_observation(
            observation_id=git_commit_message_observation_id(repo_id, commit.sha),
            repo_id=repo_id,
            observation_kind=ObservationKind.GIT_COMMIT_MESSAGE.value,
            source_kind="commit",
            source_id=f"commit:{repo_id}:{commit.sha}",
            source_record_type="commit",
            observed_at=commit.committed_at or commit.authored_at,
            author=commit.author_name or None,
            title=commit.subject or None,
            text=full_text,
            metadata={
                "commit_sha": commit.sha,
                "branch": commit.branch,
                "author_email": commit.author_email,
                "parent_shas": list(commit.parent_shas),
                "changed_files": list(commit.changed_files),
            },
            context_kind="commit",
            context_id=commit.sha,
            thread_kind="commit",
            thread_id=commit.sha,
            display_name=commit.sha,
        )

    def plan_commit_message_observation(
        self,
        *,
        repo_id: str,
        commit: CommitInfo,
        snapshot: GraphSnapshot | None = None,
    ) -> MutationPlan:
        planned = self.build_commit_message_observation(repo_id=repo_id, commit=commit)
        if planned is None:
            return MutationPlan()
        return self.plan_observations(planned, snapshot=snapshot)

    def build_github_observations(self, *, bundle: Any) -> tuple[PlannedObservation, ...]:
        observations: list[PlannedObservation] = []
        for source in getattr(bundle, "observation_sources", ()):
            body = _normalized_text(getattr(source, "text", None))
            if not body:
                continue
            observation_kind = observation_kind_value(getattr(source, "observation_kind"))
            thread_kind = _observation_thread_kind(
                source_kind=str(source.source_kind),
                observation_kind=observation_kind,
            )
            thread_id = _observation_thread_id(source=source)
            planned = self.build_observation(
                observation_id=_github_observation_id(
                    bundle.repo_id,
                    observation_kind,
                    str(source.external_id),
                    int(source.number) if source.number is not None else None,
                ),
                repo_id=bundle.repo_id,
                observation_kind=observation_kind,
                source_kind=str(source.source_kind),
                source_id=str(source.source_id),
                source_record_type=str(source.source_record_type),
                observed_at=getattr(source, "updated_at", None) or getattr(source, "created_at", None),
                author=getattr(source, "author_login", None),
                title=getattr(source, "title", None),
                text=body,
                metadata=dict(getattr(source, "metadata", {}) or {}),
                context_kind=thread_kind,
                context_id=thread_id,
                thread_kind=thread_kind,
                thread_id=thread_id,
                display_name=getattr(source, "title", None) or str(source.source_id),
            )
            if planned is not None:
                observations.append(planned)
        return tuple(observations)

    def plan_github_observations(
        self,
        *,
        bundle: Any,
        snapshot: GraphSnapshot | None = None,
    ) -> MutationPlan:
        return self.plan_observations(*self.build_github_observations(bundle=bundle), snapshot=snapshot)

    def plan_github_entity_observations(
        self,
        *,
        bundle: Any,
        snapshot: GraphSnapshot | None = None,
    ) -> MutationPlan:
        """Compatibility wrapper for older callers."""

        return self.plan_github_observations(bundle=bundle, snapshot=snapshot)

    def plan_observations(
        self,
        *planned_observations: PlannedObservation,
        snapshot: GraphSnapshot | None = None,
    ) -> MutationPlan:
        return merge_mutation_plans(
            *(
                self.plan_observation(
                    observation=item.observation,
                    source=item.source,
                    about_links=item.about_links,
                    snapshot=snapshot,
                )
                for item in planned_observations
            )
        )

    def plan_observation(
        self,
        *,
        observation: Observation,
        source: ObservationSourceRef,
        about_links: tuple[ObservationLink, ...] = (),
        snapshot: GraphSnapshot | None = None,
    ) -> MutationPlan:
        source_nodes = ()
        if source.ensure_source_node:
            source_nodes = (
                NodeRecord(
                    record_id=source.source_id,
                    record_type=source.source_record_type,
                    repo_id=observation.repo_id,
                    group_id=observation.repo_id,
                    labels=("BackendRecord", "ObservationSource"),
                    properties={
                        "repo_id": observation.repo_id,
                        "source_kind": source.source_kind,
                        "source_id": source.source_id,
                        "display_name": source.display_name,
                        "metadata_json": json.dumps(
                            source.source_metadata,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                ),
            )
        node = NodeRecord(
            record_id=observation.observation_id,
            record_type="observation",
            repo_id=observation.repo_id,
            group_id=observation.repo_id,
            labels=("BackendRecord", "Observation"),
            properties={
                "observation_id": observation.observation_id,
                "observation_kind": observation.observation_kind,
                "source_kind": observation.source_kind,
                "source_id": observation.source_id,
                "observed_at": observation.observed_at,
                "ingested_at": observation.ingested_at,
                "author": observation.author,
                "title": observation.title,
                "text": observation.text,
                "context_kind": observation.context_kind,
                "context_id": observation.context_id,
                "thread_kind": observation.thread_kind,
                "thread_id": observation.thread_id,
                "metadata_json": json.dumps(observation.metadata, sort_keys=True, separators=(",", ":")),
            },
        )
        source_edges = (
            EdgeRecord(
                record_id=f"from_source:{observation.observation_id}:{source.source_id}",
                record_type="from_source",
                repo_id=observation.repo_id,
                group_id=observation.repo_id,
                relationship_type="FROM_SOURCE",
                source_record_id=observation.observation_id,
                target_record_id=source.source_id,
                properties={
                    "source_kind": source.source_kind,
                    "source_record_type": source.source_record_type,
                },
            ),
        ) if source.source_id else ()
        about_edges = tuple(
            EdgeRecord(
                record_id=f"about:{observation.observation_id}:{link.target_record_id}",
                record_type="about",
                repo_id=observation.repo_id,
                group_id=observation.repo_id,
                relationship_type="ABOUT",
                source_record_id=observation.observation_id,
                target_record_id=link.target_record_id,
                properties=dict(link.properties),
            )
            for link in about_links
        )
        mention_edges = tuple(
            EdgeRecord(
                record_id=f"mentions:{observation.observation_id}:{target_record_id}",
                record_type="mentions",
                repo_id=observation.repo_id,
                group_id=observation.repo_id,
                relationship_type="MENTIONS",
                source_record_id=observation.observation_id,
                target_record_id=target_record_id,
                properties={},
            )
            for target_record_id in resolve_mention_record_ids(
                snapshot,
                collect_observation_grounding_hints(observation=observation),
            )
        )
        search_document = SearchDocumentRecord(
            record_kind=RecordKind.NODE,
            record_id=observation.observation_id,
            record_type="observation",
            repo_id=observation.repo_id,
            group_id=observation.repo_id,
            search_text=_observation_search_text(observation),
            properties={
                "observation_kind": observation.observation_kind,
                "source_kind": observation.source_kind,
                "source_id": observation.source_id,
                "author": observation.author,
                "context_kind": observation.context_kind,
                "context_id": observation.context_id,
                "thread_kind": observation.thread_kind,
                "thread_id": observation.thread_id,
                "observed_at": observation.observed_at,
                "ingested_at": observation.ingested_at,
            },
        )
        return MutationPlan(
            upsert_nodes=(*source_nodes, node),
            upsert_edges=(*source_edges, *about_edges, *mention_edges),
            upsert_search_documents=(search_document,),
        )


def _observation_search_text(observation: Observation) -> str:
    metadata_bits: list[str] = []
    for key in sorted(observation.metadata):
        value = observation.metadata[key]
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            metadata_bits.extend(_normalized_text(item) for item in value)
        else:
            metadata_bits.append(_normalized_text(value))
    return " ".join(
        part
        for part in (
            _normalized_text(observation.title),
            _normalized_text(observation.text),
            _normalized_text(observation.author),
            _normalized_text(observation.source_kind),
            *metadata_bits,
        )
        if part
    )


def _full_commit_message(subject: str | None, body: str | None) -> str:
    subject_text = _normalized_text(subject)
    body_text = _normalized_text(body)
    if subject_text and body_text:
        return f"{subject_text}\n\n{body_text}"
    return subject_text or body_text


def _normalized_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _observation_thread_kind(*, source_kind: str, observation_kind: str | ObservationKind) -> str | None:
    normalized_kind = observation_kind_value(observation_kind)
    if normalized_kind == ObservationKind.GIT_COMMIT_MESSAGE.value:
        return "commit"
    if source_kind == "pull_request":
        return "pull_request"
    if source_kind == "issue":
        return "issue"
    return None


def _observation_thread_id(*, source: Any) -> str | None:
    if getattr(source, "number", None) is not None:
        return str(source.number)
    source_id = str(getattr(source, "source_id", "") or "").strip()
    if source_id.startswith("commit:"):
        return source_id.rsplit(":", 1)[-1] or None
    return None


def _github_observation_id(
    repo_id: str,
    observation_kind: str | ObservationKind,
    external_id: str,
    number: int | None,
) -> str:
    normalized_kind = observation_kind_value(observation_kind)
    if normalized_kind == ObservationKind.GITHUB_PR_BODY.value:
        if number is None:
            raise ValueError("github_pr_body observations require a pull request number")
        return github_pr_body_observation_id(repo_id, number)
    if normalized_kind == ObservationKind.GITHUB_ISSUE_BODY.value:
        if number is None:
            raise ValueError("github_issue_body observations require an issue number")
        return github_issue_body_observation_id(repo_id, number)
    if normalized_kind == ObservationKind.GITHUB_ISSUE_CLOSED.value:
        if number is None:
            raise ValueError("github_issue_closed observations require an issue number")
        return github_issue_closed_observation_id(repo_id, number)
    if normalized_kind == ObservationKind.GITHUB_PR_REVIEW.value:
        return github_pr_review_observation_id(repo_id, external_id)
    if normalized_kind == ObservationKind.GITHUB_PR_REVIEW_COMMENT.value:
        return github_pr_review_comment_observation_id(repo_id, external_id)
    if normalized_kind == ObservationKind.GITHUB_ISSUE_COMMENT.value:
        return github_issue_comment_observation_id(repo_id, external_id)
    raise ValueError(f"unsupported GitHub observation kind: {normalized_kind}")
