"""Tests for context_graph.ingestion.git.github_client to increase coverage."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from context_graph.ingestion.git.github_client import (
    GitHubApiClient,
    GitHubApiError,
    GitHubRepoRef,
    GitHubRepoResolver,
    _graphql_connection_nodes,
    _graphql_issue_number,
    _graphql_issue_numbers_from_event,
    collect_unique_numbers,
    is_github_remote,
    parse_github_repo_input,
)


class TestGitHubApiError:
    def test_stores_status_code(self):
        err = GitHubApiError(404, "not found")
        assert err.status_code == 404
        assert "not found" in str(err)


class TestParseGitHubRepoInput:
    def test_https_url(self):
        owner, repo = parse_github_repo_input("https://github.com/owner/repo.git")
        assert owner == "owner"
        assert repo == "repo"

    def test_ssh_url(self):
        owner, repo = parse_github_repo_input("git@github.com:owner/repo.git")
        assert owner == "owner"
        assert repo == "repo"

    def test_slug(self):
        owner, repo = parse_github_repo_input("owner/repo")
        assert owner == "owner"
        assert repo == "repo"

    def test_invalid_raises(self):
        with pytest.raises(RuntimeError, match="unsupported"):
            parse_github_repo_input("not-a-valid-input")


class TestIsGitHubRemote:
    def test_valid(self):
        assert is_github_remote("https://github.com/owner/repo") is True

    def test_invalid(self):
        assert is_github_remote("https://gitlab.com/owner/repo") is False


class TestCollectUniqueNumbers:
    def test_collects_and_sorts(self):
        payloads = [{"number": 3}, {"number": 1}, {"number": 3}, {"number": 2}]
        assert collect_unique_numbers(payloads) == (1, 2, 3)

    def test_skips_non_int(self):
        payloads = [{"number": "not_int"}, {"other": 1}]
        assert collect_unique_numbers(payloads) == ()


class TestGraphQLHelpers:
    def test_connection_nodes_valid(self):
        payload = {"nodes": [{"id": 1}, {"id": 2}]}
        assert len(_graphql_connection_nodes(payload)) == 2

    def test_connection_nodes_invalid(self):
        assert _graphql_connection_nodes({"nodes": "not a list"}) == []
        assert _graphql_connection_nodes({}) == []

    def test_issue_number_valid(self):
        payload = {
            "__typename": "Issue",
            "number": 42,
            "repository": {"nameWithOwner": "owner/repo"},
        }
        assert _graphql_issue_number(payload, "owner/repo") == 42

    def test_issue_number_wrong_typename(self):
        payload = {"__typename": "PullRequest", "number": 42, "repository": {"nameWithOwner": "owner/repo"}}
        assert _graphql_issue_number(payload, "owner/repo") is None

    def test_issue_number_wrong_repo(self):
        payload = {"__typename": "Issue", "number": 42, "repository": {"nameWithOwner": "other/repo"}}
        assert _graphql_issue_number(payload, "owner/repo") is None

    def test_issue_number_no_repository(self):
        payload = {"__typename": "Issue", "number": 42}
        assert _graphql_issue_number(payload, "owner/repo") is None

    def test_issue_number_non_int(self):
        payload = {"__typename": "Issue", "number": "not_int", "repository": {"nameWithOwner": "owner/repo"}}
        assert _graphql_issue_number(payload, "owner/repo") is None

    def test_issue_numbers_from_event(self):
        event = {
            "source": {"__typename": "Issue", "number": 10, "repository": {"nameWithOwner": "owner/repo"}},
            "subject": {"__typename": "Issue", "number": 20, "repository": {"nameWithOwner": "owner/repo"}},
        }
        numbers = _graphql_issue_numbers_from_event(event, "owner/repo", fields=("source", "subject"))
        assert numbers == {10, 20}

    def test_issue_numbers_from_event_non_mapping(self):
        event = {"source": "not a mapping"}
        numbers = _graphql_issue_numbers_from_event(event, "owner/repo", fields=("source",))
        assert numbers == set()


class FakeUrlResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class TestGitHubApiClient:
    def _make_client(self, token: str | None = "test-token"):
        return GitHubApiClient(token=token, api_base_url="https://api.github.com")

    def test_get_repository(self):
        client = self._make_client()
        response_data = json.dumps({"id": 1, "name": "repo"}).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            result = client.get_repository("owner", "repo")
        assert result["name"] == "repo"

    def test_get_pull_request(self):
        client = self._make_client()
        response_data = json.dumps({"number": 1, "title": "PR"}).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            result = client.get_pull_request("owner", "repo", 1)
        assert result["number"] == 1

    def test_get_pull_request_invalid_response(self):
        client = self._make_client()
        response_data = json.dumps(["not-a-dict"]).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            with pytest.raises(RuntimeError, match="unexpected GitHub pull request response"):
                client.get_pull_request("owner", "repo", 1)

    def test_get_issue(self):
        client = self._make_client()
        response_data = json.dumps({"number": 5, "title": "Issue"}).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            result = client.get_issue("owner", "repo", 5)
        assert result["number"] == 5

    def test_get_issue_invalid_response(self):
        client = self._make_client()
        response_data = json.dumps(["not-a-dict"]).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            with pytest.raises(RuntimeError, match="unexpected GitHub issue response"):
                client.get_issue("owner", "repo", 5)

    def test_paginate_single_page(self):
        client = self._make_client()
        response_data = json.dumps([{"id": 1}]).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            result = client.list_pull_request_reviews("owner", "repo", 1)
        assert len(result) == 1

    def test_list_pull_request_review_comments_and_timeline(self):
        client = self._make_client()
        response_data = json.dumps([{"id": 1}]).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            assert client.list_pull_request_review_comments("owner", "repo", 1) == [{"id": 1}]
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            assert client.list_issue_timeline("owner", "repo", 1) == [{"id": 1}]

    def test_paginate_empty(self):
        client = self._make_client()
        response_data = json.dumps([]).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            result = client.list_issue_comments("owner", "repo", 1)
        assert result == []

    def test_paginate_multiple_pages_and_invalid_payload(self):
        client = self._make_client()
        page1 = json.dumps([{"id": idx} for idx in range(100)]).encode()
        page2 = json.dumps([{"id": 101}]).encode()
        with patch(
            "context_graph.ingestion.git.github_client.urlopen",
            side_effect=[FakeUrlResponse(page1), FakeUrlResponse(page2)],
        ):
            result = client.list_pull_request_reviews("owner", "repo", 1)
        assert len(result) == 101

        invalid_payload = json.dumps({"id": 1}).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(invalid_payload)):
            with pytest.raises(RuntimeError, match="unexpected paginated GitHub response"):
                client.list_pull_request_reviews("owner", "repo", 1)

    def test_request_json_http_error(self):
        from urllib.error import HTTPError
        client = self._make_client()
        error = HTTPError("https://api.github.com/repos/o/r", 404, "Not Found", {}, None)
        error.read = lambda: b"not found"
        with patch("context_graph.ingestion.git.github_client.urlopen", side_effect=error):
            with pytest.raises(GitHubApiError) as exc_info:
                client.get_repository("o", "r")
        assert exc_info.value.status_code == 404

    def test_request_json_url_error(self):
        from urllib.error import URLError
        client = self._make_client()
        with patch("context_graph.ingestion.git.github_client.urlopen", side_effect=URLError("connection refused")):
            with pytest.raises(RuntimeError, match="GitHub API request failed"):
                client.get_repository("o", "r")

    def test_request_json_empty_body(self):
        client = self._make_client()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(b"")):
            # get_repository expects a dict, so empty body returns None which raises
            with pytest.raises(RuntimeError, match="unexpected"):
                client.get_repository("o", "r")

    def test_graphql_errors(self):
        client = self._make_client()
        response_data = json.dumps({"errors": [{"message": "bad query"}]}).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            with pytest.raises(RuntimeError, match="bad query"):
                client._graphql(query="query { }", variables={})

    def test_graphql_missing_data(self):
        client = self._make_client()
        response_data = json.dumps({"something": "else"}).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            with pytest.raises(RuntimeError, match="missing data"):
                client._graphql(query="query { }", variables={})

    def test_graphql_unexpected_payload_type(self):
        client = self._make_client()
        response_data = json.dumps(["not-a-mapping"]).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            with pytest.raises(RuntimeError, match="unexpected GitHub GraphQL response"):
                client._graphql(query="query { }", variables={})

    def test_list_commit_pull_requests_graphql(self):
        client = self._make_client()
        graphql_response = {
            "data": {
                "repository": {
                    "object": {
                        "__typename": "Commit",
                        "associatedPullRequests": {
                            "nodes": [
                                {
                                    "id": "PR_1",
                                    "number": 42,
                                    "title": "Fix bug",
                                    "url": "https://github.com/owner/repo/pull/42",
                                    "state": "MERGED",
                                    "body": "body",
                                    "createdAt": "2026-01-01",
                                    "updatedAt": "2026-01-02",
                                    "mergedAt": "2026-01-03",
                                    "baseRefName": "main",
                                    "headRefName": "fix-branch",
                                    "headRefOid": "abc123",
                                    "mergeCommit": {"oid": "def456"},
                                    "author": {"login": "dev"},
                                }
                            ]
                        },
                    }
                }
            }
        }
        response_data = json.dumps(graphql_response).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            result = client.list_commit_pull_requests("owner", "repo", "abc123")
        assert len(result) == 1
        assert result[0]["number"] == 42

    def test_list_commit_pull_requests_no_token_uses_rest(self):
        client = self._make_client(token=None)
        response_data = json.dumps([{"id": 1, "number": 10}]).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            result = client.list_commit_pull_requests("owner", "repo", "sha")
        assert len(result) == 1

    def test_list_pull_request_linked_issue_numbers(self):
        client = self._make_client()
        graphql_response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "closingIssuesReferences": {
                            "nodes": [
                                {"__typename": "Issue", "number": 5, "repository": {"nameWithOwner": "owner/repo"}}
                            ]
                        },
                        "timelineItems": {
                            "nodes": [
                                {
                                    "__typename": "ConnectedEvent",
                                    "source": {"__typename": "Issue", "number": 10, "repository": {"nameWithOwner": "owner/repo"}},
                                    "subject": {"__typename": "Issue", "number": 11, "repository": {"nameWithOwner": "owner/repo"}},
                                },
                                {
                                    "__typename": "DisconnectedEvent",
                                    "source": {"__typename": "Issue", "number": 10, "repository": {"nameWithOwner": "owner/repo"}},
                                    "subject": {},
                                },
                                {
                                    "__typename": "CrossReferencedEvent",
                                    "source": {"__typename": "Issue", "number": 20, "repository": {"nameWithOwner": "owner/repo"}},
                                    "target": {},
                                },
                            ]
                        },
                    }
                }
            }
        }
        response_data = json.dumps(graphql_response).encode()
        with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
            numbers = client.list_pull_request_linked_issue_numbers("owner", "repo", 1)
        assert 5 in numbers
        assert 11 in numbers
        assert 20 in numbers

    def test_list_pull_request_linked_issue_numbers_missing_shapes(self):
        client = self._make_client()
        for payload in (
            {"data": {"repository": []}},
            {"data": {"repository": {"pullRequest": []}}},
        ):
            response_data = json.dumps(payload).encode()
            with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
                assert client.list_pull_request_linked_issue_numbers("owner", "repo", 1) == ()

    def test_list_commit_pull_requests_graphql_missing_shapes_and_non_int(self):
        client = self._make_client()
        payloads = (
            {"data": {"repository": []}},
            {"data": {"repository": {"object": []}}},
            {"data": {"repository": {"object": {"associatedPullRequests": []}}}},
            {
                "data": {
                    "repository": {
                        "object": {
                            "associatedPullRequests": {
                                "nodes": [{"number": "x"}]
                            }
                        }
                    }
                }
            },
        )
        for payload in payloads:
            response_data = json.dumps(payload).encode()
            with patch("context_graph.ingestion.git.github_client.urlopen", return_value=FakeUrlResponse(response_data)):
                assert client.list_commit_pull_requests("owner", "repo", "sha") == []


class TestGitHubRepoResolver:
    def test_resolve_from_github_repo(self):
        resolver = GitHubRepoResolver(github_api=None)
        ref = resolver.resolve(github_repo="owner/repo")
        assert ref is not None
        assert ref.owner == "owner"
        assert ref.name == "repo"

    def test_resolve_from_github_url(self):
        resolver = GitHubRepoResolver(github_api=None)
        ref = resolver.resolve(github_url="https://github.com/owner/repo")
        assert ref is not None
        assert ref.owner == "owner"

    def test_resolve_from_repo_path_origin(self):
        fake_git = MagicMock()
        fake_git.list_remotes.return_value = {"origin": "https://github.com/owner/repo.git"}
        resolver = GitHubRepoResolver(git_metadata=fake_git, github_api=None)
        ref = resolver.resolve(repo_path="/tmp/repo")
        assert ref is not None
        assert ref.source == "git_remote_origin"

    def test_resolve_from_repo_path_upstream(self):
        fake_git = MagicMock()
        fake_git.list_remotes.return_value = {
            "origin": "https://github.com/fork/repo.git",
            "upstream": "https://github.com/owner/repo.git",
        }
        resolver = GitHubRepoResolver(git_metadata=fake_git, github_api=None)
        ref = resolver.resolve(repo_path="/tmp/repo")
        assert ref is not None
        assert ref.source == "git_remote_upstream"

    def test_resolve_from_repo_path_single_remote(self):
        fake_git = MagicMock()
        fake_git.list_remotes.return_value = {"custom": "https://github.com/owner/repo.git"}
        resolver = GitHubRepoResolver(git_metadata=fake_git, github_api=None)
        ref = resolver.resolve(repo_path="/tmp/repo")
        assert ref is not None
        assert ref.source == "git_remote_custom"

    def test_resolve_no_github_remotes(self):
        fake_git = MagicMock()
        fake_git.list_remotes.return_value = {"origin": "https://gitlab.com/owner/repo.git"}
        resolver = GitHubRepoResolver(git_metadata=fake_git, github_api=None)
        ref = resolver.resolve(repo_path="/tmp/repo")
        assert ref is None

    def test_resolve_no_github_remotes_require_github(self):
        fake_git = MagicMock()
        fake_git.list_remotes.return_value = {"origin": "https://gitlab.com/owner/repo.git"}
        resolver = GitHubRepoResolver(git_metadata=fake_git, github_api=None)
        with pytest.raises(RuntimeError, match="does not have a GitHub remote"):
            resolver.resolve(repo_path="/tmp/repo", require_github=True)

    def test_resolve_no_path_require_github(self):
        resolver = GitHubRepoResolver(github_api=None)
        with pytest.raises(RuntimeError, match="hosted indexing requires"):
            resolver.resolve(require_github=True)

    def test_resolve_no_path_without_require_github(self):
        resolver = GitHubRepoResolver(github_api=None)
        assert resolver.resolve() is None

    def test_resolve_multiple_github_remotes_require_github(self):
        fake_git = MagicMock()
        fake_git.list_remotes.return_value = {
            "remote1": "https://github.com/a/repo.git",
            "remote2": "https://github.com/b/repo.git",
        }
        resolver = GitHubRepoResolver(git_metadata=fake_git, github_api=None)
        with pytest.raises(RuntimeError, match="multiple GitHub remotes"):
            resolver.resolve(repo_path="/tmp/repo", require_github=True)

    def test_resolve_multiple_github_remotes_without_require_github(self):
        fake_git = MagicMock()
        fake_git.list_remotes.return_value = {
            "remote1": "https://github.com/a/repo.git",
            "remote2": "https://github.com/b/repo.git",
        }
        resolver = GitHubRepoResolver(git_metadata=fake_git, github_api=None)
        assert resolver.resolve(repo_path="/tmp/repo", require_github=False) is None

    def test_hydrate_with_api(self):
        fake_api = MagicMock()
        fake_api.get_repository.return_value = {
            "owner": {"login": "Owner"},
            "name": "Repo",
            "clone_url": "https://github.com/Owner/Repo.git",
            "html_url": "https://github.com/Owner/Repo",
            "default_branch": "main",
        }
        resolver = GitHubRepoResolver(github_api=fake_api)
        ref = resolver.resolve(github_repo="owner/repo")
        assert ref.owner == "Owner"
        assert ref.default_branch == "main"
