from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .git_metadata import GitMetadata


_GITHUB_HTTP_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?(?:/.*)?$"
)
_GITHUB_SSH_RE = re.compile(
    r"^(?:git@|ssh://git@)github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
_GITHUB_SLUG_RE = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)$")


@dataclass(frozen=True, slots=True)
class GitHubRepoRef:
    owner: str
    name: str
    canonical_repo: str
    clone_url: str
    html_url: str
    default_branch: str | None
    source: str


class GitHubApiError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubApiClient:
    def __init__(
        self,
        *,
        token: str | None,
        api_base_url: str = "https://api.github.com",
        user_agent: str = "context-graph/0.1",
    ) -> None:
        self.token = token
        self.api_base_url = api_base_url.rstrip("/")
        self.user_agent = user_agent

    def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        payload = self._request_json("GET", f"/repos/{owner}/{repo}")
        if not isinstance(payload, dict):
            raise RuntimeError("unexpected GitHub repository response")
        return payload

    def list_commit_pull_requests(self, owner: str, repo: str, sha: str) -> list[dict[str, Any]]:
        if self.token:
            return self._list_commit_pull_requests_graphql(owner, repo, sha)
        return self._paginate(f"/repos/{owner}/{repo}/commits/{sha}/pulls")

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        payload = self._request_json("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        if not isinstance(payload, dict):
            raise RuntimeError("unexpected GitHub pull request response")
        return payload

    def list_pull_request_reviews(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{owner}/{repo}/pulls/{number}/reviews")

    def list_pull_request_review_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{owner}/{repo}/pulls/{number}/comments")

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{owner}/{repo}/issues/{number}/comments")

    def list_issue_timeline(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{owner}/{repo}/issues/{number}/timeline")

    def list_pull_request_linked_issue_numbers(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> tuple[int, ...]:
        payload = self._graphql(
            query=_PULL_REQUEST_LINKED_ISSUES_QUERY,
            variables={
                "owner": owner,
                "repo": repo,
                "number": number,
            },
        )
        repository = payload.get("repository")
        if not isinstance(repository, Mapping):
            return ()
        pull_request = repository.get("pullRequest")
        if not isinstance(pull_request, Mapping):
            return ()

        repo_slug = f"{owner}/{repo}".lower()
        linked_issue_numbers: set[int] = set()
        active_connected_numbers: set[int] = set()

        closing_refs = pull_request.get("closingIssuesReferences", {})
        if isinstance(closing_refs, Mapping):
            for issue in _graphql_connection_nodes(closing_refs):
                number_value = _graphql_issue_number(issue, repo_slug)
                if number_value is not None:
                    linked_issue_numbers.add(number_value)

        timeline_items = pull_request.get("timelineItems", {})
        if isinstance(timeline_items, Mapping):
            for item in _graphql_connection_nodes(timeline_items):
                event_type = str(item.get("__typename") or "")
                if event_type == "ConnectedEvent":
                    active_connected_numbers.update(
                        _graphql_issue_numbers_from_event(item, repo_slug, fields=("source", "subject"))
                    )
                elif event_type == "DisconnectedEvent":
                    active_connected_numbers.difference_update(
                        _graphql_issue_numbers_from_event(item, repo_slug, fields=("source", "subject"))
                    )
                elif event_type == "CrossReferencedEvent":
                    linked_issue_numbers.update(
                        _graphql_issue_numbers_from_event(item, repo_slug, fields=("source", "target"))
                    )

        linked_issue_numbers.update(active_connected_numbers)
        return tuple(sorted(linked_issue_numbers))

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        payload = self._request_json("GET", f"/repos/{owner}/{repo}/issues/{number}")
        if not isinstance(payload, dict):
            raise RuntimeError("unexpected GitHub issue response")
        return payload

    def _paginate(self, path: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self._request_json(
                "GET",
                path,
                params={"per_page": 100, "page": page},
            )
            if not isinstance(payload, list):
                raise RuntimeError(f"unexpected paginated GitHub response for {path}")
            if not payload:
                return rows
            rows.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < 100:
                return rows
            page += 1

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        accept: str | None = None,
    ) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.api_base_url}{path}{query}"
        payload = json.dumps(dict(body)).encode("utf-8") if body is not None else None
        request = Request(url, data=payload, method=method)
        request.add_header("Accept", accept or "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", self.user_agent)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urlopen(request) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            message = detail or f"GitHub API request failed with status {exc.code}"
            raise GitHubApiError(exc.code, message) from exc
        except URLError as exc:
            raise RuntimeError(f"GitHub API request failed: {exc.reason}") from exc

        if not body:
            return None
        return json.loads(body)

    def _graphql(self, *, query: str, variables: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = self._request_json(
            "POST",
            "/graphql",
            body={
                "query": query,
                "variables": dict(variables),
            },
        )
        if not isinstance(payload, Mapping):
            raise RuntimeError("unexpected GitHub GraphQL response")
        errors = payload.get("errors")
        if isinstance(errors, Sequence) and errors:
            messages = [
                str(item.get("message"))
                for item in errors
                if isinstance(item, Mapping) and item.get("message")
            ]
            detail = "; ".join(messages) or "GitHub GraphQL request failed"
            raise RuntimeError(detail)
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise RuntimeError("GitHub GraphQL response missing data")
        return data

    def _list_commit_pull_requests_graphql(self, owner: str, repo: str, sha: str) -> list[dict[str, Any]]:
        payload = self._graphql(
            query=_COMMIT_ASSOCIATED_PULL_REQUESTS_QUERY,
            variables={
                "owner": owner,
                "repo": repo,
                "oid": sha,
            },
        )
        repository = payload.get("repository")
        if not isinstance(repository, Mapping):
            return []
        obj = repository.get("object")
        if not isinstance(obj, Mapping):
            return []
        associated = obj.get("associatedPullRequests")
        if not isinstance(associated, Mapping):
            return []
        rows: list[dict[str, Any]] = []
        for item in _graphql_connection_nodes(associated):
            number = item.get("number")
            if not isinstance(number, int):
                continue
            state = item.get("state")
            author = item.get("author")
            merge_commit = item.get("mergeCommit")
            rows.append(
                {
                    "id": item.get("id", number),
                    "number": number,
                    "html_url": item.get("url"),
                    "title": item.get("title"),
                    "state": str(state).lower() if isinstance(state, str) else state,
                    "user": {"login": author.get("login")} if isinstance(author, Mapping) else None,
                    "created_at": item.get("createdAt"),
                    "updated_at": item.get("updatedAt"),
                    "merged_at": item.get("mergedAt"),
                    "merge_commit_sha": merge_commit.get("oid") if isinstance(merge_commit, Mapping) else None,
                    "head": {
                        "ref": item.get("headRefName"),
                        "sha": item.get("headRefOid"),
                    },
                    "base": {
                        "ref": item.get("baseRefName"),
                    },
                    "body": item.get("body"),
                }
            )
        return rows


def _graphql_connection_nodes(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    nodes = payload.get("nodes")
    if not isinstance(nodes, Sequence):
        return []
    return [item for item in nodes if isinstance(item, Mapping)]


def _graphql_issue_numbers_from_event(
    payload: Mapping[str, Any],
    repo_slug: str,
    *,
    fields: Sequence[str],
) -> set[int]:
    values: set[int] = set()
    for field in fields:
        subject = payload.get(field)
        if not isinstance(subject, Mapping):
            continue
        number = _graphql_issue_number(subject, repo_slug)
        if number is not None:
            values.add(number)
    return values


def _graphql_issue_number(payload: Mapping[str, Any], repo_slug: str) -> int | None:
    if str(payload.get("__typename") or "") != "Issue":
        return None
    repository = payload.get("repository")
    if not isinstance(repository, Mapping):
        return None
    name_with_owner = repository.get("nameWithOwner")
    if not isinstance(name_with_owner, str) or name_with_owner.lower() != repo_slug:
        return None
    number = payload.get("number")
    if not isinstance(number, int):
        return None
    return number


_COMMIT_ASSOCIATED_PULL_REQUESTS_QUERY = """
query CommitAssociatedPullRequests($owner: String!, $repo: String!, $oid: GitObjectID!) {
  repository(owner: $owner, name: $repo) {
    object(oid: $oid) {
      __typename
      ... on Commit {
        associatedPullRequests(first: 20) {
          nodes {
            id
            number
            title
            url
            state
            body
            createdAt
            updatedAt
            mergedAt
            baseRefName
            headRefName
            headRefOid
            mergeCommit {
              oid
            }
            author {
              login
            }
          }
        }
      }
    }
  }
}
"""


_PULL_REQUEST_LINKED_ISSUES_QUERY = """
query PullRequestLinkedIssues($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 50) {
        nodes {
          __typename
          number
          repository {
            nameWithOwner
          }
        }
      }
      timelineItems(
        first: 100
        itemTypes: [CONNECTED_EVENT, DISCONNECTED_EVENT, CROSS_REFERENCED_EVENT]
      ) {
        nodes {
          __typename
          ... on ConnectedEvent {
            source {
              __typename
              ... on Issue {
                number
                repository {
                  nameWithOwner
                }
              }
              ... on PullRequest {
                number
                repository {
                  nameWithOwner
                }
              }
            }
            subject {
              __typename
              ... on Issue {
                number
                repository {
                  nameWithOwner
                }
              }
              ... on PullRequest {
                number
                repository {
                  nameWithOwner
                }
              }
            }
          }
          ... on DisconnectedEvent {
            source {
              __typename
              ... on Issue {
                number
                repository {
                  nameWithOwner
                }
              }
              ... on PullRequest {
                number
                repository {
                  nameWithOwner
                }
              }
            }
            subject {
              __typename
              ... on Issue {
                number
                repository {
                  nameWithOwner
                }
              }
              ... on PullRequest {
                number
                repository {
                  nameWithOwner
                }
              }
            }
          }
          ... on CrossReferencedEvent {
            source {
              __typename
              ... on Issue {
                number
                repository {
                  nameWithOwner
                }
              }
              ... on PullRequest {
                number
                repository {
                  nameWithOwner
                }
              }
            }
            target {
              __typename
              ... on Issue {
                number
                repository {
                  nameWithOwner
                }
              }
              ... on PullRequest {
                number
                repository {
                  nameWithOwner
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


class GitHubRepoResolver:
    def __init__(
        self,
        *,
        git_metadata: GitMetadata | None = None,
        github_api: GitHubApiClient | None = None,
    ) -> None:
        self.git_metadata = git_metadata or GitMetadata()
        self.github_api = github_api

    def resolve(
        self,
        *,
        github_url: str | None = None,
        github_repo: str | None = None,
        repo_path: str | None = None,
        require_github: bool = False,
    ) -> GitHubRepoRef | None:
        if github_repo:
            owner, repo = parse_github_repo_input(github_repo)
            return self._hydrate_repo_ref(owner, repo, source="github_repo")

        if github_url:
            owner, repo = parse_github_repo_input(github_url)
            return self._hydrate_repo_ref(owner, repo, source="github_url")

        if not repo_path:
            if require_github:
                raise RuntimeError("hosted indexing requires a GitHub repository URL or owner/repo slug")
            return None

        remotes = self.git_metadata.list_remotes(repo_path)
        github_remotes = {
            name: url
            for name, url in remotes.items()
            if is_github_remote(url)
        }
        if not github_remotes:
            if require_github:
                raise RuntimeError("repository does not have a GitHub remote and no explicit GitHub repo was provided")
            return None

        if "upstream" in github_remotes:
            owner, repo = parse_github_repo_input(github_remotes["upstream"])
            return self._hydrate_repo_ref(owner, repo, source="git_remote_upstream")
        if "origin" in github_remotes:
            owner, repo = parse_github_repo_input(github_remotes["origin"])
            return self._hydrate_repo_ref(owner, repo, source="git_remote_origin")
        if len(github_remotes) == 1:
            remote_name, remote_url = next(iter(github_remotes.items()))
            owner, repo = parse_github_repo_input(remote_url)
            return self._hydrate_repo_ref(owner, repo, source=f"git_remote_{remote_name}")

        if require_github:
            raise RuntimeError(
                "multiple GitHub remotes were found; provide an explicit GitHub repo URL or owner/repo slug"
            )
        return None

    def _hydrate_repo_ref(self, owner: str, repo: str, *, source: str) -> GitHubRepoRef:
        canonical_owner = owner
        canonical_repo = repo
        clone_url = f"https://github.com/{owner}/{repo}.git"
        html_url = f"https://github.com/{owner}/{repo}"
        default_branch = None
        if self.github_api is not None:
            payload = self.github_api.get_repository(owner, repo)
            canonical_owner = payload.get("owner", {}).get("login") or owner
            canonical_repo = payload.get("name") or repo
            clone_url = payload.get("clone_url") or clone_url
            html_url = payload.get("html_url") or html_url
            default_branch = payload.get("default_branch")
        slug = f"{canonical_owner}/{canonical_repo}"
        return GitHubRepoRef(
            owner=canonical_owner,
            name=canonical_repo,
            canonical_repo=slug,
            clone_url=clone_url,
            html_url=html_url,
            default_branch=default_branch,
            source=source,
        )


def parse_github_repo_input(value: str) -> tuple[str, str]:
    normalized = value.strip()
    for pattern in (_GITHUB_HTTP_RE, _GITHUB_SSH_RE, _GITHUB_SLUG_RE):
        match = pattern.match(normalized)
        if match:
            return match.group("owner"), match.group("repo")
    raise RuntimeError(f"unsupported GitHub repository input: {value!r}")


def is_github_remote(value: str) -> bool:
    try:
        parse_github_repo_input(value)
    except RuntimeError:
        return False
    return True


def collect_unique_numbers(payloads: Iterable[Mapping[str, Any]]) -> tuple[int, ...]:
    values = {
        int(payload["number"])
        for payload in payloads
        if "number" in payload and isinstance(payload["number"], int)
    }
    return tuple(sorted(values))
