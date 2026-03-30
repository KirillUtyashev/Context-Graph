from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from unittest.mock import patch

import pytest

from context_graph.ingestion.git.github_client import (
    GitHubApiClient,
    GitHubApiError,
    GitHubRepoResolver,
    _graphql_connection_nodes,
    _graphql_issue_number,
    _graphql_issue_numbers_from_event,
)


class FakeResponse:
    def __init__(self, payload: object, *, as_json: bool = True) -> None:
        self.payload = payload
        self.as_json = as_json

    def read(self) -> bytes:
        if self.as_json:
            return json.dumps(self.payload).encode("utf-8")
        return str(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeGitMetadata:
    def __init__(self, remotes: dict[str, str]) -> None:
        self.remotes = remotes

    def list_remotes(self, repo_path: str) -> dict[str, str]:
        return dict(self.remotes)


def test_request_json_handles_success_empty_body_and_errors() -> None:
    client = GitHubApiClient(token="secret")

    with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeResponse({"ok": True})):
        assert client._request_json("GET", "/repos/o/r") == {"ok": True}

    with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeResponse("", as_json=False)):
        assert client._request_json("GET", "/repos/o/r") is None

    http_error = HTTPError(
        "https://api.github.com/repos/o/r",
        404,
        "Not Found",
        hdrs=None,
        fp=io.BytesIO(b'{"message":"missing"}'),
    )
    with patch("context_graph.ingestion.git.github_client.urlopen", side_effect=http_error):
        with pytest.raises(GitHubApiError) as exc:
            client._request_json("GET", "/repos/o/r")
    assert exc.value.status_code == 404

    with patch(
        "context_graph.ingestion.git.github_client.urlopen",
        side_effect=URLError("offline"),
    ):
        with pytest.raises(RuntimeError, match="offline"):
            client._request_json("GET", "/repos/o/r")


def test_paginate_and_basic_rest_wrappers_cover_happy_paths() -> None:
    client = GitHubApiClient(token=None)

    responses = [
        [{"number": 1}] * 100,
        [{"number": 2}],
    ]
    with patch.object(client, "_request_json", side_effect=responses):
        rows = client._paginate("/repos/o/r/pulls")
    assert len(rows) == 101

    with patch.object(client, "_request_json", return_value={"id": 1}):
        assert client.get_repository("o", "r") == {"id": 1}
        assert client.get_pull_request("o", "r", 7) == {"id": 1}
        assert client.get_issue("o", "r", 9) == {"id": 1}

    with patch.object(client, "_request_json", return_value=[]):
        assert client.list_pull_request_reviews("o", "r", 7) == []
        assert client.list_pull_request_review_comments("o", "r", 7) == []
        assert client.list_issue_comments("o", "r", 9) == []
        assert client.list_issue_timeline("o", "r", 9) == []


def test_graphql_helpers_cover_errors_missing_data_and_issue_extraction() -> None:
    client = GitHubApiClient(token="secret")

    with patch.object(client, "_request_json", return_value={"errors": [{"message": "boom"}], "data": {}}):
        with pytest.raises(RuntimeError, match="boom"):
            client._graphql(query="query {}", variables={})

    with patch.object(client, "_request_json", return_value={"data": None}):
        with pytest.raises(RuntimeError, match="missing data"):
            client._graphql(query="query {}", variables={})

    assert _graphql_connection_nodes({"nodes": [{"a": 1}, 2, {"b": 2}]}) == [{"a": 1}, {"b": 2}]
    assert _graphql_connection_nodes({"nodes": None}) == []

    issue = {"__typename": "Issue", "number": 7, "repository": {"nameWithOwner": "OpenAI/Context-Graph"}}
    assert _graphql_issue_number(issue, "openai/context-graph") == 7
    assert _graphql_issue_number({"__typename": "PullRequest"}, "openai/context-graph") is None
    assert _graphql_issue_numbers_from_event({"source": issue, "subject": {"bad": "value"}}, "openai/context-graph", fields=("source", "subject")) == {7}


def test_graphql_commit_and_linked_issue_helpers_cover_branching_paths() -> None:
    client = GitHubApiClient(token="secret")

    with patch.object(
        client,
        "_graphql",
        return_value={
            "repository": {
                "object": {
                    "associatedPullRequests": {
                        "nodes": [
                            {
                                "id": "pr1",
                                "number": 7,
                                "url": "https://example/pr/7",
                                "title": "Improve rollback",
                                "state": "OPEN",
                                "body": "body",
                                "createdAt": "2026-03-27T00:00:00Z",
                                "updatedAt": "2026-03-27T00:00:00Z",
                                "mergedAt": None,
                                "baseRefName": "main",
                                "headRefName": "feature",
                                "headRefOid": "abc",
                                "mergeCommit": {"oid": "def"},
                                "author": {"login": "octocat"},
                            },
                            {"number": "bad"},
                        ]
                    }
                }
            }
        },
    ):
        pulls = client.list_commit_pull_requests("openai", "context-graph", "abc123")
    assert pulls[0]["number"] == 7
    assert pulls[0]["state"] == "open"

    with patch.object(
        client,
        "_graphql",
        return_value={
            "repository": {
                "pullRequest": {
                    "closingIssuesReferences": {
                        "nodes": [
                            {
                                "__typename": "Issue",
                                "number": 1,
                                "repository": {"nameWithOwner": "openai/context-graph"},
                            }
                        ]
                    },
                    "timelineItems": {
                        "nodes": [
                            {
                                "__typename": "ConnectedEvent",
                                "source": {
                                    "__typename": "Issue",
                                    "number": 2,
                                    "repository": {"nameWithOwner": "openai/context-graph"},
                                },
                                "subject": {"__typename": "PullRequest", "number": 10, "repository": {"nameWithOwner": "openai/context-graph"}},
                            },
                            {
                                "__typename": "CrossReferencedEvent",
                                "source": {
                                    "__typename": "Issue",
                                    "number": 3,
                                    "repository": {"nameWithOwner": "openai/context-graph"},
                                },
                                "target": {
                                    "__typename": "Issue",
                                    "number": 4,
                                    "repository": {"nameWithOwner": "openai/context-graph"},
                                },
                            },
                            {
                                "__typename": "DisconnectedEvent",
                                "source": {
                                    "__typename": "Issue",
                                    "number": 2,
                                    "repository": {"nameWithOwner": "openai/context-graph"},
                                },
                                "subject": {"__typename": "PullRequest", "number": 10, "repository": {"nameWithOwner": "openai/context-graph"}},
                            },
                        ]
                    },
                }
            }
        },
    ):
        numbers = client.list_pull_request_linked_issue_numbers("openai", "context-graph", 7)
    assert numbers == (1, 3, 4)


def test_list_commit_pull_requests_uses_rest_without_token_and_resolver_covers_error_paths() -> None:
    client = GitHubApiClient(token=None)
    with patch.object(client, "_paginate", return_value=[{"number": 9}]) as paginate:
        rows = client.list_commit_pull_requests("openai", "context-graph", "abc123")
    assert rows == [{"number": 9}]
    paginate.assert_called_once()

    resolver = GitHubRepoResolver(git_metadata=FakeGitMetadata({}), github_api=None)
    assert resolver.resolve(repo_path="/tmp/repo", require_github=False) is None

    with pytest.raises(RuntimeError, match="does not have a GitHub remote"):
        resolver.resolve(repo_path="/tmp/repo", require_github=True)

    resolver = GitHubRepoResolver(
        git_metadata=FakeGitMetadata(
            {
                "fork": "git@github.com:someone/context-graph.git",
                "mirror": "https://github.com/openai/context-graph.git",
            }
        ),
        github_api=None,
    )
    with pytest.raises(RuntimeError, match="multiple GitHub remotes"):
        resolver.resolve(repo_path="/tmp/repo", require_github=True)
