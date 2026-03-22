from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import boto3

from context_graph.github_client import GitHubApiClient
from context_graph.graph_sinks import NeptuneGraphSink, Neo4jGraphSink
from context_graph.hosted_indexer import HostedIndexRequest, HostedIndexer
from context_graph.metadata import PostgresMetadataStore
from context_graph.repo_cache import RepoCacheManager
from context_graph.snapshot_builder import CodebaseMemorySnapshotProducer


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    request = _parse_request(event)
    metadata_store = PostgresMetadataStore.from_dsn(_metadata_dsn())
    graph_sink = _graph_sink()
    github_token = _github_token()
    github_api = GitHubApiClient(token=github_token)
    repo_cache_dir = os.getenv("REPO_CACHE_DIR", "/mnt/context-graph")
    snapshot_root = Path(__file__).resolve().parent.parent

    hosted_indexer = HostedIndexer(
        metadata_store=metadata_store,
        graph_sink=graph_sink,
        snapshot_producer=CodebaseMemorySnapshotProducer(root=snapshot_root),
        github_api=github_api,
        repo_cache=RepoCacheManager(base_dir=repo_cache_dir),
    )
    result = hosted_indexer.index(request)
    body = {
        "request_id": result.request_id,
        "repo_id": result.repo_id,
        "repo_path": result.repo_path,
        "repo_ref": _repo_ref_summary(result.repo_ref),
        "repo_source_state": _repo_source_state_summary(result.repo_source_state),
        "commit": {
            "sha": result.commit_result.commit.sha,
            "branch": result.commit_result.commit.branch,
            "parent_shas": list(result.commit_result.commit.parent_shas),
            "changed_files": list(result.commit_result.commit.changed_files),
            "subject": result.commit_result.commit.subject,
        },
        "commit_index": {
            "skipped": result.commit_result.skipped,
            "skip_reason": result.commit_result.skip_reason,
            "previous_commit_sha": result.commit_result.previous_commit_sha,
            "applied_commit_shas": list(result.commit_result.applied_commit_shas),
            "statements_emitted": len(result.commit_result.statements),
            "statements_executed": result.commit_result.apply_result.statements_executed if result.commit_result.apply_result else 0,
        },
        "github": None if result.github_result is None else {
            "skipped": result.github_result.skipped,
            "skip_reason": result.github_result.skip_reason,
            "entity_counts": dict(result.github_result.entity_counts),
            "pull_request_numbers": list(result.github_result.pull_request_numbers),
            "issue_numbers": list(result.github_result.issue_numbers),
            "statements_emitted": len(result.github_result.statements),
            "statements_executed": result.github_result.statements_executed,
        },
        "github_error": result.github_error,
        "statements_emitted": len(result.statements),
        "statements_executed": result.statements_executed,
    }
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, sort_keys=True),
    }


def _parse_request(event: dict[str, Any]) -> HostedIndexRequest:
    payload = event
    if "body" in event:
        raw_body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        payload = json.loads(raw_body) if raw_body else {}
    return HostedIndexRequest(
        repo_path=payload.get("repo_path"),
        github_url=payload.get("github_url"),
        github_repo=payload.get("github_repo"),
        repo_id=payload.get("repo_id"),
        ref=payload.get("ref"),
        commit_sha=payload.get("commit_sha"),
        limit=int(payload.get("limit", 1000000)),
        execute=bool(payload.get("execute", True)),
    )


def _graph_sink():
    neptune_host = os.getenv("NEPTUNE_ENDPOINT")
    if neptune_host:
        return NeptuneGraphSink(host=neptune_host)
    neo4j_uri = os.getenv("NEO4J_URI")
    neo4j_user = os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER")
    neo4j_password = os.getenv("NEO4J_PASSWORD")
    if neo4j_uri and neo4j_user is not None and neo4j_password is not None:
        return Neo4jGraphSink(uri=neo4j_uri, username=neo4j_user, password=neo4j_password)
    raise RuntimeError("repo-sync-index Lambda requires NEPTUNE_ENDPOINT or Neo4j credentials")


def _metadata_dsn() -> str:
    direct_dsn = os.getenv("CONTEXT_GRAPH_METADATA_DSN")
    if direct_dsn:
        return direct_dsn
    endpoint = os.environ["AURORA_ENDPOINT"]
    db_name = os.getenv("AURORA_DB_NAME", "context_graph")
    username = os.getenv("AURORA_USERNAME", "postgres")
    password = _secret_string(os.environ["AURORA_PASSWORD_SECRET_ARN"])
    return f"postgresql://{quote_plus(username)}:{quote_plus(password)}@{endpoint}/{quote_plus(db_name)}"


def _github_token() -> str:
    token = os.getenv("GITHUB_TOKEN")
    if token:
        return token
    secret_arn = os.getenv("GITHUB_TOKEN_SECRET_ARN")
    if not secret_arn:
        raise RuntimeError("GITHUB_TOKEN_SECRET_ARN or GITHUB_TOKEN must be configured")
    return _secret_string(secret_arn)


def _secret_string(secret_arn: str) -> str:
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    raw_secret = response.get("SecretString")
    if raw_secret is None:
        raise RuntimeError(f"secret {secret_arn} did not contain a SecretString")
    try:
        payload = json.loads(raw_secret)
    except json.JSONDecodeError:
        return raw_secret
    for key in ("token", "github_token", "password", "value"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    raise RuntimeError(f"secret {secret_arn} did not contain a supported token/password field")


def _repo_ref_summary(repo_ref):
    if repo_ref is None:
        return None
    return {
        "owner": repo_ref.owner,
        "name": repo_ref.name,
        "canonical_repo": repo_ref.canonical_repo,
        "clone_url": repo_ref.clone_url,
        "default_branch": repo_ref.default_branch,
        "source": repo_ref.source,
    }


def _repo_source_state_summary(state):
    if state is None:
        return None
    return {
        "provider": state.provider,
        "canonical_repo": state.canonical_repo,
        "clone_url": state.clone_url,
        "default_branch": state.default_branch,
        "clone_path": state.clone_path,
        "last_fetch_at": state.last_fetch_at,
        "last_fetched_head_sha": state.last_fetched_head_sha,
        "last_github_sync_at": state.last_github_sync_at,
        "last_github_sync_status": state.last_github_sync_status,
        "last_github_sync_error": state.last_github_sync_error,
        "metadata": dict(state.metadata or {}),
    }
