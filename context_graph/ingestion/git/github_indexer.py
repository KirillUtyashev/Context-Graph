from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Mapping, Sequence

from context_graph.models import GraphSnapshot
from context_graph.provenance import (
    EvidenceExtractionService,
    EvidenceMutationPlanner,
    ObservationKind,
    ObservationMutationPlanner,
)
from context_graph.ingestion.git.github_client import GitHubApiClient, GitHubRepoRef, collect_unique_numbers
from context_graph.storage.backend import (
    BackendDriver,
    BackendSyncBridge,
    merge_mutation_plans,
    MutationPlan,
)

from .structural_planner import GitHubStructuralPlanner


@dataclass(frozen=True, slots=True)
class GitHubEntity:
    node_id: str
    kind: str
    repo_id: str
    github_repo: str
    external_id: str
    number: int | None
    url: str | None
    title: str | None
    state: str | None
    author_login: str | None
    created_at: str | None
    updated_at: str | None
    metadata: Mapping[str, Any]
    body: str | None = None


@dataclass(frozen=True, slots=True)
class GitHubProjectLink:
    target_node_id: str
    relation_type: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class GitHubCommitLink:
    commit_sha: str
    target_node_id: str
    relation_type: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class GitHubEntityLink:
    source_node_id: str
    target_node_id: str
    relation_type: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class GitHubObservationSource:
    observation_kind: ObservationKind
    source_kind: str
    source_id: str
    source_record_type: str
    external_id: str
    number: int | None
    url: str | None
    title: str | None
    author_login: str | None
    created_at: str | None
    updated_at: str | None
    text: str | None
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class GitHubProvenanceBundle:
    repo_id: str
    github_repo: str
    entities: tuple[GitHubEntity, ...]
    project_links: tuple[GitHubProjectLink, ...]
    commit_links: tuple[GitHubCommitLink, ...]
    entity_links: tuple[GitHubEntityLink, ...]
    observation_sources: tuple[GitHubObservationSource, ...] = ()


@dataclass(frozen=True, slots=True)
class GitHubSyncResult:
    repo_ref: GitHubRepoRef
    skipped: bool
    skip_reason: str | None
    error: str | None
    operations: MutationPlan
    operations_applied: int
    entity_counts: Mapping[str, int]
    pull_request_numbers: tuple[int, ...]
    issue_numbers: tuple[int, ...]
    observation_operations: MutationPlan = field(default_factory=MutationPlan)
    evidence_operations: MutationPlan = field(default_factory=MutationPlan)


class GitHubIndexer:
    def __init__(
        self,
        *,
        github_api: GitHubApiClient,
        backend_driver: BackendDriver | None,
        planner: GitHubStructuralPlanner | None = None,
        observation_planner: ObservationMutationPlanner | None = None,
        llm_client: Any | None = None,
        evidence_service: EvidenceExtractionService | None = None,
        evidence_planner: EvidenceMutationPlanner | None = None,
    ) -> None:
        self.github_api = github_api
        self.backend_driver = backend_driver
        self.planner = planner or GitHubStructuralPlanner()
        self.observation_planner = observation_planner or ObservationMutationPlanner()
        self.evidence_service = evidence_service or (
            EvidenceExtractionService(llm_client=llm_client) if llm_client is not None else None
        )
        self.evidence_planner = evidence_planner or EvidenceMutationPlanner()

    def sync_for_commits(
        self,
        *,
        repo_id: str,
        repo_ref: GitHubRepoRef,
        commit_shas: Sequence[str],
        snapshot: GraphSnapshot | None = None,
        execute: bool = False,
    ) -> GitHubSyncResult:
        if not commit_shas:
            return GitHubSyncResult(
                repo_ref=repo_ref,
                skipped=True,
                skip_reason="no commits indexed in this run",
                error=None,
                operations=MutationPlan(),
                operations_applied=0,
                entity_counts={},
                pull_request_numbers=(),
                issue_numbers=(),
            )

        bundle = self._build_bundle(
            repo_id=repo_id,
            repo_ref=repo_ref,
            commit_shas=commit_shas,
        )
        structural_operations = self.planner.plan_bundle(bundle)
        planned_observations = self.observation_planner.build_github_observations(bundle=bundle)
        observation_operations = self.observation_planner.plan_observations(*planned_observations)
        evidence_operations = self._plan_evidence_operations(
            planned_observations=planned_observations,
            snapshot=snapshot,
        )
        operations = merge_mutation_plans(
            structural_operations,
            observation_operations,
            evidence_operations,
        )
        operations_applied = 0
        if execute:
            if self.backend_driver is None:
                raise RuntimeError("GitHub structural sync execution requires a configured backend driver")
            bridge = BackendSyncBridge(self.backend_driver)
            operations_applied = bridge.apply(operations).operations_applied
        counts: dict[str, int] = {}
        pull_requests: set[int] = set()
        issues: set[int] = set()
        for entity in bundle.entities:
            counts[entity.kind] = counts.get(entity.kind, 0) + 1
            if entity.kind == "PullRequest" and entity.number is not None:
                pull_requests.add(entity.number)
            if entity.kind == "Issue" and entity.number is not None:
                issues.add(entity.number)
        return GitHubSyncResult(
            repo_ref=repo_ref,
            skipped=False,
            skip_reason=None,
            error=None,
            operations=operations,
            operations_applied=operations_applied,
            entity_counts=counts,
            pull_request_numbers=tuple(sorted(pull_requests)),
            issue_numbers=tuple(sorted(issues)),
            observation_operations=observation_operations,
            evidence_operations=evidence_operations,
        )

    def _plan_evidence_operations(
        self,
        *,
        planned_observations: Sequence[Any],
        snapshot: GraphSnapshot | None,
    ) -> MutationPlan:
        if self.evidence_service is None:
            return MutationPlan()

        plans: list[MutationPlan] = []
        for planned in planned_observations:
            try:
                extraction = self.evidence_service.extract(
                    planned.observation,
                    source_context={
                        "source_kind": planned.source.source_kind,
                        "source_record_type": planned.source.source_record_type,
                        "source_id": planned.source.source_id,
                        "source_display_name": planned.source.display_name,
                        "observation_metadata": dict(planned.observation.metadata),
                    },
                )
            except Exception:
                continue
            plans.append(
                self.evidence_planner.plan_evidence_records(
                    observation=planned.observation,
                    extraction=extraction,
                    snapshot=snapshot,
                )
            )
        return merge_mutation_plans(*plans)

    def _build_bundle(
        self,
        *,
        repo_id: str,
        repo_ref: GitHubRepoRef,
        commit_shas: Sequence[str],
    ) -> GitHubProvenanceBundle:
        owner = repo_ref.owner
        repo_name = repo_ref.name
        commit_to_prs: dict[str, tuple[int, ...]] = {}
        pr_payloads: dict[int, dict[str, Any]] = {}
        pr_to_issues: dict[int, tuple[int, ...]] = {}
        linked_issues: set[int] = set()

        for commit_sha in commit_shas:
            commit_prs = self.github_api.list_commit_pull_requests(owner, repo_name, commit_sha)
            numbers = collect_unique_numbers(commit_prs)
            commit_to_prs[commit_sha] = numbers
            for number in numbers:
                if number in pr_payloads:
                    continue
                pr = self.github_api.get_pull_request(owner, repo_name, number)
                pr_payloads[number] = pr
                linked_numbers = self.github_api.list_pull_request_linked_issue_numbers(
                    owner,
                    repo_name,
                    number,
                )
                pr_to_issues[number] = linked_numbers
                linked_issues.update(linked_numbers)

        issue_payloads: dict[int, dict[str, Any]] = {}
        for number in sorted(linked_issues):
            issue_payloads[number] = self.github_api.get_issue(owner, repo_name, number)

        entities: dict[str, GitHubEntity] = {}
        project_links: dict[tuple[str, str], GitHubProjectLink] = {}
        commit_links: dict[tuple[str, str, str], GitHubCommitLink] = {}
        entity_links: dict[tuple[str, str, str], GitHubEntityLink] = {}
        observation_sources: dict[str, GitHubObservationSource] = {}

        for number, pr in pr_payloads.items():
            pr_entity = _build_pull_request_entity(repo_id=repo_id, repo_ref=repo_ref, payload=pr)
            entities[pr_entity.node_id] = pr_entity
            project_links[("HAS_PULL_REQUEST", pr_entity.node_id)] = GitHubProjectLink(
                target_node_id=pr_entity.node_id,
                relation_type="HAS_PULL_REQUEST",
                metadata={"number": number},
            )

            for issue_number in pr_to_issues[number]:
                issue_node_id = _issue_node_id(repo_id, issue_number)
                entity_links[("REFERENCES_ISSUE", pr_entity.node_id, issue_node_id)] = GitHubEntityLink(
                    source_node_id=pr_entity.node_id,
                    target_node_id=issue_node_id,
                    relation_type="REFERENCES_ISSUE",
                    metadata={"issue_number": issue_number},
                )
            _maybe_add_observation_source(
                observation_sources,
                _build_pull_request_body_observation_source(
                    repo_id=repo_id,
                    repo_ref=repo_ref,
                    payload=pr,
                ),
            )
            for review in self.github_api.list_pull_request_reviews(owner, repo_name, number):
                _maybe_add_observation_source(
                    observation_sources,
                    _build_review_observation_source(
                        repo_id=repo_id,
                        repo_ref=repo_ref,
                        pull_request_payload=pr,
                        payload=review,
                    ),
                )
            for comment in self.github_api.list_pull_request_review_comments(owner, repo_name, number):
                _maybe_add_observation_source(
                    observation_sources,
                    _build_review_comment_observation_source(
                        repo_id=repo_id,
                        repo_ref=repo_ref,
                        pull_request_payload=pr,
                        payload=comment,
                    ),
                )

        for number, issue in issue_payloads.items():
            issue_entity = _build_issue_entity(repo_id=repo_id, repo_ref=repo_ref, payload=issue)
            entities[issue_entity.node_id] = issue_entity
            project_links[("HAS_ISSUE", issue_entity.node_id)] = GitHubProjectLink(
                target_node_id=issue_entity.node_id,
                relation_type="HAS_ISSUE",
                metadata={"number": number},
            )
            _maybe_add_observation_source(
                observation_sources,
                _build_issue_body_observation_source(
                    repo_id=repo_id,
                    repo_ref=repo_ref,
                    payload=issue,
                ),
            )
            for comment in self.github_api.list_issue_comments(owner, repo_name, number):
                _maybe_add_observation_source(
                    observation_sources,
                    _build_issue_comment_observation_source(
                        repo_id=repo_id,
                        repo_ref=repo_ref,
                        issue_payload=issue,
                        payload=comment,
                    ),
                )

        for commit_sha, pr_numbers in commit_to_prs.items():
            for pr_number in pr_numbers:
                pr_node_id = _pull_request_node_id(repo_id, pr_number)
                commit_links[("ASSOCIATED_WITH_PR", commit_sha, pr_node_id)] = GitHubCommitLink(
                    commit_sha=commit_sha,
                    target_node_id=pr_node_id,
                    relation_type="ASSOCIATED_WITH_PR",
                    metadata={"pull_request_number": pr_number},
                )
                for issue_number in pr_to_issues.get(pr_number, ()):
                    issue_node_id = _issue_node_id(repo_id, issue_number)
                    commit_links[("REFERENCES_ISSUE", commit_sha, issue_node_id)] = GitHubCommitLink(
                        commit_sha=commit_sha,
                        target_node_id=issue_node_id,
                        relation_type="REFERENCES_ISSUE",
                        metadata={"issue_number": issue_number},
                    )

        return GitHubProvenanceBundle(
            repo_id=repo_id,
            github_repo=repo_ref.canonical_repo,
            entities=tuple(sorted(entities.values(), key=lambda item: item.node_id)),
            project_links=tuple(sorted(project_links.values(), key=lambda item: (item.relation_type, item.target_node_id))),
            commit_links=tuple(sorted(commit_links.values(), key=lambda item: (item.relation_type, item.commit_sha, item.target_node_id))),
            entity_links=tuple(sorted(entity_links.values(), key=lambda item: (item.relation_type, item.source_node_id, item.target_node_id))),
            observation_sources=tuple(sorted(observation_sources.values(), key=lambda item: (item.observation_kind.value, item.external_id))),
        )


def _build_pull_request_entity(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    payload: Mapping[str, Any],
) -> GitHubEntity:
    number = int(payload["number"])
    return GitHubEntity(
        node_id=_pull_request_node_id(repo_id, number),
        kind="PullRequest",
        repo_id=repo_id,
        github_repo=repo_ref.canonical_repo,
        external_id=str(payload.get("id", number)),
        number=number,
        url=payload.get("html_url"),
        title=payload.get("title"),
        state=payload.get("state"),
        author_login=_author_login(payload),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
        metadata={
            "merged_at": payload.get("merged_at"),
            "merge_commit_sha": payload.get("merge_commit_sha"),
            "base_ref": payload.get("base", {}).get("ref"),
            "head_ref": payload.get("head", {}).get("ref"),
            "head_sha": payload.get("head", {}).get("sha"),
        },
        body=payload.get("body"),
    )


def _build_pull_request_body_observation_source(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    payload: Mapping[str, Any],
) -> GitHubObservationSource | None:
    number = int(payload["number"])
    body = payload.get("body")
    if not _has_text(body):
        return None
    return GitHubObservationSource(
        observation_kind=ObservationKind.GITHUB_PR_BODY,
        source_kind="pull_request",
        source_id=_pull_request_node_id(repo_id, number),
        source_record_type="github_pull_request",
        external_id=str(payload.get("id", number)),
        number=number,
        url=payload.get("html_url"),
        title=payload.get("title"),
        author_login=_author_login(payload),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
        text=body,
        metadata={
            "github_repo": repo_ref.canonical_repo,
            "pull_request_number": number,
            "url": payload.get("html_url"),
            "state": payload.get("state"),
        },
    )


def _build_issue_entity(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    payload: Mapping[str, Any],
) -> GitHubEntity:
    number = int(payload["number"])
    return GitHubEntity(
        node_id=_issue_node_id(repo_id, number),
        kind="Issue",
        repo_id=repo_id,
        github_repo=repo_ref.canonical_repo,
        external_id=str(payload.get("id", number)),
        number=number,
        url=payload.get("html_url"),
        title=payload.get("title"),
        state=payload.get("state"),
        author_login=_author_login(payload),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
        metadata={
            "closed_at": payload.get("closed_at"),
        },
        body=payload.get("body"),
    )


def _build_issue_body_observation_source(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    payload: Mapping[str, Any],
) -> GitHubObservationSource | None:
    number = int(payload["number"])
    body = payload.get("body")
    if not _has_text(body):
        return None
    return GitHubObservationSource(
        observation_kind=ObservationKind.GITHUB_ISSUE_BODY,
        source_kind="issue",
        source_id=_issue_node_id(repo_id, number),
        source_record_type="github_issue",
        external_id=str(payload.get("id", number)),
        number=number,
        url=payload.get("html_url"),
        title=payload.get("title"),
        author_login=_author_login(payload),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
        text=body,
        metadata={
            "github_repo": repo_ref.canonical_repo,
            "issue_number": number,
            "url": payload.get("html_url"),
            "state": payload.get("state"),
        },
    )


def _build_review_observation_source(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    pull_request_payload: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> GitHubObservationSource | None:
    review_id = int(payload["id"])
    pull_number = int(pull_request_payload["number"])
    body = payload.get("body")
    if not _has_text(body):
        return None
    pr_title = pull_request_payload.get("title")
    title = f"Review on PR #{pull_number}"
    if pr_title:
        title = f"{title}: {pr_title}"
    return GitHubObservationSource(
        observation_kind=ObservationKind.GITHUB_PR_REVIEW,
        source_kind="pull_request",
        source_id=_pull_request_node_id(repo_id, pull_number),
        source_record_type="github_pull_request",
        external_id=str(review_id),
        number=pull_number,
        url=payload.get("html_url"),
        title=title,
        author_login=_author_login(payload),
        created_at=payload.get("submitted_at") or payload.get("created_at"),
        updated_at=payload.get("submitted_at") or payload.get("updated_at"),
        text=body,
        metadata={
            "github_repo": repo_ref.canonical_repo,
            "pull_request_number": pull_number,
            "review_id": review_id,
            "review_state": payload.get("state"),
            "commit_id": payload.get("commit_id"),
            "url": payload.get("html_url"),
        },
    )


def _build_review_comment_observation_source(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    pull_request_payload: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> GitHubObservationSource | None:
    comment_id = int(payload["id"])
    pull_number = int(pull_request_payload["number"])
    body = payload.get("body")
    if not _has_text(body):
        return None
    pr_title = pull_request_payload.get("title")
    title = f"Review comment on PR #{pull_number}"
    if pr_title:
        title = f"{title}: {pr_title}"
    return GitHubObservationSource(
        observation_kind=ObservationKind.GITHUB_PR_REVIEW_COMMENT,
        source_kind="pull_request",
        source_id=_pull_request_node_id(repo_id, pull_number),
        source_record_type="github_pull_request",
        external_id=str(comment_id),
        number=pull_number,
        url=payload.get("html_url"),
        title=title,
        author_login=_author_login(payload),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
        text=body,
        metadata={
            "github_repo": repo_ref.canonical_repo,
            "pull_request_number": pull_number,
            "comment_id": comment_id,
            "path": payload.get("path"),
            "commit_id": payload.get("commit_id"),
            "pull_request_review_id": payload.get("pull_request_review_id"),
            "url": payload.get("html_url"),
        },
    )


def _build_issue_comment_observation_source(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    issue_payload: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> GitHubObservationSource | None:
    comment_id = int(payload["id"])
    issue_number = int(issue_payload["number"])
    body = payload.get("body")
    if not _has_text(body):
        return None
    issue_title = issue_payload.get("title")
    title = f"Comment on issue #{issue_number}"
    if issue_title:
        title = f"{title}: {issue_title}"
    return GitHubObservationSource(
        observation_kind=ObservationKind.GITHUB_ISSUE_COMMENT,
        source_kind="issue",
        source_id=_issue_node_id(repo_id, issue_number),
        source_record_type="github_issue",
        external_id=str(comment_id),
        number=issue_number,
        url=payload.get("html_url"),
        title=title,
        author_login=_author_login(payload),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
        text=body,
        metadata={
            "github_repo": repo_ref.canonical_repo,
            "issue_number": issue_number,
            "comment_id": comment_id,
            "url": payload.get("html_url"),
        },
    )


def _build_review_entity(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    pull_number: int,
    payload: Mapping[str, Any],
) -> GitHubEntity:
    review_id = int(payload["id"])
    return GitHubEntity(
        node_id=f"github:review:{repo_id}:{review_id}",
        kind="PullRequestReview",
        repo_id=repo_id,
        github_repo=repo_ref.canonical_repo,
        external_id=str(review_id),
        number=pull_number,
        url=payload.get("html_url"),
        title=None,
        state=payload.get("state"),
        author_login=_author_login(payload),
        created_at=payload.get("submitted_at") or payload.get("created_at"),
        updated_at=payload.get("submitted_at") or payload.get("updated_at"),
        metadata={
            "commit_id": payload.get("commit_id"),
        },
    )


def _build_review_comment_entity(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    pull_number: int,
    payload: Mapping[str, Any],
) -> GitHubEntity:
    comment_id = int(payload["id"])
    return GitHubEntity(
        node_id=f"github:review-comment:{repo_id}:{comment_id}",
        kind="PullRequestReviewComment",
        repo_id=repo_id,
        github_repo=repo_ref.canonical_repo,
        external_id=str(comment_id),
        number=pull_number,
        url=payload.get("html_url"),
        title=None,
        state=None,
        author_login=_author_login(payload),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
        metadata={
            "path": payload.get("path"),
            "commit_id": payload.get("commit_id"),
            "pull_request_review_id": payload.get("pull_request_review_id"),
        },
    )


def _build_issue_comment_entity(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    issue_number: int,
    payload: Mapping[str, Any],
) -> GitHubEntity:
    comment_id = int(payload["id"])
    return GitHubEntity(
        node_id=f"github:issue-comment:{repo_id}:{comment_id}",
        kind="IssueComment",
        repo_id=repo_id,
        github_repo=repo_ref.canonical_repo,
        external_id=str(comment_id),
        number=issue_number,
        url=payload.get("html_url"),
        title=None,
        state=None,
        author_login=_author_login(payload),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
        metadata={},
    )


def _build_timeline_event_entity(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    parent_kind: str,
    parent_number: int,
    payload: Mapping[str, Any],
) -> GitHubEntity:
    raw_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    raw_id = payload.get("id")
    stable_token = raw_id if raw_id is not None else hashlib.sha1(raw_json.encode("utf-8")).hexdigest()
    event_type = str(payload.get("event") or "timeline")
    return GitHubEntity(
        node_id=f"github:timeline-event:{repo_id}:{parent_kind}:{stable_token}",
        kind="GitHubTimelineEvent",
        repo_id=repo_id,
        github_repo=repo_ref.canonical_repo,
        external_id=str(stable_token),
        number=parent_number,
        url=None,
        title=event_type,
        state=None,
        author_login=_author_login(payload),
        created_at=payload.get("created_at"),
        updated_at=payload.get("created_at"),
        metadata={"event": event_type},
    )

def _pull_request_node_id(repo_id: str, number: int) -> str:
    return f"github:pr:{repo_id}:{number}"


def _issue_node_id(repo_id: str, number: int) -> str:
    return f"github:issue:{repo_id}:{number}"


def _author_login(payload: Mapping[str, Any]) -> str | None:
    user = payload.get("user")
    if isinstance(user, Mapping):
        login = user.get("login")
        if isinstance(login, str):
            return login
    return None


def _maybe_add_observation_source(
    sources: dict[str, GitHubObservationSource],
    source: GitHubObservationSource | None,
) -> None:
    if source is None:
        return
    sources[f"{source.observation_kind.value}:{source.external_id}"] = source


def _has_text(value: object) -> bool:
    return bool(" ".join(str(value).split()).strip()) if value is not None else False
