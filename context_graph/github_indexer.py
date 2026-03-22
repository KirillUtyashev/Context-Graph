from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from .episode_processor import EpisodeBatchResult, EpisodeProcessor, EpisodeWorkItem
from .episodes import ContextNodeRef, EpisodeRecord
from .github_client import GitHubApiClient, GitHubRepoRef, collect_unique_numbers
from .graph_sinks import BaseCypherGraphSink, CypherStatement
from .models import utc_now_iso


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
    raw: Mapping[str, Any]
    metadata: Mapping[str, Any]


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
class GitHubProvenanceBundle:
    repo_id: str
    github_repo: str
    entities: tuple[GitHubEntity, ...]
    project_links: tuple[GitHubProjectLink, ...]
    commit_links: tuple[GitHubCommitLink, ...]
    entity_links: tuple[GitHubEntityLink, ...]
    episode_work_items: tuple[EpisodeWorkItem, ...]


@dataclass(frozen=True, slots=True)
class GitHubSyncResult:
    repo_ref: GitHubRepoRef
    skipped: bool
    skip_reason: str | None
    error: str | None
    statements: tuple[CypherStatement, ...]
    statements_executed: int
    entity_counts: Mapping[str, int]
    episode_count: int
    pull_request_numbers: tuple[int, ...]
    issue_numbers: tuple[int, ...]


class GitHubIndexer:
    def __init__(
        self,
        *,
        github_api: GitHubApiClient,
        graph_sink: BaseCypherGraphSink,
        episode_processor: EpisodeProcessor | None = None,
    ) -> None:
        self.github_api = github_api
        self.graph_sink = graph_sink
        self.episode_processor = episode_processor

    def sync_for_commits(
        self,
        *,
        repo_id: str,
        repo_ref: GitHubRepoRef,
        commit_shas: Sequence[str],
        execute: bool = False,
    ) -> GitHubSyncResult:
        if not commit_shas:
            return GitHubSyncResult(
                repo_ref=repo_ref,
                skipped=True,
                skip_reason="no commits indexed in this run",
                error=None,
                statements=(),
                statements_executed=0,
                entity_counts={},
                episode_count=0,
                pull_request_numbers=(),
                issue_numbers=(),
            )

        bundle = self._build_bundle(
            repo_id=repo_id,
            repo_ref=repo_ref,
            commit_shas=commit_shas,
        )
        structural_statements = tuple(
            self.graph_sink.plan_attach_github_provenance(
                bundle,
                observed_at=utc_now_iso(),
            )
        )
        structural_executed = self.graph_sink.execute(structural_statements) if execute and structural_statements else 0
        episode_batch = (
            self.episode_processor.apply_batch(bundle.episode_work_items, execute=execute)
            if self.episode_processor is not None and bundle.episode_work_items
            else EpisodeBatchResult(results=(), statements=(), statements_executed=0)
        )
        statements = tuple(structural_statements) + tuple(episode_batch.statements)
        executed = structural_executed + episode_batch.statements_executed
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
            statements=statements,
            statements_executed=executed,
            entity_counts=counts,
            episode_count=len(bundle.episode_work_items),
            pull_request_numbers=tuple(sorted(pull_requests)),
            issue_numbers=tuple(sorted(issues)),
        )

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
        pr_reviews: dict[int, list[dict[str, Any]]] = {}
        pr_review_comments: dict[int, list[dict[str, Any]]] = {}
        pr_issue_comments: dict[int, list[dict[str, Any]]] = {}
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
                pr_reviews[number] = self.github_api.list_pull_request_reviews(owner, repo_name, number)
                pr_review_comments[number] = self.github_api.list_pull_request_review_comments(owner, repo_name, number)
                pr_issue_comments[number] = self.github_api.list_issue_comments(owner, repo_name, number)
                linked_numbers = self.github_api.list_pull_request_linked_issue_numbers(
                    owner,
                    repo_name,
                    number,
                )
                pr_to_issues[number] = linked_numbers
                linked_issues.update(linked_numbers)

        issue_payloads: dict[int, dict[str, Any]] = {}
        issue_comments: dict[int, list[dict[str, Any]]] = {}
        for number in sorted(linked_issues):
            issue_payloads[number] = self.github_api.get_issue(owner, repo_name, number)
            issue_comments[number] = self.github_api.list_issue_comments(owner, repo_name, number)

        entities: dict[str, GitHubEntity] = {}
        project_links: dict[tuple[str, str], GitHubProjectLink] = {}
        commit_links: dict[tuple[str, str, str], GitHubCommitLink] = {}
        entity_links: dict[tuple[str, str, str], GitHubEntityLink] = {}
        episode_work_items: list[EpisodeWorkItem] = []

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

            pr_context_nodes = [
                _entity_ref(pr_entity),
                *[
                    ContextNodeRef(
                        node_id=_issue_node_id(repo_id, issue_number),
                        repo_id=repo_id,
                        kind="Issue",
                        name=f"Issue #{issue_number}",
                        aliases=(f"#{issue_number}",),
                    )
                    for issue_number in pr_to_issues[number]
                ],
            ]

            body_episode = _build_artifact_body_episode(
                repo_id=repo_id,
                artifact_node_id=pr_entity.node_id,
                source_type="github_pr_body",
                source_id=f"pr:{number}:body",
                author=pr_entity.author_login,
                created_at=pr_entity.created_at,
                updated_at=pr_entity.updated_at,
                title=pr_entity.title,
                body=pr_entity.metadata.get("body"),
                metadata={
                    "thread_id": f"pr:{number}",
                    "github_repo": repo_ref.canonical_repo,
                    "number": number,
                    "url": pr_entity.url,
                },
            )
            if body_episode is not None:
                episode_work_items.append(
                    EpisodeWorkItem(episode=body_episode, extra_nodes=tuple(pr_context_nodes))
                )

            for review in pr_reviews[number]:
                review_episode = _build_review_episode(
                    repo_id=repo_id,
                    repo_ref=repo_ref,
                    pull_number=number,
                    payload=review,
                )
                if review_episode is not None:
                    extra_nodes = list(pr_context_nodes)
                    commit_ref = _commit_ref(repo_id, review.get("commit_id"))
                    if commit_ref is not None:
                        extra_nodes.append(commit_ref)
                    episode_work_items.append(
                        EpisodeWorkItem(
                            episode=review_episode,
                            extra_nodes=tuple(extra_nodes),
                        )
                    )

            for comment in pr_review_comments[number]:
                review_comment_episode = _build_review_comment_episode(
                    repo_id=repo_id,
                    repo_ref=repo_ref,
                    pull_number=number,
                    payload=comment,
                )
                if review_comment_episode is not None:
                    extra_nodes = list(pr_context_nodes)
                    commit_ref = _commit_ref(repo_id, comment.get("commit_id"))
                    if commit_ref is not None:
                        extra_nodes.append(commit_ref)
                    episode_work_items.append(
                        EpisodeWorkItem(
                            episode=review_comment_episode,
                            extra_nodes=tuple(extra_nodes),
                        )
                    )

            for comment in pr_issue_comments[number]:
                pr_comment_episode = _build_issue_comment_episode(
                    repo_id=repo_id,
                    repo_ref=repo_ref,
                    parent_kind="PullRequest",
                    parent_node_id=pr_entity.node_id,
                    parent_number=number,
                    payload=comment,
                )
                if pr_comment_episode is not None:
                    episode_work_items.append(
                        EpisodeWorkItem(
                            episode=pr_comment_episode,
                            extra_nodes=tuple(pr_context_nodes),
                        )
                    )

        for number, issue in issue_payloads.items():
            issue_entity = _build_issue_entity(repo_id=repo_id, repo_ref=repo_ref, payload=issue)
            entities[issue_entity.node_id] = issue_entity
            project_links[("HAS_ISSUE", issue_entity.node_id)] = GitHubProjectLink(
                target_node_id=issue_entity.node_id,
                relation_type="HAS_ISSUE",
                metadata={"number": number},
            )

            issue_context_nodes = [_entity_ref(issue_entity)]
            issue_body_episode = _build_artifact_body_episode(
                repo_id=repo_id,
                artifact_node_id=issue_entity.node_id,
                source_type="github_issue_body",
                source_id=f"issue:{number}:body",
                author=issue_entity.author_login,
                created_at=issue_entity.created_at,
                updated_at=issue_entity.updated_at,
                title=issue_entity.title,
                body=issue_entity.metadata.get("body"),
                metadata={
                    "thread_id": f"issue:{number}",
                    "github_repo": repo_ref.canonical_repo,
                    "number": number,
                    "url": issue_entity.url,
                },
            )
            if issue_body_episode is not None:
                episode_work_items.append(
                    EpisodeWorkItem(episode=issue_body_episode, extra_nodes=tuple(issue_context_nodes))
                )

            for comment in issue_comments[number]:
                issue_comment_episode = _build_issue_comment_episode(
                    repo_id=repo_id,
                    repo_ref=repo_ref,
                    parent_kind="Issue",
                    parent_node_id=issue_entity.node_id,
                    parent_number=number,
                    payload=comment,
                )
                if issue_comment_episode is not None:
                    episode_work_items.append(
                        EpisodeWorkItem(
                            episode=issue_comment_episode,
                            extra_nodes=tuple(issue_context_nodes),
                        )
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
            episode_work_items=tuple(
                sorted(
                    episode_work_items,
                    key=lambda item: (
                        item.episode.created_at or "",
                        item.episode.source_type,
                        item.episode.episode_id,
                    ),
                )
            ),
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
        raw=dict(payload),
        metadata={
            "merged_at": payload.get("merged_at"),
            "merge_commit_sha": payload.get("merge_commit_sha"),
            "base_ref": payload.get("base", {}).get("ref"),
            "head_ref": payload.get("head", {}).get("ref"),
            "head_sha": payload.get("head", {}).get("sha"),
            "body": payload.get("body"),
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
        raw=dict(payload),
        metadata={
            "closed_at": payload.get("closed_at"),
            "body": payload.get("body"),
        },
    )


def _entity_ref(entity: GitHubEntity) -> ContextNodeRef:
    aliases = []
    if entity.number is not None:
        aliases.append(f"#{entity.number}")
    if entity.title:
        aliases.append(entity.title)
    return ContextNodeRef(
        node_id=entity.node_id,
        repo_id=entity.repo_id,
        kind=entity.kind,
        name=entity.title or f"{entity.kind} {entity.number or entity.external_id}",
        aliases=tuple(aliases),
        metadata=entity.metadata,
    )


def _commit_ref(repo_id: str, commit_sha: Any) -> ContextNodeRef | None:
    if not isinstance(commit_sha, str) or not commit_sha:
        return None
    return ContextNodeRef(
        node_id=f"commit:{repo_id}:{commit_sha}",
        repo_id=repo_id,
        kind="Commit",
        name=commit_sha[:12],
        aliases=(commit_sha,),
    )


def _build_artifact_body_episode(
    *,
    repo_id: str,
    artifact_node_id: str,
    source_type: str,
    source_id: str,
    author: str | None,
    created_at: str | None,
    updated_at: str | None,
    title: str | None,
    body: Any,
    metadata: Mapping[str, Any],
) -> EpisodeRecord | None:
    text = _compose_body_text(title=title, body=body)
    if text is None:
        return None
    created = str(created_at or updated_at or utc_now_iso())
    return EpisodeRecord(
        episode_id=source_id,
        repo_id=repo_id,
        source_type=source_type,
        source_id=source_id,
        author=author,
        created_at=created,
        updated_at=str(updated_at or created),
        text=text,
        metadata=dict(metadata),
        parent_node_id=artifact_node_id,
    )


def _build_review_episode(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    pull_number: int,
    payload: Mapping[str, Any],
) -> EpisodeRecord | None:
    body = payload.get("body")
    state = payload.get("state")
    if not body and not state:
        return None
    review_id = int(payload["id"])
    parts = []
    if state:
        parts.append(f"Review state: {state}")
    if isinstance(body, str) and body.strip():
        parts.append(body.strip())
    text = "\n\n".join(parts).strip()
    if not text:
        return None
    created = str(payload.get("submitted_at") or payload.get("created_at") or utc_now_iso())
    return EpisodeRecord(
        episode_id=f"github-review:{review_id}",
        repo_id=repo_id,
        source_type="github_pr_review",
        source_id=str(review_id),
        author=_author_login(payload),
        created_at=created,
        updated_at=str(payload.get("submitted_at") or created),
        text=text,
        metadata={
            "thread_id": f"pr:{pull_number}",
            "github_repo": repo_ref.canonical_repo,
            "pull_request_number": pull_number,
            "commit_id": payload.get("commit_id"),
            "state": state,
        },
        parent_node_id=_pull_request_node_id(repo_id, pull_number),
    )


def _build_review_comment_episode(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    pull_number: int,
    payload: Mapping[str, Any],
) -> EpisodeRecord | None:
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    comment_id = int(payload["id"])
    path = payload.get("path")
    prefix = f"Path: {path}\n\n" if isinstance(path, str) and path else ""
    created = str(payload.get("created_at") or utc_now_iso())
    return EpisodeRecord(
        episode_id=f"github-review-comment:{comment_id}",
        repo_id=repo_id,
        source_type="github_pr_review_comment",
        source_id=str(comment_id),
        author=_author_login(payload),
        created_at=created,
        updated_at=str(payload.get("updated_at") or created),
        text=f"{prefix}{body.strip()}",
        metadata={
            "thread_id": f"pr:{pull_number}",
            "github_repo": repo_ref.canonical_repo,
            "pull_request_number": pull_number,
            "path": path,
            "commit_id": payload.get("commit_id"),
            "url": payload.get("html_url"),
        },
        parent_node_id=_pull_request_node_id(repo_id, pull_number),
    )


def _build_issue_comment_episode(
    *,
    repo_id: str,
    repo_ref: GitHubRepoRef,
    parent_kind: str,
    parent_node_id: str,
    parent_number: int,
    payload: Mapping[str, Any],
) -> EpisodeRecord | None:
    body = payload.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    comment_id = int(payload["id"])
    created = str(payload.get("created_at") or utc_now_iso())
    return EpisodeRecord(
        episode_id=f"github-issue-comment:{comment_id}",
        repo_id=repo_id,
        source_type=(
            "github_pr_issue_comment"
            if parent_kind == "PullRequest"
            else "github_issue_comment"
        ),
        source_id=str(comment_id),
        author=_author_login(payload),
        created_at=created,
        updated_at=str(payload.get("updated_at") or created),
        text=body.strip(),
        metadata={
            "thread_id": f"{parent_kind.lower()}:{parent_number}",
            "github_repo": repo_ref.canonical_repo,
            "parent_kind": parent_kind,
            "parent_number": parent_number,
            "url": payload.get("html_url"),
        },
        parent_node_id=parent_node_id,
    )


def _compose_body_text(*, title: str | None, body: Any) -> str | None:
    parts: list[str] = []
    if isinstance(title, str) and title.strip():
        parts.append(title.strip())
    if isinstance(body, str) and body.strip():
        parts.append(body.strip())
    if not parts:
        return None
    return "\n\n".join(parts)


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
        created_at=payload.get("submitted_at") or payload.get("submitted_at"),
        updated_at=payload.get("submitted_at") or payload.get("submitted_at"),
        raw=dict(payload),
        metadata={
            "body": payload.get("body"),
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
        raw=dict(payload),
        metadata={
            "body": payload.get("body"),
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
        raw=dict(payload),
        metadata={"body": payload.get("body")},
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
        raw=dict(payload),
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
