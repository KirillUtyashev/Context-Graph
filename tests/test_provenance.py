from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import tempfile
import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from context_graph.code_identity import CodeIdentityCanonicalizer, SnapshotCodeResolver
from context_graph.ingestion.commit_indexing import CommitIndexingService
from context_graph.ingestion.git.github_client import GitHubRepoRef
from context_graph.ingestion.git.github_indexer import GitHubIndexer, GitHubObservationSource, GitHubProvenanceBundle
from context_graph.ingestion.git.git_metadata import CommitInfo
from context_graph.ingestion.temporal import TemporalGraphWriter
from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.provenance import (
    ClaimCanonicalizationResult,
    ClaimCanonicalizationService,
    ClaimBundleValidator,
    ClaimCandidate,
    ClaimProfileKeyGenerationService,
    ClaimResolutionCandidateRetriever,
    ClaimWithEvidenceCandidate,
    ClaimDedupCandidate,
    ClaimDerivationResponse,
    ClaimDerivationService,
    ClaimEvidenceExtractionResponse,
    ClaimEvidenceExtractionService,
    ClaimRecord,
    ClaimResolutionService,
    ClaimSemanticRetrievalService,
    CurrentEvidenceReader,
    DEFAULT_PROVENANCE_SCHEMA_REGISTRY,
    ClaimRelationResponse,
    ClaimRelationService,
    EvidenceCandidate,
    EvidenceDedupCandidate,
    EvidenceDedupResult,
    EvidenceDedupService,
    EvidenceInvalidationPlanner,
    EvidenceMutationPlanner,
    FactConflictResolutionService,
    MarkdownObservationAdapter,
    Observation,
    ObservationIngestionItem,
    ObservationIngestionService,
    ObservationKind,
    ObservationMutationPlanner,
    ResearchArtifactSynthesisResponse,
    ResearchArtifactSynthesisService,
)
from context_graph.provenance.prompts import (
    extract_claims_and_evidence,
    normalize_claims,
    relate_claims,
    resolve_fact_conflicts,
    synthesize_research_artifact,
)
from context_graph.provenance.grounding import resolve_about_record_ids, resolve_mention_record_ids
from context_graph.provenance.canonicalization import canonical_claim_key
from context_graph.provenance.hint_enrichment import collect_grounding_hints
from context_graph.provenance.models import claim_record_id
from context_graph.profile_vectors import (
    ProfileSearchService,
    ProfileVectorIndexer,
    SemanticProvenanceSearchService,
)
from context_graph.models.profiles import (
    PROFILE_KEY_ORIGIN_GENERATED_SEMANTIC,
    PROFILE_KEY_ORIGIN_RAW_TEXT,
    ProfileKeyDefinition,
    ProfileRecord,
    RelationProfileSpec,
    SEMANTIC_CLAIM_PROFILE_KIND,
    SEMANTIC_OBSERVATION_PROFILE_KIND,
    SEMANTIC_RELATION_PROFILE_KIND,
    semantic_claim_profile_from_claim,
    semantic_observation_profile_from_observation,
    semantic_relation_profile_from_spec,
)
from context_graph.query.parser import QueryParseResult
from context_graph.backend import (
    GraphRecordRef,
    InMemoryMetadataStore,
    InMemoryProfileVectorStore,
    MutationPlan,
    RecordKind,
    SearchHit,
)


def make_snapshot() -> GraphSnapshot:
    project = CanonicalNode("project:demo", "demo", "Project", "demo", qualified_name="demo")
    file_node = CanonicalNode(
        "file:demo:demo.app.__file__",
        "demo",
        "File",
        "app.py",
        qualified_name="demo.app.__file__",
        file_path="app.py",
    )
    function_node = CanonicalNode(
        "symbol:demo:demo.app.main",
        "demo",
        "Function",
        "main",
        qualified_name="demo.app.main",
        file_path="app.py",
    )
    auth_main_file = CanonicalNode(
        "file:demo:demo.auth.main.__file__",
        "demo",
        "File",
        "main.py",
        qualified_name="demo.auth.main.__file__",
        file_path="src/auth/main.py",
    )
    web_main_file = CanonicalNode(
        "file:demo:demo.web.main.__file__",
        "demo",
        "File",
        "main.py",
        qualified_name="demo.web.main.__file__",
        file_path="src/web/main.py",
    )
    auth_service_class = CanonicalNode(
        "symbol:demo:demo.auth.AuthService",
        "demo",
        "Class",
        "AuthService",
        qualified_name="demo.auth.AuthService",
        file_path="src/auth/service.py",
    )
    auth_service_login = CanonicalNode(
        "symbol:demo:demo.auth.AuthService.login",
        "demo",
        "Method",
        "login",
        qualified_name="demo.auth.AuthService.login",
        file_path="src/auth/service.py",
    )
    web_login = CanonicalNode(
        "symbol:demo:demo.web.login",
        "demo",
        "Function",
        "login",
        qualified_name="demo.web.login",
        file_path="src/web/login.py",
    )
    edge = CanonicalEdge(
        "edge:demo:DEFINES:file:demo:demo.app.__file__:symbol:demo:demo.app.main",
        "demo",
        "DEFINES",
        file_node.node_id,
        function_node.node_id,
    )
    return GraphSnapshot(
        repo_id="demo",
        revision_id="r1",
        root_path="/tmp/demo",
        generated_at="2026-03-26T00:00:00+00:00",
        nodes=(
            project,
            file_node,
            function_node,
            auth_main_file,
            web_main_file,
            auth_service_class,
            auth_service_login,
            web_login,
        ),
        edges=(edge,),
        metadata={},
    )


def make_tree_snapshot() -> GraphSnapshot:
    return GraphSnapshot(
        repo_id="demo",
        revision_id="r1",
        root_path="/tmp/demo",
        generated_at="2026-03-26T00:00:00+00:00",
        nodes=(
            CanonicalNode("project:demo", "demo", "Project", "demo", qualified_name="demo"),
            CanonicalNode("folder:demo:demo.src", "demo", "Folder", "src", qualified_name="demo.src"),
            CanonicalNode("folder:demo:demo.src.auth", "demo", "Folder", "auth", qualified_name="demo.src.auth"),
            CanonicalNode(
                "file:demo:demo.src.auth.login.__file__",
                "demo",
                "File",
                "login.py",
                qualified_name="demo.src.auth.login.__file__",
                file_path="src/auth/login.py",
            ),
            CanonicalNode(
                "function:demo:demo.src.auth.login",
                "demo",
                "Function",
                "login",
                qualified_name="demo.src.auth.login",
                file_path="src/auth/login.py",
            ),
        ),
        edges=(),
        metadata={},
    )


def make_harness_like_snapshot() -> GraphSnapshot:
    return GraphSnapshot(
        repo_id="test",
        revision_id="r1",
        root_path="/tmp/test",
        generated_at="2026-03-27T00:00:00+00:00",
        nodes=(
            CanonicalNode("project:test", "test", "Project", "test", qualified_name="test"),
            CanonicalNode(
                "file:test:test.src.auth.__file__",
                "test",
                "File",
                "auth.py",
                qualified_name="test.src.auth.__file__",
                file_path="src/auth.py",
            ),
            CanonicalNode(
                "file:test:test.src.tasks.__file__",
                "test",
                "File",
                "tasks.py",
                qualified_name="test.src.tasks.__file__",
                file_path="src/tasks.py",
            ),
            CanonicalNode(
                "variable:test:test.src.auth.SECRET_KEY",
                "test",
                "Variable",
                "SECRET_KEY",
                qualified_name="test.src.auth.SECRET_KEY",
                file_path="src/auth.py",
            ),
            CanonicalNode(
                "variable:test:test.src.tasks.MAX_RETRIES",
                "test",
                "Variable",
                "MAX_RETRIES",
                qualified_name="test.src.tasks.MAX_RETRIES",
                file_path="src/tasks.py",
            ),
        ),
        edges=(),
        metadata={},
    )


def make_provenance_examples_snapshot() -> GraphSnapshot:
    return GraphSnapshot(
        repo_id="test",
        revision_id="r1",
        root_path="/tmp/test",
        generated_at="2026-03-27T00:00:00+00:00",
        nodes=(
            CanonicalNode("project:test", "test", "Project", "test", qualified_name="test"),
            CanonicalNode(
                "file:test:test.db.__file__",
                "test",
                "File",
                "db.py",
                qualified_name="test.db.__file__",
                file_path="db.py",
            ),
            CanonicalNode(
                "function:test:test.db.init_db",
                "test",
                "Function",
                "init_db",
                qualified_name="test.db.init_db",
                file_path="db.py",
            ),
            CanonicalNode(
                "variable:test:test.db.DB_URL",
                "test",
                "Variable",
                "DB_URL",
                qualified_name="test.db.DB_URL",
                file_path="db.py",
            ),
            CanonicalNode(
                "file:test:test.config.__file__",
                "test",
                "File",
                "config.py",
                qualified_name="test.config.__file__",
                file_path="config.py",
            ),
            CanonicalNode(
                "variable:test:test.config.APP_PORT",
                "test",
                "Variable",
                "APP_PORT",
                qualified_name="test.config.APP_PORT",
                file_path="config.py",
            ),
        ),
        edges=(),
        metadata={},
    )


def make_commit(
    *,
    sha: str = "abc123",
    parents: tuple[str, ...] = ("parent1",),
    changed_files: tuple[str, ...] = ("app.py",),
    subject: str = "Add helper support",
    body: str = "body text",
) -> CommitInfo:
    return CommitInfo(
        sha=sha,
        parent_shas=parents,
        branch="main",
        author_name="Dev",
        author_email="dev@example.com",
        authored_at="2026-03-26T00:00:00+00:00",
        committed_at="2026-03-26T00:00:05+00:00",
        subject=subject,
        body=body,
        changed_files=changed_files,
    )


def make_observation(
    *,
    kind: ObservationKind = ObservationKind.GITHUB_PR_BODY,
    source_kind: str = "pull_request",
    text: str = "This PR updates demo.app.main.",
) -> Observation:
    source_id = {
        "pull_request": "github:pr:demo:7",
        "issue": "github:issue:demo:9",
        "commit": "commit:demo:abc123",
    }.get(source_kind, "github:pr:demo:7")
    thread_id = {
        "pull_request": "7",
        "issue": "9",
        "commit": "abc123",
    }.get(source_kind)
    metadata = {"pull_request_number": 7}
    if source_kind == "issue":
        metadata = {"issue_number": 9}
    if source_kind == "commit":
        metadata = {"commit_sha": "abc123", "changed_files": ["app.py"]}
    return Observation(
        observation_id="observation:github_pr_body:demo:7",
        repo_id="demo",
        observation_kind=kind,
        source_kind=source_kind,
        source_id=source_id,
        observed_at="2026-03-26T00:01:00+00:00",
        author="octocat",
        title="Tighten rollback handling",
        text=text,
        metadata=metadata,
        ingested_at="2026-03-28T00:00:00Z",
        thread_kind=source_kind if source_kind in {"pull_request", "issue", "commit"} else None,
        thread_id=thread_id,
    )


def make_extraction_response(
    *,
    claim_text: str = "demo.app.main was updated.",
    claim_kind: str | None = "change_claim",
    claim_subkind: str | None = None,
    assertion_status: str | None = None,
    claim_frame: str | None = None,
    confidence: str = "high",
    source_hint: str | None = "demo.app.main",
    quote_text: str = "updates demo.app.main",
    span_start: int | None = 8,
    span_end: int | None = 29,
    evidence_kind: str = "change_summary",
    support_kind: str = "direct_statement",
    support_source: str = "observation_text",
    support_role: str = "states_change",
    support_rationale: str | None = None,
    summary_text: str | None = None,
) -> ClaimEvidenceExtractionResponse:
    if claim_subkind is None and claim_kind is not None:
        claim_subkind = {
            "bug_report": "bug_or_regression",
            "change_claim": "fix_or_mitigation",
            "state_claim": "invariant_or_constraint",
            "requirement": "desired_behavior_or_requirement",
            "review_feedback": "reviewer_objection_or_requested_change",
            "approval": "approval_or_acceptance",
            "concern": "reviewer_objection_or_requested_change",
            "workaround": "workaround_or_temporary_mitigation",
            "scope_statement": "rollout_migration_or_dependency",
            "test_expectation": "test_result_or_validation",
        }.get(claim_kind)
    return ClaimEvidenceExtractionResponse.model_validate(
        {
            "claims": [
                {
                    "claim_text": claim_text,
                    "claim_subkind": claim_subkind,
                    "assertion_status": assertion_status,
                    "claim_frame": claim_frame,
                    "about_hints": [source_hint] if source_hint else [],
                    "confidence": confidence,
                    "derivation_kind": "direct_from_observation",
                    "metadata": {},
                    "supporting_evidence": [
                        {
                            "evidence_kind": evidence_kind,
                            "quote_text": quote_text,
                            "summary_text": (
                                summary_text
                                if summary_text is not None
                                else (
                                    f"The source mentions {source_hint}."
                                    if source_hint
                                    else "The source supports the claim."
                                )
                            ),
                            "span_start": span_start,
                            "span_end": span_end,
                            "about_hints": [source_hint] if source_hint else [],
                            "support_kind": support_kind,
                            "support_source": support_source,
                            "support_role": support_role,
                            "support_rationale": (
                                support_rationale
                                if support_rationale is not None
                                else (
                                    f"This quote directly supports the claim about {source_hint}."
                                    if source_hint
                                    else "This quote directly supports the claim."
                                )
                            ),
                            "metadata": {},
                        }
                    ],
                }
            ]
        }
    )


def make_existing_evidence_row(
    *,
    evidence_id: str = "evidence:demo:existing",
    claim_text: str = "demo.app.main is disabled.",
    claim_kind: str = "state_claim",
    observed_at: str | None = "2026-03-25T00:00:00+00:00",
    about_record_ids: tuple[str, ...] = ("symbol:demo:demo.app.main@r1",),
    fact_key: str | None = None,
    top_level_claims: bool = True,
    is_current: bool | None = True,
) -> dict[str, object]:
    resolved_fact_key = fact_key or canonical_claim_key(
        claim_kind=claim_kind,
        claim_text=claim_text,
    )
    evidence_properties = {
        "evidence_id": evidence_id,
        "repo_id": "demo",
        "evidence_kind": "state_detail",
        "quote_text": "demo.app.main is disabled",
        "summary_text": "The source says demo.app.main is disabled.",
        "span_start": 0,
        "span_end": 25,
        "support_kind": "direct_statement",
        "support_source": "observation_text",
        "fact_key": resolved_fact_key if top_level_claims else None,
        "claim_text": claim_text if top_level_claims else None,
        "claim_kind": claim_kind if top_level_claims else None,
        "claim_subkind": "invariant_or_constraint" if top_level_claims else None,
        "assertion_status": "reported" if top_level_claims else None,
        "claim_frame": "author_statement" if top_level_claims else None,
        "claim_confidence": "high" if top_level_claims else None,
        "claim_derivation_kind": "direct_from_observation" if top_level_claims else None,
        "observation_id": "observation:github_pr_body:demo:5" if top_level_claims else None,
        "observed_at": observed_at if top_level_claims else None,
        "is_current": is_current,
        "metadata_json": json.dumps(
            {
                "claim_text": claim_text,
                "claim_kind": claim_kind,
                "claim_subkind": "invariant_or_constraint",
                "assertion_status": "reported",
                "claim_frame": "author_statement",
                "claim_confidence": "high",
                "claim_derivation_kind": "direct_from_observation",
                "support_source": "observation_text",
            }
        ),
    }
    return {
        "evidence_id": evidence_id,
        "evidence_properties": evidence_properties,
        "about_record_ids": list(about_record_ids),
        "observation_record_ids": ["observation:github_pr_body:demo:5"],
        "observation_properties": [
            {
                "observed_at": observed_at,
                "source_kind": "pull_request",
                "source_id": "github:pr:demo:5",
                "thread_kind": "pull_request",
                "thread_id": "5",
            }
        ]
        if observed_at
        else [{}],
    }


def make_existing_claim_row(
    *,
    claim_id: str = "claim:demo:existing",
    claim_text: str = "demo.app.main is disabled.",
    claim_kind: str = "state_claim",
    claim_subkind: str = "invariant_or_constraint",
    first_observed_at: str = "2026-03-25T00:00:00+00:00",
    latest_observed_at: str = "2026-03-25T00:00:00+00:00",
    about_record_ids: tuple[str, ...] = ("symbol:demo:demo.app.main@r1",),
    source_kind: str = "pull_request",
    source_id: str = "github:pr:demo:7",
    thread_kind: str = "pull_request",
    thread_id: str = "7",
    is_current: bool = True,
    valid_from: str | None = "2026-03-25T00:00:00+00:00",
    valid_to: str | None = None,
) -> dict[str, object]:
    canonical_key = canonical_claim_key(
        claim_kind=claim_kind,
        claim_text=claim_text,
        about_record_ids=about_record_ids,
    )
    return {
        "claim_id": claim_id,
        "claim_properties": {
            "claim_id": claim_id,
            "repo_id": "demo",
            "canonical_key": canonical_key,
            "claim_text": claim_text,
            "claim_kind": claim_kind,
            "claim_subkind": claim_subkind,
            "source_kind": source_kind,
            "source_id": source_id,
            "observation_id": "observation:github_pr_body:demo:5",
            "first_observed_at": first_observed_at,
            "latest_observed_at": latest_observed_at,
            "ingested_at": "2026-03-28T00:00:00Z",
            "valid_from": valid_from,
            "valid_to": valid_to,
            "thread_kind": thread_kind,
            "thread_id": thread_id,
            "is_current": is_current,
            "metadata_json": json.dumps(
                {
                    "assertion_status": "reported",
                    "claim_frame": "author_statement",
                }
            ),
        },
        "about_record_ids": list(about_record_ids),
        "evidence_record_ids": [],
    }


class FakeLLMClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def generate_response(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.response


class PromptMapLLMClient:
    def __init__(self, responses_by_prompt):
        self.responses_by_prompt = {
            key: list(value) if isinstance(value, list) else [value]
            for key, value in responses_by_prompt.items()
        }
        self.calls = []

    async def generate_response(self, messages, **kwargs):
        prompt_name = kwargs["prompt_name"]
        self.calls.append({"messages": messages, **kwargs})
        responses = self.responses_by_prompt[prompt_name]
        if not responses:
            raise AssertionError(f"no queued response for prompt {prompt_name}")
        response = responses.pop(0)
        if callable(response):
            return response(messages=messages, **kwargs)
        return response


class FakeBackendDriver:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []
        self.search = FakeBackendSearchOperations(self.rows)

    async def execute_query(self, query, **kwargs):
        self.calls.append({"query": query, "kwargs": kwargs})
        return list(self.rows)


class FakeBackendSearchOperations:
    def __init__(self, rows):
        self.rows = list(rows)
        self.search_queries = []

    async def search(self, query):
        self.search_queries.append(query)
        text = str(query.text or "").lower()
        query_tokens = {
            token.strip("`.,:;!?()[]{}")
            for token in text.split()
            if token.strip("`.,:;!?()[]{}")
        }
        hits: list[SearchHit] = []
        for row in self.rows:
            properties = dict(row.get("claim_properties") or {})
            if query.record_types and "claim" not in query.record_types:
                continue
            if "is_current" in query.filters:
                expected_current = bool(query.filters["is_current"])
                actual_current = bool(properties.get("is_current", True))
                if actual_current is not expected_current:
                    continue
            claim_id = str(properties.get("claim_id") or row.get("claim_id") or "").strip()
            claim_text = str(properties.get("claim_text") or "").strip()
            if not claim_id or not claim_text:
                continue
            claim_text_lower = claim_text.lower()
            overlap = sum(1 for token in query_tokens if token in claim_text_lower)
            if overlap <= 0:
                continue
            hits.append(
                SearchHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id=claim_id,
                        record_type="claim",
                        repo_id=str(properties.get("repo_id") or "demo"),
                        group_id=str(properties.get("repo_id") or "demo"),
                    ),
                    score=float(overlap),
                    lexical_score=float(overlap),
                )
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: query.limit]

    async def fetch(self, refs):
        records_by_id = {}
        for row in self.rows:
            properties = dict(row.get("claim_properties") or {})
            claim_id = str(properties.get("claim_id") or row.get("claim_id") or "").strip()
            if not claim_id:
                continue
            records_by_id[claim_id] = {
                "record_id": claim_id,
                "record_type": "claim",
                "properties": properties,
            }
        return [
            dict(records_by_id[ref.record_id])
            for ref in refs
            if ref.record_id in records_by_id
        ]


class FakeMetadataStore:
    def bootstrap(self):
        return None


class FakeSnapshotProducer:
    def __init__(self, snapshot: GraphSnapshot):
        self.snapshot = snapshot

    def build_snapshot(self, repo_path: str, *, repo_id: str, revision_id: str, limit: int):
        _ = repo_path, repo_id, revision_id, limit
        return self.snapshot


class FakeGitMetadata:
    def __init__(self, commit: CommitInfo):
        self.commit = commit

    def resolve_repo_root(self, repo_path: str) -> str:
        return repo_path

    def get_commit_info(self, repo_path: str, commit_sha: str | None = None, branch_hint: str | None = None):
        _ = repo_path, commit_sha, branch_hint
        return self.commit

    def is_first_parent_ancestor(self, repo_path: str, previous_commit_sha: str, target_commit_sha: str) -> bool:
        _ = repo_path, previous_commit_sha, target_commit_sha
        return True

    @contextmanager
    def temporary_worktree(self, repo_path: str, commit_sha: str):
        _ = commit_sha
        yield repo_path


class FakeProfileIndexer:
    def __init__(self) -> None:
        self.calls: list[tuple[ProfileRecord, ...]] = []

    def apply_profile_updates(self, *, profiles, deactivations=()):
        _ = deactivations
        materialized = tuple(profiles)
        self.calls.append(materialized)
        return len(materialized)


class FakeBackendLifecycleDriver:
    def __init__(self) -> None:
        self.mutations = self
        self.maintenance = self

    async def execute_query(self, query, **kwargs):
        _ = query, kwargs
        return []

    async def health_check(self):
        return None

    async def build_indices(self, delete_existing: bool = False):
        _ = delete_existing
        return None

    async def apply(self, plan):
        return type("MutationApplyResult", (), {"operations_applied": plan.operation_count()})()

    async def close(self):
        return None


class FakeEmbeddingProvider:
    def embed_texts(self, texts):
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append(
                [
                    1.0 if "secret_key" in lowered or "secret key" in lowered else 0.0,
                    1.0 if "auth.py" in lowered or "auth" in lowered else 0.0,
                    1.0 if "rotated" in lowered else 0.0,
                ]
            )
        return vectors


class FakeQueryParser:
    def __init__(self, keywords):
        self._keywords = tuple(keywords)

    def parse(self, query: str, llm_enabled: bool = True):
        _ = query, llm_enabled
        return QueryParseResult(
            raw_query=query,
            anchors=(),
            high_level_keywords=self._keywords,
            intent_tags=(),
        )


class AuthEmbeddingProvider:
    def embed_texts(self, texts):
        vectors = []
        for text in texts:
            lowered = text.lower()
            authish = 1.0 if "auth" in lowered or "authentication" in lowered else 0.0
            loginish = 1.0 if "login" in lowered or "session" in lowered else 0.0
            issueish = 1.0 if "token" in lowered or "credential" in lowered else 0.0
            vectors.append([authish, loginish, issueish])
        return vectors


class FakeGitHubApi:
    def __init__(
        self,
        *,
        commit_prs: list[dict[str, object]] | None = None,
        pull_request_payload: dict[str, object] | None = None,
        issue_payloads: dict[int, dict[str, object]] | None = None,
        review_comments: list[dict[str, object]] | None = None,
        issue_comments: list[dict[str, object]] | None = None,
        reviews: list[dict[str, object]] | None = None,
        linked_issue_numbers: tuple[int, ...] = (),
    ) -> None:
        self.commit_prs = list(commit_prs or [])
        self.pull_request_payload = dict(
            pull_request_payload
            or {
                "id": 700,
                "number": 7,
                "html_url": "https://github.com/octo/demo/pull/7",
                "title": "Enable main",
                "state": "open",
                "user": {"login": "octocat"},
                "created_at": "2026-03-26T00:00:00+00:00",
                "updated_at": "2026-03-26T00:01:00+00:00",
                "body": "demo.app.main is enabled",
            }
        )
        self.issue_payloads = {
            key: dict(value)
            for key, value in (issue_payloads or {}).items()
        }
        self.review_comments = list(review_comments or [])
        self.issue_comments = list(issue_comments or [])
        self.reviews = list(reviews or [])
        self.linked_issue_numbers = tuple(linked_issue_numbers)

    def list_commit_pull_requests(self, owner: str, repo: str, sha: str) -> list[dict[str, object]]:
        _ = owner, repo, sha
        return list(self.commit_prs)

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, object]:
        _ = owner, repo, number
        return dict(self.pull_request_payload)

    def list_pull_request_linked_issue_numbers(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> tuple[int, ...]:
        _ = owner, repo, number
        return self.linked_issue_numbers

    def list_pull_request_reviews(self, owner: str, repo: str, number: int) -> list[dict[str, object]]:
        _ = owner, repo, number
        return list(self.reviews)

    def list_pull_request_review_comments(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> list[dict[str, object]]:
        _ = owner, repo, number
        return list(self.review_comments)

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, object]]:
        _ = owner, repo, number
        return list(self.issue_comments)

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, object]:
        _ = owner, repo, number
        if number not in self.issue_payloads:
            raise AssertionError("linked issues are not expected in this test")
        return dict(self.issue_payloads[number])


class InstrumentedGitHubApi:
    def __init__(
        self,
        *,
        commit_prs_by_sha: dict[str, list[dict[str, object]]] | None = None,
        pull_requests: dict[int, dict[str, object]] | None = None,
        linked_issue_numbers: dict[int, tuple[int, ...]] | None = None,
        reviews: dict[int, list[dict[str, object]]] | None = None,
        review_comments: dict[int, list[dict[str, object]]] | None = None,
        issue_comments: dict[int, list[dict[str, object]]] | None = None,
        issues: dict[int, dict[str, object]] | None = None,
        gate_methods: set[str] | None = None,
    ) -> None:
        self.commit_prs_by_sha = {
            sha: [dict(item) for item in rows]
            for sha, rows in (commit_prs_by_sha or {}).items()
        }
        self.pull_requests = {
            number: dict(payload)
            for number, payload in (pull_requests or {}).items()
        }
        self.linked_issue_numbers = {
            number: tuple(values)
            for number, values in (linked_issue_numbers or {}).items()
        }
        self.reviews = {
            number: [dict(item) for item in rows]
            for number, rows in (reviews or {}).items()
        }
        self.review_comments = {
            number: [dict(item) for item in rows]
            for number, rows in (review_comments or {}).items()
        }
        self.issue_comments_map = {
            number: [dict(item) for item in rows]
            for number, rows in (issue_comments or {}).items()
        }
        self.issues = {
            number: dict(payload)
            for number, payload in (issues or {}).items()
        }
        self.call_counts: Counter[tuple[str, str | int]] = Counter()
        self.max_in_flight_by_method: dict[str, int] = {}
        self._in_flight_by_method: dict[str, int] = {}
        self._lock = threading.Lock()
        self._gate_methods = set(gate_methods or ())
        self._gate_events = {
            method: threading.Event()
            for method in self._gate_methods
        }

    def _begin_call(self, method: str, key: str | int) -> None:
        with self._lock:
            self.call_counts[(method, key)] += 1
            in_flight = self._in_flight_by_method.get(method, 0) + 1
            self._in_flight_by_method[method] = in_flight
            self.max_in_flight_by_method[method] = max(
                in_flight,
                self.max_in_flight_by_method.get(method, 0),
            )
            gate = self._gate_events.get(method)
            if gate is not None and in_flight >= 2:
                gate.set()
        if gate is not None:
            gate.wait(timeout=1.0)

    def _end_call(self, method: str) -> None:
        with self._lock:
            self._in_flight_by_method[method] = self._in_flight_by_method.get(method, 1) - 1

    def list_commit_pull_requests(self, owner: str, repo: str, sha: str) -> list[dict[str, object]]:
        _ = owner, repo
        self._begin_call("list_commit_pull_requests", sha)
        try:
            return [dict(item) for item in self.commit_prs_by_sha.get(sha, [])]
        finally:
            self._end_call("list_commit_pull_requests")

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, object]:
        _ = owner, repo
        self._begin_call("get_pull_request", number)
        try:
            return dict(self.pull_requests[number])
        finally:
            self._end_call("get_pull_request")

    def list_pull_request_linked_issue_numbers(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> tuple[int, ...]:
        _ = owner, repo
        self._begin_call("list_pull_request_linked_issue_numbers", number)
        try:
            return tuple(self.linked_issue_numbers.get(number, ()))
        finally:
            self._end_call("list_pull_request_linked_issue_numbers")

    def list_pull_request_reviews(self, owner: str, repo: str, number: int) -> list[dict[str, object]]:
        _ = owner, repo
        self._begin_call("list_pull_request_reviews", number)
        try:
            return [dict(item) for item in self.reviews.get(number, [])]
        finally:
            self._end_call("list_pull_request_reviews")

    def list_pull_request_review_comments(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> list[dict[str, object]]:
        _ = owner, repo
        self._begin_call("list_pull_request_review_comments", number)
        try:
            return [dict(item) for item in self.review_comments.get(number, [])]
        finally:
            self._end_call("list_pull_request_review_comments")

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, object]]:
        _ = owner, repo
        self._begin_call("list_issue_comments", number)
        try:
            return [dict(item) for item in self.issue_comments_map.get(number, [])]
        finally:
            self._end_call("list_issue_comments")

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, object]:
        _ = owner, repo
        self._begin_call("get_issue", number)
        try:
            return dict(self.issues[number])
        finally:
            self._end_call("get_issue")


class ObservationPlannerTests(unittest.TestCase):
    def test_commit_message_observation_emits_node_and_source_link_only(self):
        plan = ObservationMutationPlanner().plan_commit_message_observation(
            repo_id="demo",
            commit=make_commit(),
        )

        self.assertEqual(len(plan.upsert_nodes), 1)
        self.assertEqual(len(plan.upsert_edges), 1)
        self.assertEqual(len(plan.upsert_search_documents), 1)
        self.assertEqual(plan.upsert_nodes[0].record_type, "observation")
        self.assertEqual({edge.relationship_type for edge in plan.upsert_edges}, {"FROM_SOURCE"})
        self.assertIn("Add helper support", plan.upsert_search_documents[0].search_text)
        self.assertEqual(
            plan.upsert_search_documents[0].properties["observation_kind"],
            "git_commit_message",
        )

    def test_commit_message_observation_skips_empty_message(self):
        plan = ObservationMutationPlanner().plan_commit_message_observation(
            repo_id="demo",
            commit=make_commit(subject="", body=""),
        )

        self.assertTrue(plan.is_empty())

    def test_observation_emits_mentions_for_deterministic_symbol_matches(self):
        planned = ObservationMutationPlanner().build_github_observations(
            bundle=GitHubProvenanceBundle(
                repo_id="demo",
                github_repo="octo/demo",
                entities=(),
                project_links=(),
                commit_links=(),
                entity_links=(),
                observation_sources=(
                    GitHubObservationSource(
                        observation_kind=ObservationKind.GITHUB_PR_BODY,
                        source_kind="pull_request",
                        source_id="github:pr:demo:7",
                        source_record_type="github_pull_request",
                        external_id="700",
                        number=7,
                        url="https://github.com/octo/demo/pull/7",
                        title="Enable demo.app.main",
                        author_login="octocat",
                        created_at="2026-03-26T00:00:00+00:00",
                        updated_at="2026-03-26T00:01:00+00:00",
                        text="This PR updates demo.app.main after init_db().",
                        metadata={"pull_request_number": 7},
                    ),
                ),
            )
        )[0]

        plan = ObservationMutationPlanner().plan_observations(planned, snapshot=make_snapshot())

        mentions = {
            edge.target_record_id
            for edge in plan.upsert_edges
            if edge.relationship_type == "MENTIONS"
        }
        self.assertEqual(
            mentions,
            {
                "symbol:demo:demo.app.main",
            },
        )

    def test_observation_emits_mentions_for_metadata_file_paths(self):
        planned = ObservationMutationPlanner().build_commit_message_observation(
            repo_id="demo",
            commit=make_commit(subject="Refine startup order", body="No explicit file names here."),
        )

        plan = ObservationMutationPlanner().plan_observations(planned, snapshot=make_snapshot())

        mentions = {
            edge.target_record_id
            for edge in plan.upsert_edges
            if edge.relationship_type == "MENTIONS"
        }
        self.assertIn("file:demo:demo.app.__file__", mentions)

    def test_observation_mentions_fail_closed_for_ambiguous_file_basename(self):
        planned = ObservationMutationPlanner().build_github_observations(
            bundle=GitHubProvenanceBundle(
                repo_id="demo",
                github_repo="octo/demo",
                entities=(),
                project_links=(),
                commit_links=(),
                entity_links=(),
                observation_sources=(
                    GitHubObservationSource(
                        observation_kind=ObservationKind.GITHUB_PR_BODY,
                        source_kind="pull_request",
                        source_id="github:pr:demo:7",
                        source_record_type="github_pull_request",
                        external_id="700",
                        number=7,
                        url="https://github.com/octo/demo/pull/7",
                        title="Fix startup order",
                        author_login="octocat",
                        created_at="2026-03-26T00:00:00+00:00",
                        updated_at="2026-03-26T00:01:00+00:00",
                        text="This PR fixes main.py startup order.",
                        metadata={"pull_request_number": 7},
                    ),
                ),
            )
        )[0]

        plan = ObservationMutationPlanner().plan_observations(planned, snapshot=make_snapshot())

        mentions = {
            edge.target_record_id
            for edge in plan.upsert_edges
            if edge.relationship_type == "MENTIONS"
        }
        self.assertEqual(mentions, set())

    def test_github_observations_cover_bodies_reviews_and_comments(self):
        bundle = GitHubProvenanceBundle(
            repo_id="demo",
            github_repo="octo/demo",
            entities=(),
            project_links=(),
            commit_links=(),
            entity_links=(),
            observation_sources=(
                GitHubObservationSource(
                    observation_kind=ObservationKind.GITHUB_PR_BODY,
                    source_kind="pull_request",
                    source_id="github:pr:demo:7",
                    source_record_type="github_pull_request",
                    external_id="700",
                    number=7,
                    url="https://github.com/octo/demo/pull/7",
                    title="Tighten rollback handling",
                    author_login="octocat",
                    created_at="2026-03-26T00:00:00+00:00",
                    updated_at="2026-03-26T00:01:00+00:00",
                    text="This PR tightens rollback handling.",
                    metadata={"pull_request_number": 7},
                ),
                GitHubObservationSource(
                    observation_kind=ObservationKind.GITHUB_PR_REVIEW,
                    source_kind="pull_request",
                    source_id="github:pr:demo:7",
                    source_record_type="github_pull_request",
                    external_id="800",
                    number=7,
                    url="https://github.com/octo/demo/pull/7#pullrequestreview-800",
                    title="Review on PR #7: Tighten rollback handling",
                    author_login="reviewer",
                    created_at="2026-03-26T00:02:00+00:00",
                    updated_at="2026-03-26T00:02:00+00:00",
                    text="Please add a regression test.",
                    metadata={"review_id": 800},
                ),
                GitHubObservationSource(
                    observation_kind=ObservationKind.GITHUB_PR_REVIEW_COMMENT,
                    source_kind="pull_request",
                    source_id="github:pr:demo:7",
                    source_record_type="github_pull_request",
                    external_id="801",
                    number=7,
                    url="https://github.com/octo/demo/pull/7#discussion_r801",
                    title="Review comment on PR #7: Tighten rollback handling",
                    author_login="reviewer",
                    created_at="2026-03-26T00:03:00+00:00",
                    updated_at="2026-03-26T00:03:00+00:00",
                    text="Can this branch be flattened?",
                    metadata={"comment_id": 801, "path": "app.py"},
                ),
                GitHubObservationSource(
                    observation_kind=ObservationKind.GITHUB_ISSUE_COMMENT,
                    source_kind="issue",
                    source_id="github:issue:demo:9",
                    source_record_type="github_issue",
                    external_id="901",
                    number=9,
                    url="https://github.com/octo/demo/issues/9#issuecomment-901",
                    title="Comment on issue #9: Rollback bug",
                    author_login="octocat",
                    created_at="2026-03-26T00:04:00+00:00",
                    updated_at="2026-03-26T00:04:00+00:00",
                    text="Still reproduces on the latest build.",
                    metadata={"comment_id": 901},
                ),
                GitHubObservationSource(
                    observation_kind=ObservationKind.GITHUB_ISSUE_BODY,
                    source_kind="issue",
                    source_id="github:issue:demo:9",
                    source_record_type="github_issue",
                    external_id="900",
                    number=9,
                    url="https://github.com/octo/demo/issues/9",
                    title="Rollback bug",
                    author_login="octocat",
                    created_at="2026-03-26T00:00:00+00:00",
                    updated_at="2026-03-26T00:01:00+00:00",
                    text="",
                    metadata={"issue_number": 9},
                ),
            ),
        )

        planner = ObservationMutationPlanner()
        first = planner.plan_github_observations(bundle=bundle)
        second = planner.plan_github_observations(bundle=bundle)

        self.assertEqual(first.operation_count(), second.operation_count())
        self.assertEqual(len(first.upsert_nodes), 4)
        self.assertEqual(len(first.upsert_edges), 4)
        self.assertEqual(len(first.upsert_search_documents), 4)
        self.assertEqual(
            {node.properties["observation_kind"] for node in first.upsert_nodes},
            {
                "github_pr_body",
                "github_pr_review",
                "github_pr_review_comment",
                "github_issue_comment",
            },
        )
        self.assertEqual({edge.relationship_type for edge in first.upsert_edges}, {"FROM_SOURCE"})

    def test_github_bundle_includes_pull_request_issue_comments_as_observations(self):
        github_api = FakeGitHubApi(
            commit_prs=[{"number": 2, "id": 2002}],
            pull_request_payload={
                "id": 2002,
                "number": 2,
                "html_url": "https://github.com/octo/demo/pull/2",
                "title": "Rotate SECRET_KEY",
                "state": "closed",
                "user": {"login": "octocat"},
                "created_at": "2026-03-27T20:58:00Z",
                "updated_at": "2026-03-27T20:58:13Z",
                "body": "Rotate SECRET_KEY in auth.py",
            },
            issue_comments=[
                {
                    "id": 4145222468,
                    "html_url": "https://github.com/octo/demo/pull/2#issuecomment-4145222468",
                    "user": {"login": "octocat"},
                    "created_at": "2026-03-27T20:58:11Z",
                    "updated_at": "2026-03-27T20:58:11Z",
                    "body": "Correction: APP_PORT in config.py should be 9000.",
                }
            ],
        )
        indexer = GitHubIndexer(github_api=github_api, backend_driver=None)

        bundle = indexer._build_bundle(
            repo_id="demo",
            repo_ref=GitHubRepoRef(
                owner="octo",
                name="demo",
                canonical_repo="octo/demo",
                clone_url="https://github.com/octo/demo.git",
                html_url="https://github.com/octo/demo",
                default_branch="main",
                source="github_url",
            ),
            commit_shas=("merge-sha",),
        )

        comment_observations = [
            source
            for source in bundle.observation_sources
            if source.observation_kind == ObservationKind.GITHUB_ISSUE_COMMENT
        ]
        self.assertEqual(len(comment_observations), 1)
        self.assertEqual(comment_observations[0].source_kind, "pull_request")
        self.assertEqual(comment_observations[0].source_id, "github:pr:demo:2")
        self.assertEqual(comment_observations[0].metadata["pull_request_number"], 2)
        self.assertEqual(comment_observations[0].metadata["comment_id"], 4145222468)
        self.assertEqual(comment_observations[0].metadata["anchor_revision_id"], "merge-sha")

    def test_github_bundle_dedupes_pr_fetches_across_multiple_commits(self):
        github_api = InstrumentedGitHubApi(
            commit_prs_by_sha={
                "sha-1": [{"number": 7, "id": 700}],
                "sha-2": [{"number": 7, "id": 700}],
            },
            pull_requests={
                7: {
                    "id": 700,
                    "number": 7,
                    "html_url": "https://github.com/octo/demo/pull/7",
                    "title": "Enable main",
                    "state": "open",
                    "user": {"login": "octocat"},
                    "created_at": "2026-03-26T00:00:00Z",
                    "updated_at": "2026-03-26T00:01:00Z",
                    "body": "demo.app.main is enabled",
                }
            },
            linked_issue_numbers={7: (42,)},
            reviews={
                7: [
                    {
                        "id": 701,
                        "state": "APPROVED",
                        "user": {"login": "reviewer"},
                        "submitted_at": "2026-03-26T00:02:00Z",
                        "body": "Looks good",
                        "commit_id": "sha-2",
                    }
                ]
            },
            review_comments={
                7: [
                    {
                        "id": 702,
                        "html_url": "https://github.com/octo/demo/pull/7#discussion_r1",
                        "user": {"login": "reviewer"},
                        "created_at": "2026-03-26T00:02:30Z",
                        "updated_at": "2026-03-26T00:02:30Z",
                        "body": "Please rename this",
                        "path": "app.py",
                        "commit_id": "sha-2",
                        "pull_request_review_id": 701,
                    }
                ]
            },
            issue_comments={
                7: [
                    {
                        "id": 703,
                        "html_url": "https://github.com/octo/demo/issues/7#issuecomment-1",
                        "user": {"login": "commenter"},
                        "created_at": "2026-03-26T00:03:00Z",
                        "updated_at": "2026-03-26T00:03:00Z",
                        "body": "Ship it",
                    }
                ],
                42: [],
            },
            issues={
                42: {
                    "id": 4200,
                    "number": 42,
                    "html_url": "https://github.com/octo/demo/issues/42",
                    "title": "Track rollout",
                    "state": "open",
                    "user": {"login": "issue-author"},
                    "created_at": "2026-03-26T00:00:00Z",
                    "updated_at": "2026-03-26T00:05:00Z",
                    "body": "Issue body",
                    "closed_at": None,
                }
            },
        )
        indexer = GitHubIndexer(github_api=github_api, backend_driver=None)

        bundle = indexer._build_bundle(
            repo_id="demo",
            repo_ref=GitHubRepoRef(
                owner="octo",
                name="demo",
                canonical_repo="octo/demo",
                clone_url="https://github.com/octo/demo.git",
                html_url="https://github.com/octo/demo",
                default_branch="main",
                source="github_url",
            ),
            commit_shas=("sha-1", "sha-2"),
        )

        self.assertEqual(github_api.call_counts[("get_pull_request", 7)], 1)
        self.assertEqual(github_api.call_counts[("list_pull_request_linked_issue_numbers", 7)], 1)
        self.assertEqual(github_api.call_counts[("list_pull_request_reviews", 7)], 1)
        self.assertEqual(github_api.call_counts[("list_pull_request_review_comments", 7)], 1)
        self.assertEqual(github_api.call_counts[("list_issue_comments", 7)], 1)
        self.assertEqual(github_api.call_counts[("get_issue", 42)], 1)
        pr_body = next(
            source
            for source in bundle.observation_sources
            if source.observation_kind == ObservationKind.GITHUB_PR_BODY
        )
        self.assertEqual(pr_body.metadata["anchor_revision_id"], "sha-2")

    def test_github_bundle_dedupes_shared_linked_issue_fetches_across_pull_requests(self):
        github_api = InstrumentedGitHubApi(
            commit_prs_by_sha={
                "merge-sha": [{"number": 2, "id": 2002}, {"number": 3, "id": 2003}],
            },
            pull_requests={
                2: {
                    "id": 2002,
                    "number": 2,
                    "html_url": "https://github.com/octo/demo/pull/2",
                    "title": "Rotate secret",
                    "state": "closed",
                    "user": {"login": "octocat"},
                    "created_at": "2026-03-27T20:58:00Z",
                    "updated_at": "2026-03-27T20:58:13Z",
                    "body": "Rotate SECRET_KEY in auth.py",
                },
                3: {
                    "id": 2003,
                    "number": 3,
                    "html_url": "https://github.com/octo/demo/pull/3",
                    "title": "Add follow-up checks",
                    "state": "open",
                    "user": {"login": "octocat"},
                    "created_at": "2026-03-27T21:00:00Z",
                    "updated_at": "2026-03-27T21:01:00Z",
                    "body": "Adds rollout validation",
                },
            },
            linked_issue_numbers={
                2: (42,),
                3: (42,),
            },
            issue_comments={
                2: [],
                3: [],
                42: [
                    {
                        "id": 4145222468,
                        "html_url": "https://github.com/octo/demo/issues/42#issuecomment-4145222468",
                        "user": {"login": "octocat"},
                        "created_at": "2026-03-27T20:58:30Z",
                        "updated_at": "2026-03-27T20:58:30Z",
                        "body": "The rotation shipped cleanly.",
                    }
                ],
            },
            issues={
                42: {
                    "id": 4200,
                    "number": 42,
                    "html_url": "https://github.com/octo/demo/issues/42",
                    "title": "Track SECRET_KEY rollout",
                    "state": "open",
                    "user": {"login": "issue-author"},
                    "created_at": "2026-03-27T20:57:00Z",
                    "updated_at": "2026-03-27T20:57:00Z",
                    "body": "Issue body",
                    "closed_at": None,
                }
            },
        )
        indexer = GitHubIndexer(github_api=github_api, backend_driver=None)

        bundle = indexer._build_bundle(
            repo_id="demo",
            repo_ref=GitHubRepoRef(
                owner="octo",
                name="demo",
                canonical_repo="octo/demo",
                clone_url="https://github.com/octo/demo.git",
                html_url="https://github.com/octo/demo",
                default_branch="main",
                source="github_url",
            ),
            commit_shas=("merge-sha",),
        )

        self.assertEqual(github_api.call_counts[("get_issue", 42)], 1)
        self.assertEqual(github_api.call_counts[("list_issue_comments", 42)], 1)
        issue_entities = [
            entity
            for entity in bundle.entities
            if entity.kind == "Issue" and entity.number == 42
        ]
        self.assertEqual(len(issue_entities), 1)
        issue_body_observations = [
            source
            for source in bundle.observation_sources
            if source.observation_kind == ObservationKind.GITHUB_ISSUE_BODY and source.number == 42
        ]
        self.assertEqual(len(issue_body_observations), 1)

    def test_github_bundle_parallelizes_commit_pull_request_fetches(self):
        github_api = InstrumentedGitHubApi(
            commit_prs_by_sha={
                "sha-1": [{"number": 2, "id": 2002}],
                "sha-2": [{"number": 3, "id": 2003}],
            },
            pull_requests={
                2: {
                    "id": 2002,
                    "number": 2,
                    "html_url": "https://github.com/octo/demo/pull/2",
                    "title": "Rotate secret",
                    "state": "closed",
                    "user": {"login": "octocat"},
                    "created_at": "2026-03-27T20:58:00Z",
                    "updated_at": "2026-03-27T20:58:13Z",
                    "body": "Rotate SECRET_KEY in auth.py",
                },
                3: {
                    "id": 2003,
                    "number": 3,
                    "html_url": "https://github.com/octo/demo/pull/3",
                    "title": "Add follow-up checks",
                    "state": "open",
                    "user": {"login": "octocat"},
                    "created_at": "2026-03-27T21:00:00Z",
                    "updated_at": "2026-03-27T21:01:00Z",
                    "body": "Adds rollout validation",
                },
            },
            gate_methods={"list_commit_pull_requests"},
        )
        indexer = GitHubIndexer(github_api=github_api, backend_driver=None)

        bundle = indexer._build_bundle(
            repo_id="demo",
            repo_ref=GitHubRepoRef(
                owner="octo",
                name="demo",
                canonical_repo="octo/demo",
                clone_url="https://github.com/octo/demo.git",
                html_url="https://github.com/octo/demo",
                default_branch="main",
                source="github_url",
            ),
            commit_shas=("sha-1", "sha-2"),
        )

        self.assertGreaterEqual(github_api.max_in_flight_by_method.get("list_commit_pull_requests", 0), 2)
        self.assertEqual(
            {entity.number for entity in bundle.entities if entity.kind == "PullRequest"},
            {2, 3},
        )

    def test_github_bundle_orders_observations_chronologically_and_includes_issue_closed(self):
        github_api = FakeGitHubApi(
            commit_prs=[{"number": 2, "id": 2002}],
            pull_request_payload={
                "id": 2002,
                "number": 2,
                "html_url": "https://github.com/octo/demo/pull/2",
                "title": "Rotate SECRET_KEY",
                "state": "closed",
                "user": {"login": "octocat"},
                "created_at": "2026-03-27T20:58:00Z",
                "updated_at": "2026-03-27T20:58:00Z",
                "body": "Rotate SECRET_KEY in auth.py",
            },
            linked_issue_numbers=(42,),
            issue_payloads={
                42: {
                    "id": 4200,
                    "number": 42,
                    "html_url": "https://github.com/octo/demo/issues/42",
                    "title": "Track SECRET_KEY rollout",
                    "state": "closed",
                    "user": {"login": "issue-author"},
                    "created_at": "2026-03-27T20:57:00Z",
                    "updated_at": "2026-03-27T20:57:00Z",
                    "closed_at": "2026-03-27T20:59:00Z",
                    "closed_by": {"login": "maintainer"},
                    "state_reason": "completed",
                    "body": "Issue body",
                }
            },
            issue_comments=[
                {
                    "id": 4145222468,
                    "html_url": "https://github.com/octo/demo/issues/42#issuecomment-4145222468",
                    "user": {"login": "octocat"},
                    "created_at": "2026-03-27T20:58:30Z",
                    "updated_at": "2026-03-27T20:58:30Z",
                    "body": "The rotation shipped cleanly.",
                }
            ],
        )
        indexer = GitHubIndexer(github_api=github_api, backend_driver=None)

        bundle = indexer._build_bundle(
            repo_id="demo",
            repo_ref=GitHubRepoRef(
                owner="octo",
                name="demo",
                canonical_repo="octo/demo",
                clone_url="https://github.com/octo/demo.git",
                html_url="https://github.com/octo/demo",
                default_branch="main",
                source="github_url",
            ),
            commit_shas=("merge-sha",),
        )

        ordered_kinds = [source.observation_kind.value for source in bundle.observation_sources]
        self.assertEqual(
            ordered_kinds,
            [
                "github_issue_body",
                "github_pr_body",
                "github_issue_comment",
                "github_issue_closed",
            ],
        )
        close_source = next(
            source
            for source in bundle.observation_sources
            if source.observation_kind == ObservationKind.GITHUB_ISSUE_CLOSED
        )
        self.assertEqual(close_source.created_at, "2026-03-27T20:59:00Z")
        self.assertEqual(close_source.metadata["issue_number"], 42)
        self.assertEqual(close_source.metadata["closed_by"], "maintainer")
        self.assertEqual(close_source.metadata["state_reason"], "completed")

    def test_github_bundle_parallel_fetch_preserves_entities_links_and_observation_order(self):
        github_api = InstrumentedGitHubApi(
            commit_prs_by_sha={
                "sha-1": [{"number": 2, "id": 2002}],
                "sha-2": [{"number": 2, "id": 2002}, {"number": 3, "id": 2003}],
            },
            pull_requests={
                2: {
                    "id": 2002,
                    "number": 2,
                    "html_url": "https://github.com/octo/demo/pull/2",
                    "title": "Rotate secret",
                    "state": "closed",
                    "user": {"login": "octocat"},
                    "created_at": "2026-03-27T20:58:00Z",
                    "updated_at": "2026-03-27T20:58:00Z",
                    "body": "Rotate SECRET_KEY in auth.py",
                },
                3: {
                    "id": 2003,
                    "number": 3,
                    "html_url": "https://github.com/octo/demo/pull/3",
                    "title": "Add follow-up checks",
                    "state": "open",
                    "user": {"login": "octocat"},
                    "created_at": "2026-03-27T21:00:00Z",
                    "updated_at": "2026-03-27T21:00:00Z",
                    "body": "Adds rollout validation",
                },
            },
            linked_issue_numbers={
                2: (42,),
                3: (99,),
            },
            review_comments={
                2: [
                    {
                        "id": 702,
                        "html_url": "https://github.com/octo/demo/pull/2#discussion_r1",
                        "user": {"login": "reviewer"},
                        "created_at": "2026-03-27T20:58:20Z",
                        "updated_at": "2026-03-27T20:58:20Z",
                        "body": "Please rename this",
                        "path": "app.py",
                        "commit_id": "sha-2",
                        "pull_request_review_id": 701,
                    }
                ],
            },
            issue_comments={
                2: [
                    {
                        "id": 703,
                        "html_url": "https://github.com/octo/demo/issues/2#issuecomment-1",
                        "user": {"login": "commenter"},
                        "created_at": "2026-03-27T20:58:30Z",
                        "updated_at": "2026-03-27T20:58:30Z",
                        "body": "Ship it",
                    }
                ],
                3: [],
                42: [
                    {
                        "id": 704,
                        "html_url": "https://github.com/octo/demo/issues/42#issuecomment-2",
                        "user": {"login": "issue-user"},
                        "created_at": "2026-03-27T20:58:40Z",
                        "updated_at": "2026-03-27T20:58:40Z",
                        "body": "Issue discussion",
                    }
                ],
                99: [
                    {
                        "id": 705,
                        "html_url": "https://github.com/octo/demo/issues/99#issuecomment-3",
                        "user": {"login": "issue-user"},
                        "created_at": "2026-03-27T21:00:30Z",
                        "updated_at": "2026-03-27T21:00:30Z",
                        "body": "Issue discussion",
                    }
                ],
            },
            issues={
                42: {
                    "id": 4200,
                    "number": 42,
                    "html_url": "https://github.com/octo/demo/issues/42",
                    "title": "Track rollout",
                    "state": "open",
                    "user": {"login": "issue-author"},
                    "created_at": "2026-03-27T20:57:00Z",
                    "updated_at": "2026-03-27T20:57:00Z",
                    "body": "Issue body",
                    "closed_at": None,
                },
                99: {
                    "id": 9900,
                    "number": 99,
                    "html_url": "https://github.com/octo/demo/issues/99",
                    "title": "Follow-up validation",
                    "state": "open",
                    "user": {"login": "issue-author"},
                    "created_at": "2026-03-27T20:59:30Z",
                    "updated_at": "2026-03-27T20:59:30Z",
                    "body": "Need more checks",
                    "closed_at": None,
                },
            },
        )
        indexer = GitHubIndexer(github_api=github_api, backend_driver=None)

        bundle = indexer._build_bundle(
            repo_id="demo",
            repo_ref=GitHubRepoRef(
                owner="octo",
                name="demo",
                canonical_repo="octo/demo",
                clone_url="https://github.com/octo/demo.git",
                html_url="https://github.com/octo/demo",
                default_branch="main",
                source="github_url",
            ),
            commit_shas=("sha-1", "sha-2"),
        )

        self.assertEqual(
            {(entity.kind, entity.number) for entity in bundle.entities},
            {
                ("PullRequest", 2),
                ("PullRequest", 3),
                ("Issue", 42),
                ("Issue", 99),
            },
        )
        self.assertEqual(
            {
                (link.relation_type, link.commit_sha, link.target_node_id)
                for link in bundle.commit_links
            },
            {
                ("ASSOCIATED_WITH_PR", "sha-1", "github:pr:demo:2"),
                ("ASSOCIATED_WITH_PR", "sha-2", "github:pr:demo:2"),
                ("ASSOCIATED_WITH_PR", "sha-2", "github:pr:demo:3"),
                ("REFERENCES_ISSUE", "sha-1", "github:issue:demo:42"),
                ("REFERENCES_ISSUE", "sha-2", "github:issue:demo:42"),
                ("REFERENCES_ISSUE", "sha-2", "github:issue:demo:99"),
            },
        )
        self.assertEqual(
            [(source.observation_kind.value, source.number) for source in bundle.observation_sources],
            [
                ("github_issue_body", 42),
                ("github_pr_body", 2),
                ("github_pr_review_comment", 2),
                ("github_issue_comment", 2),
                ("github_issue_comment", 42),
                ("github_issue_body", 99),
                ("github_pr_body", 3),
                ("github_issue_comment", 99),
            ],
        )

    def test_issue_closed_observations_persist_ingested_and_thread_fields(self):
        planned = ObservationMutationPlanner().build_github_observations(
            bundle=GitHubProvenanceBundle(
                repo_id="demo",
                github_repo="octo/demo",
                entities=(),
                project_links=(),
                commit_links=(),
                entity_links=(),
                observation_sources=(
                    GitHubObservationSource(
                        observation_kind=ObservationKind.GITHUB_ISSUE_CLOSED,
                        source_kind="issue",
                        source_id="github:issue:demo:42",
                        source_record_type="github_issue",
                        external_id="4200:closed",
                        number=42,
                        url="https://github.com/octo/demo/issues/42",
                        title="Issue #42 closed: Track SECRET_KEY rollout",
                        author_login="maintainer",
                        created_at="2026-03-27T20:59:00Z",
                        updated_at="2026-03-27T20:59:00Z",
                        text="Issue #42 was closed. State reason: completed.",
                        metadata={"issue_number": 42, "closed_at": "2026-03-27T20:59:00Z"},
                    ),
                ),
            )
        )[0]

        plan = ObservationMutationPlanner().plan_observations(planned)

        node = plan.upsert_nodes[0]
        self.assertEqual(node.properties["observation_kind"], "github_issue_closed")
        self.assertEqual(node.properties["thread_kind"], "issue")
        self.assertEqual(node.properties["thread_id"], "42")
        self.assertTrue(node.properties["ingested_at"])


class ObservationGeneralizationTests(unittest.TestCase):
    def test_registry_exposes_markdown_and_agent_trace_policies(self):
        markdown_policy = DEFAULT_PROVENANCE_SCHEMA_REGISTRY.observation_policy(
            ObservationKind.MARKDOWN_DOCUMENT.value,
            source_kind="document",
        )
        trace_policy = DEFAULT_PROVENANCE_SCHEMA_REGISTRY.observation_policy(
            ObservationKind.AGENT_TRACE.value,
            source_kind="agent",
        )

        self.assertIsNotNone(markdown_policy)
        self.assertEqual(markdown_policy.extraction_mode, "generic_claims")
        self.assertEqual(markdown_policy.prompt_profile, "generic_observation")
        self.assertEqual(markdown_policy.default_context_kind, "document")
        self.assertIsNotNone(trace_policy)
        self.assertEqual(trace_policy.extraction_mode, "observation_only")
        self.assertIsNone(trace_policy.prompt_profile)

    def test_markdown_observation_uses_generic_prompt_profile(self):
        observation = Observation(
            observation_id="observation:markdown_document:demo:1",
            repo_id="demo",
            observation_kind=ObservationKind.MARKDOWN_DOCUMENT.value,
            source_kind="document",
            source_id="file:demo:docs.readme.__file__",
            observed_at="2026-03-29T00:00:00Z",
            author=None,
            title="Authentication Overview",
            text="# Authentication Overview\n\nAuth rotates tokens after refresh.",
            metadata={"document_path": "docs/README.md"},
            context_kind="document",
            context_id="docs/README.md",
        )

        request = ClaimEvidenceExtractionService().build_request(observation)

        self.assertEqual(
            request.prompt_name,
            "provenance.extract_claims_and_evidence.from_generic_observation.v1",
        )

    def test_markdown_adapter_builds_document_observation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            docs_dir = Path(temp_dir) / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)
            markdown_path = docs_dir / "README.md"
            markdown_path.write_text(
                "# Authentication Notes\n\nAuth rotates refresh tokens daily.\n",
                encoding="utf-8",
            )
            snapshot = GraphSnapshot(
                repo_id="demo",
                revision_id="rev-1",
                root_path=temp_dir,
                generated_at="2026-03-29T00:00:00Z",
                nodes=(
                    CanonicalNode(
                        "file:demo:docs.readme.__file__",
                        "demo",
                        "File",
                        "README.md",
                        qualified_name="demo.docs.readme.__file__",
                        file_path="docs/README.md",
                    ),
                ),
                edges=(),
                metadata={},
            )

            planned = MarkdownObservationAdapter().build_observations(
                planner=ObservationMutationPlanner(),
                snapshot=snapshot,
                checkout_root=temp_dir,
            )

        self.assertEqual(len(planned), 1)
        observation = planned[0].observation
        self.assertEqual(observation.observation_kind, "markdown_document")
        self.assertEqual(observation.source_kind, "document")
        self.assertEqual(observation.context_kind, "document")
        self.assertEqual(observation.context_id, "docs/README.md")
        self.assertEqual(planned[0].about_links[0].target_record_id, "file:demo:docs.readme.__file__")

    def test_observation_ingestion_service_skips_claim_extraction_for_stack_trace(self):
        class RecordingExtractionService:
            def __init__(self) -> None:
                self.calls = 0

            def extract(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("stack traces should not trigger extraction")

        class GuardedInvalidationPlanner:
            def begin_claim_session(self, *, repo_id: str):
                return object()

            def plan_validated_claims(self, **kwargs):
                raise AssertionError("stack traces should not produce claim mutations")

        planned = ObservationMutationPlanner().build_observation(
            observation_id="observation:stack_trace:demo:1",
            repo_id="demo",
            observation_kind=ObservationKind.STACK_TRACE.value,
            source_kind="agent",
            source_id="observation_source:agent:demo:task-1",
            source_record_type="observation_source",
            title="Null pointer in auth flow",
            text="Traceback ... AuthService.login ... NullPointerException",
            context_kind="agent_task",
            context_id="task-1",
            ensure_source_node=True,
        )
        extraction_service = RecordingExtractionService()
        result = ObservationIngestionService(
            metadata_store=InMemoryMetadataStore(),
            observation_planner=ObservationMutationPlanner(),
            claim_extraction_service=extraction_service,
            evidence_invalidation_planner=GuardedInvalidationPlanner(),
        ).plan_ingestion(
            items=(ObservationIngestionItem(planned_observation=planned),),
            snapshot=None,
        )

        self.assertEqual(extraction_service.calls, 0)
        self.assertFalse(result.observation_plan.is_empty())
        self.assertTrue(result.evidence_plan.is_empty())
        self.assertEqual(len(result.profiles), 1)
        self.assertEqual(result.profiles[0].profile_kind, SEMANTIC_OBSERVATION_PROFILE_KIND)

    def test_observation_ingestion_service_deactivates_active_relation_profiles_for_invalidated_claims(self):
        metadata = InMemoryMetadataStore()
        active_relation_profile = semantic_relation_profile_from_spec(
            RelationProfileSpec(
                repo_id="demo",
                profile_target_id="claim_relation:claim:new:claim:old:contradicts",
                relation_type="contradicts",
                source_record_id="claim:new",
                source_record_type="claim",
                target_record_id="claim:old",
                target_record_type="claim",
                value_text="The new claim contradicts the older claim.",
                updated_at="2026-03-28T00:00:00Z",
            )
        )
        assert active_relation_profile is not None
        metadata.upsert_profiles((active_relation_profile,))

        class ObservationOnlyExtractionService:
            def extract(self, *args, **kwargs):
                return make_extraction_response(
                    claim_text="demo.app.main is enabled.",
                    claim_kind="state_claim",
                    quote_text="demo.app.main is enabled",
                    summary_text="The source says demo.app.main is enabled.",
                    span_start=None,
                    span_end=None,
                    evidence_kind="state_detail",
                )

        class InvalidatingPlanner:
            def begin_claim_session(self, *, repo_id: str):
                return object()

            def plan_validated_claims(self, **kwargs):
                return SimpleNamespace(
                    plan=MutationPlan(),
                    invalidated_count=1,
                    invalidated_claim_ids=("claim:old",),
                    profiles=(),
                    profile_deactivations=(),
                )

        planned = ObservationMutationPlanner().build_observation(
            observation_id="observation:agent_note:demo:1",
            repo_id="demo",
            observation_kind=ObservationKind.AGENT_NOTE.value,
            source_kind="agent",
            source_id="observation_source:agent:demo:task-1",
            source_record_type="observation_source",
            title="Auth contradiction",
            text="The new note says the old auth claim is incorrect.",
            context_kind="agent_task",
            context_id="task-1",
            observed_at="2026-03-29T00:00:00Z",
            ensure_source_node=True,
        )
        service = ObservationIngestionService(
            metadata_store=metadata,
            observation_planner=ObservationMutationPlanner(),
            claim_extraction_service=ObservationOnlyExtractionService(),
            evidence_invalidation_planner=InvalidatingPlanner(),
        )

        result = service.plan_ingestion(
            items=(ObservationIngestionItem(planned_observation=planned),),
            snapshot=None,
        )

        self.assertEqual(result.invalidated_claim_ids, ("claim:old",))
        self.assertEqual(len(result.profile_deactivations), 1)
        deactivation = result.profile_deactivations[0]
        self.assertEqual(deactivation.profile_kind, SEMANTIC_RELATION_PROFILE_KIND)
        self.assertEqual(deactivation.target_node_id, "claim:old")
        self.assertEqual(deactivation.observed_at, "2026-03-29T00:00:00Z")
        relation_edge_update = next(
            edge for edge in result.plan.upsert_edges
            if edge.record_id == "claim_relation:claim:new:claim:old:contradicts"
        )
        self.assertFalse(relation_edge_update.properties["is_active"])
        relation_search_doc = next(
            doc
            for doc in result.plan.upsert_search_documents
            if doc.record_kind is RecordKind.EDGE
            and doc.record_id == "claim_relation:claim:new:claim:old:contradicts"
        )
        self.assertFalse(relation_search_doc.properties["is_active"])

        service.persist_profiles(result.profiles, deactivations=result.profile_deactivations)
        stored_relation_profile = next(
            profile
            for profile in metadata.load_profiles("demo", active_only=False)
            if profile.profile_kind == SEMANTIC_RELATION_PROFILE_KIND
        )
        self.assertFalse(stored_relation_profile.is_active)


class CodeIdentityTests(unittest.TestCase):
    def test_canonicalizer_preserves_stable_node_and_edge_ids(self):
        canonicalizer = CodeIdentityCanonicalizer()

        self.assertEqual(
            canonicalizer.stable_node_id(
                "demo",
                kind="Project",
                qualified_name="demo",
                file_path=None,
                name="demo",
            ),
            "project:demo",
        )
        self.assertEqual(
            canonicalizer.stable_node_id(
                "demo",
                kind="File",
                qualified_name="demo.app.__file__",
                file_path="app.py",
                name="app.py",
            ),
            "file:demo:demo.app.__file__",
        )
        self.assertEqual(
            canonicalizer.stable_node_id(
                "demo",
                kind="Folder",
                qualified_name="demo.src.auth",
                file_path=None,
                name="auth",
            ),
            "folder:demo:demo.src.auth",
        )
        self.assertEqual(
            canonicalizer.stable_node_id(
                "demo",
                kind="Function",
                qualified_name="demo.app.main",
                file_path="app.py",
                name="main",
            ),
            "function:demo:demo.app.main",
        )
        self.assertEqual(
            canonicalizer.stable_edge_id(
                "demo",
                "DEFINES",
                "file:demo:demo.app.__file__",
                "function:demo:demo.app.main",
            ),
            "edge:demo:DEFINES:file:demo:demo.app.__file__:function:demo:demo.app.main",
        )

    def test_snapshot_resolver_returns_changed_file_and_all_ancestor_folders(self):
        snapshot = make_tree_snapshot()

        resolved = SnapshotCodeResolver(snapshot).resolve_changed_files(("src/auth/login.py",))

        self.assertEqual(
            {node.node_id for node in resolved},
            {
                "file:demo:demo.src.auth.login.__file__",
                "folder:demo:demo.src",
                "folder:demo:demo.src.auth",
            },
        )

    def test_grounding_wrapper_matches_shared_resolver_behavior(self):
        snapshot = make_snapshot()
        resolver = SnapshotCodeResolver(snapshot)

        self.assertEqual(
            resolve_about_record_ids(snapshot, ["demo.app.main", "demo.app.main"]),
            resolver.resolve_about_record_ids(["demo.app.main", "demo.app.main"]),
        )

    def test_observation_grounding_wrapper_returns_stable_node_ids(self):
        snapshot = make_snapshot()
        resolver = SnapshotCodeResolver(snapshot)

        self.assertEqual(
            resolve_mention_record_ids(snapshot, ["app.py", "demo.app.main"]),
            tuple(node.node_id for node in resolver.resolve_about_hints(["app.py", "demo.app.main"])),
        )

    def test_grounding_resolves_max_retries_summary_to_variable(self):
        observation = make_observation(text="Correction: `MAX_RETRIES` in `tasks.py` changed.")
        evidence = EvidenceCandidate.model_validate(
            {
                "evidence_kind": "change_summary",
                "quote_text": "MAX_RETRIES changed",
                "summary_text": "Change made to `MAX_RETRIES` due to alignment with timeout.",
                "span_start": None,
                "span_end": None,
                "about_hints": [],
                "support_kind": "direct_statement",
                "metadata": {},
            }
        )
        claim = ClaimWithEvidenceCandidate.model_validate(
            {
                "claim_text": "Increased `MAX_RETRIES` in `tasks.py` from 3 to 5 to align with the new timeout window.",
                "claim_subkind": "fix_or_mitigation",
                "about_hints": [],
                "confidence": "high",
                "derivation_kind": "direct_from_observation",
                "metadata": {},
                "supporting_evidence": [],
            }
        )

        hints = collect_grounding_hints(observation=observation, claim=claim, evidence=evidence)

        self.assertEqual(
            resolve_about_record_ids(make_harness_like_snapshot(), hints),
            (
                "variable:test:test.src.tasks.MAX_RETRIES@r1",
                "file:test:test.src.tasks.__file__@r1",
            ),
        )

    def test_grounding_resolves_secret_key_summary_to_variable_and_auth_file(self):
        observation = make_observation(text="Rotate `SECRET_KEY` in `auth.py`.")
        evidence = EvidenceCandidate.model_validate(
            {
                "evidence_kind": "change_summary",
                "quote_text": "SECRET_KEY rotated",
                "summary_text": "The secret key in `auth.py` was updated to enhance security.",
                "span_start": None,
                "span_end": None,
                "about_hints": [],
                "support_kind": "direct_statement",
                "metadata": {},
            }
        )
        claim = ClaimWithEvidenceCandidate.model_validate(
            {
                "claim_text": "Rotated `SECRET_KEY` in `auth.py` to a new value.",
                "claim_subkind": "fix_or_mitigation",
                "about_hints": [],
                "confidence": "high",
                "derivation_kind": "direct_from_observation",
                "metadata": {},
                "supporting_evidence": [],
            }
        )

        hints = collect_grounding_hints(observation=observation, claim=claim, evidence=evidence)

        self.assertEqual(
            resolve_about_record_ids(make_harness_like_snapshot(), hints),
            (
                "variable:test:test.src.auth.SECRET_KEY@r1",
                "file:test:test.src.auth.__file__@r1",
            ),
        )

    def test_grounding_prefers_active_version_ids_from_snapshot_state(self):
        base_snapshot = make_harness_like_snapshot()
        snapshot = GraphSnapshot(
            repo_id=base_snapshot.repo_id,
            revision_id="new-revision",
            root_path=base_snapshot.root_path,
            generated_at=base_snapshot.generated_at,
            nodes=base_snapshot.nodes,
            edges=base_snapshot.edges,
            metadata={
                "deterministic_mutation_state": {
                    "active_node_versions": {
                        "variable:test:test.src.auth.SECRET_KEY": "variable:test:test.src.auth.SECRET_KEY@old-active",
                        "file:test:test.src.auth.__file__": "file:test:test.src.auth.__file__@old-active",
                    }
                }
            },
        )

        self.assertEqual(
            resolve_about_record_ids(snapshot, ["SECRET_KEY", "auth.py"]),
            (
                "variable:test:test.src.auth.SECRET_KEY@old-active",
                "file:test:test.src.auth.__file__@old-active",
            ),
        )


class ClaimValidationTests(unittest.TestCase):
    def setUp(self):
        self.validator = ClaimBundleValidator()
        self.observation = make_observation()
        self.snapshot = make_snapshot()

    def test_validator_accepts_grounded_claim_bundle(self):
        result = self.validator.validate(
            observation=self.observation,
            extraction=make_extraction_response(),
            snapshot=self.snapshot,
        )

        self.assertEqual(result.extracted_count, 1)
        self.assertEqual(result.validated_count, 1)
        self.assertEqual(result.rejected_count, 0)
        self.assertEqual(
            result.validated_claims[0].resolved_about_record_ids,
            ("symbol:demo:demo.app.main@r1",),
        )

    def test_validator_rejects_empty_claim_text(self):
        result = self.validator.validate(
            observation=self.observation,
            extraction=make_extraction_response(claim_text=" "),
            snapshot=self.snapshot,
        )

        self.assertEqual(result.validated_count, 0)
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual(result.rejected_claims[0].reason_code, "empty_claim_text")

    def test_validator_rejects_missing_supporting_evidence(self):
        extraction = ClaimEvidenceExtractionResponse.model_validate(
            {
                "claims": [
                    {
                        "claim_text": "demo.app.main was updated.",
                        "claim_subkind": "fix_or_mitigation",
                        "about_hints": ["demo.app.main"],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [],
                    }
                ]
            }
        )

        result = self.validator.validate(
            observation=self.observation,
            extraction=extraction,
            snapshot=self.snapshot,
        )

        self.assertEqual(result.rejected_claims[0].reason_code, "missing_supporting_evidence")

    def test_validator_accepts_observation_text_quotes_even_with_bad_spans(self):
        result = self.validator.validate(
            observation=self.observation,
            extraction=make_extraction_response(span_start=100, span_end=120),
            snapshot=self.snapshot,
        )

        self.assertEqual(result.validated_count, 1)

    def test_validator_normalizes_support_role_values_placed_in_support_kind(self):
        result = self.validator.validate(
            observation=self.observation,
            extraction=make_extraction_response(
                support_kind="states_rationale",
                support_role="provides_context",
                support_rationale="This quote explains why the change was made.",
            ),
            snapshot=self.snapshot,
        )

        self.assertEqual(result.validated_count, 1)
        evidence = result.validated_claims[0].supporting_evidence[0]
        self.assertEqual(evidence.support_kind, "direct_statement")
        self.assertEqual(evidence.support_role, "states_rationale")

    def test_validator_rejects_unsupported_source_kind(self):
        issue_observation = make_observation(kind=ObservationKind.GITHUB_ISSUE_BODY, source_kind="issue")
        result = self.validator.validate(
            observation=issue_observation,
            extraction=make_extraction_response(claim_kind="approval"),
            snapshot=self.snapshot,
        )

        self.assertEqual(result.rejected_claims[0].reason_code, "unsupported_source_kind")

    def test_validator_rejects_speculative_or_low_confidence_claims(self):
        speculative = self.validator.validate(
            observation=self.observation,
            extraction=make_extraction_response(
                claim_text="Maybe demo.app.main was updated.",
                assertion_status="confirmed",
            ),
            snapshot=self.snapshot,
        )
        low_confidence = self.validator.validate(
            observation=self.observation,
            extraction=make_extraction_response(confidence="low"),
            snapshot=self.snapshot,
        )

        self.assertEqual(speculative.rejected_claims[0].reason_code, "speculative_claim")
        self.assertEqual(low_confidence.rejected_claims[0].reason_code, "insufficient_confidence")

    def test_validator_maps_subkind_and_accepts_title_sourced_evidence(self):
        result = self.validator.validate(
            observation=self.observation,
            extraction=make_extraction_response(
                claim_text="Rollback handling should be tightened.",
                claim_kind=None,
                claim_subkind="desired_behavior_or_requirement",
                assertion_status="proposed",
                claim_frame="author_statement",
                source_hint=None,
                quote_text="Tighten rollback handling",
                span_start=None,
                span_end=None,
                evidence_kind="requirement_detail",
                support_source="observation_title",
                summary_text="The observation title states the requested change.",
            ),
            snapshot=self.snapshot,
        )

        self.assertEqual(result.validated_count, 1)
        self.assertEqual(result.validated_claims[0].claim_kind, "requirement")
        self.assertEqual(result.validated_claims[0].claim_subkind, "desired_behavior_or_requirement")
        self.assertEqual(result.validated_claims[0].assertion_status, "proposed")
        self.assertEqual(result.validated_claims[0].claim_frame, "author_statement")

    def test_validator_rejects_title_support_with_spans(self):
        result = self.validator.validate(
            observation=self.observation,
            extraction=make_extraction_response(
                claim_text="Rollback handling should be tightened.",
                claim_kind=None,
                claim_subkind="desired_behavior_or_requirement",
                source_hint=None,
                quote_text="Tighten rollback handling",
                span_start=0,
                span_end=25,
                evidence_kind="requirement_detail",
                support_source="observation_title",
                summary_text="The observation title states the requested change.",
            ),
            snapshot=self.snapshot,
        )

        self.assertEqual(result.rejected_claims[0].reason_code, "invalid_evidence_span")

    def test_validator_accepts_multi_claim_examples_with_rejected_correction(self):
        observation = Observation(
            observation_id="observation:github_issue_comment:test:4145222468",
            repo_id="test",
            observation_kind=ObservationKind.GITHUB_ISSUE_COMMENT,
            source_kind="issue",
            source_id="github:issue:test:2",
            observed_at="2026-03-27T20:58:11Z",
            author="octocat",
            title="Comment on issue #2: Fix app port",
            text=(
                "Correction: the `APP_PORT` change from 8080 to 9000 in `config.py` was caused by a load "
                "balancer misconfiguration that permanently bound port 8080 to a different service - not a firewall block."
            ),
            metadata={"issue_number": 2},
        )
        extraction = ClaimEvidenceExtractionResponse.model_validate(
            {
                "claims": [
                    {
                        "claim_text": "The APP_PORT change from 8080 to 9000 in config.py was caused by a load balancer misconfiguration.",
                        "claim_subkind": "rollout_migration_or_dependency",
                        "assertion_status": "reported",
                        "claim_frame": "author_statement",
                        "about_hints": ["config.py", "APP_PORT"],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "change_summary",
                                "quote_text": "the `APP_PORT` change from 8080 to 9000 in `config.py` was caused by a load balancer misconfiguration",
                                "summary_text": "The correction explains the new cause for the port change.",
                                "span_start": None,
                                "span_end": None,
                                "about_hints": ["config.py", "APP_PORT"],
                                "support_kind": "direct_statement",
                                "support_source": "observation_text",
                                "metadata": {},
                            }
                        ],
                    },
                    {
                        "claim_text": "A firewall block caused the APP_PORT change from 8080 to 9000 in config.py.",
                        "claim_subkind": "bug_or_regression",
                        "assertion_status": "rejected",
                        "claim_frame": "reported_observation",
                        "about_hints": ["config.py", "APP_PORT"],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "bug_signal",
                                "quote_text": "not a firewall block",
                                "summary_text": "The correction explicitly rejects the firewall explanation.",
                                "span_start": None,
                                "span_end": None,
                                "about_hints": ["config.py", "APP_PORT"],
                                "support_kind": "direct_statement",
                                "support_source": "observation_text",
                                "metadata": {},
                            }
                        ],
                    },
                ]
            }
        )

        result = self.validator.validate(
            observation=observation,
            extraction=extraction,
            snapshot=make_harness_like_snapshot(),
        )

        self.assertEqual(result.validated_count, 2)
        self.assertEqual(
            {claim.assertion_status for claim in result.validated_claims},
            {"reported", "rejected"},
        )

    def test_validator_accepts_three_claim_init_db_example(self):
        observation = Observation(
            observation_id="observation:github_pr_body:test:1",
            repo_id="test",
            observation_kind=ObservationKind.GITHUB_PR_BODY,
            source_kind="pull_request",
            source_id="github:pr:test:1",
            observed_at="2026-03-27T20:58:07Z",
            author="octocat",
            title="Fix DB connection timeout",
            text=(
                "Updated `init_db()` in `db.py` to pass `CONNECTION_TIMEOUT` to the SQLAlchemy engine. "
                "`DB_URL` in `db.py` now includes `connect_timeout=30` in the query string. "
                "The timeout value was set to 30 seconds because of slow network conditions observed in the staging environment."
            ),
            metadata={"pull_request_number": 1},
        )
        extraction = ClaimEvidenceExtractionResponse.model_validate(
            {
                "claims": [
                    {
                        "claim_text": "init_db in db.py now passes CONNECTION_TIMEOUT to the SQLAlchemy engine.",
                        "claim_subkind": "fix_or_mitigation",
                        "assertion_status": "reported",
                        "claim_frame": "author_statement",
                        "about_hints": ["init_db", "db.py"],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "change_summary",
                                "quote_text": "Updated `init_db()` in `db.py` to pass `CONNECTION_TIMEOUT` to the SQLAlchemy engine.",
                                "summary_text": "The observation describes the init_db timeout wiring change.",
                                "span_start": None,
                                "span_end": None,
                                "about_hints": ["init_db", "db.py"],
                                "support_kind": "direct_statement",
                                "support_source": "observation_text",
                                "metadata": {},
                            }
                        ],
                    },
                    {
                        "claim_text": "DB_URL in db.py now includes connect_timeout=30 in the query string.",
                        "claim_subkind": "fix_or_mitigation",
                        "assertion_status": "reported",
                        "claim_frame": "author_statement",
                        "about_hints": ["DB_URL", "db.py"],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "change_summary",
                                "quote_text": "`DB_URL` in `db.py` now includes `connect_timeout=30` in the query string.",
                                "summary_text": "The observation describes the DB_URL query-string update.",
                                "span_start": None,
                                "span_end": None,
                                "about_hints": ["DB_URL", "db.py"],
                                "support_kind": "direct_statement",
                                "support_source": "observation_text",
                                "metadata": {},
                            }
                        ],
                    },
                    {
                        "claim_text": "The timeout value was set to 30 seconds because of slow network conditions observed in the staging environment.",
                        "claim_subkind": "rollout_migration_or_dependency",
                        "assertion_status": "reported",
                        "claim_frame": "author_statement",
                        "about_hints": ["db.py"],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "scope_detail",
                                "quote_text": "The timeout value was set to 30 seconds because of slow network conditions observed in the staging environment.",
                                "summary_text": "The observation states the rationale for the timeout value.",
                                "span_start": None,
                                "span_end": None,
                                "about_hints": ["db.py"],
                                "support_kind": "direct_statement",
                                "support_source": "observation_text",
                                "metadata": {},
                            }
                        ],
                    },
                ]
            }
        )

        result = self.validator.validate(
            observation=observation,
            extraction=extraction,
            snapshot=make_provenance_examples_snapshot(),
        )

        self.assertEqual(result.validated_count, 3)
        self.assertEqual(result.rejected_count, 0)


class EvidencePlannerTests(unittest.TestCase):
    def test_evidence_planner_grounds_file_hints_to_file_versions(self):
        observation = make_observation(text="This PR changes app.py.")
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="app.py was changed.",
                source_hint="app.py",
                quote_text="app.py",
                span_start=16,
                span_end=22,
            ),
            snapshot=make_snapshot(),
        )

        prepared = EvidenceMutationPlanner().prepare_validated_claim_bundle(
            observation=observation,
            bundle=validation.validated_claims[0],
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {record_id for item in prepared for record_id in item.about_record_ids},
            {"file:demo:demo.app.__file__@r1"},
        )

    def test_evidence_planner_grounds_exact_file_paths_from_text(self):
        observation = make_observation(text="This PR updates src/auth/main.py.")
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="src/auth/main.py was updated.",
                source_hint=None,
                quote_text="src/auth/main.py",
                summary_text="The source mentions src/auth/main.py.",
                span_start=None,
                span_end=None,
            ),
            snapshot=make_snapshot(),
        )

        prepared = EvidenceMutationPlanner().prepare_validated_claim_bundle(
            observation=observation,
            bundle=validation.validated_claims[0],
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {record_id for item in prepared for record_id in item.about_record_ids},
            {"file:demo:demo.auth.main.__file__@r1"},
        )

    def test_evidence_planner_grounds_exact_qualified_names_from_text(self):
        observation = make_observation(text="This PR updates demo.app.main.")
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="demo.app.main was updated.",
                source_hint=None,
                quote_text="demo.app.main",
                summary_text="The source mentions demo.app.main.",
                span_start=None,
                span_end=None,
            ),
            snapshot=make_snapshot(),
        )

        prepared = EvidenceMutationPlanner().prepare_validated_claim_bundle(
            observation=observation,
            bundle=validation.validated_claims[0],
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {record_id for item in prepared for record_id in item.about_record_ids},
            {"symbol:demo:demo.app.main@r1"},
        )

    def test_evidence_planner_grounds_dotted_partial_suffixes_from_text(self):
        observation = make_observation(text="This PR updates AuthService.login().")
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="AuthService.login() was updated.",
                source_hint=None,
                quote_text="AuthService.login()",
                summary_text="The source mentions AuthService.login().",
                span_start=None,
                span_end=None,
            ),
            snapshot=make_snapshot(),
        )

        prepared = EvidenceMutationPlanner().prepare_validated_claim_bundle(
            observation=observation,
            bundle=validation.validated_claims[0],
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {record_id for item in prepared for record_id in item.about_record_ids},
            {"symbol:demo:demo.auth.AuthService.login@r1"},
        )

    def test_evidence_planner_grounds_unique_bare_symbol_names_from_text(self):
        observation = make_observation(text="This PR updates AuthService.")
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="AuthService was updated.",
                source_hint=None,
                quote_text="AuthService",
                summary_text="The source mentions AuthService.",
                span_start=None,
                span_end=None,
            ),
            snapshot=make_snapshot(),
        )

        prepared = EvidenceMutationPlanner().prepare_validated_claim_bundle(
            observation=observation,
            bundle=validation.validated_claims[0],
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {record_id for item in prepared for record_id in item.about_record_ids},
            {"symbol:demo:demo.auth.AuthService@r1"},
        )

    def test_evidence_planner_emits_grounded_evidence_records_from_validated_claims(self):
        observation = make_observation()
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(),
            snapshot=make_snapshot(),
        )

        plan = EvidenceMutationPlanner().plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=make_snapshot(),
        )

        self.assertEqual(len(plan.upsert_nodes), 1)
        self.assertEqual(len(plan.upsert_search_documents), 1)
        self.assertEqual(
            {edge.relationship_type for edge in plan.upsert_edges},
            {"DERIVED_FROM"},
        )
        self.assertNotIn("claim_kind", plan.upsert_nodes[0].properties)
        self.assertNotIn("claim_kind", plan.upsert_search_documents[0].properties)
        self.assertEqual(plan.upsert_search_documents[0].properties["support_source"], "observation_text")

    def test_evidence_planner_does_not_ground_ambiguous_file_basenames(self):
        observation = make_observation(text="This PR changes main.py.")
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="main.py was updated.",
                source_hint=None,
                quote_text="main.py",
                summary_text="The source mentions main.py.",
                span_start=None,
                span_end=None,
            ),
            snapshot=make_snapshot(),
        )

        plan = EvidenceMutationPlanner().plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {edge.relationship_type for edge in plan.upsert_edges},
            {"DERIVED_FROM"},
        )

    def test_evidence_planner_does_not_ground_ambiguous_bare_symbol_names(self):
        observation = make_observation(text="This PR updates login.")
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="login was updated.",
                source_hint=None,
                quote_text="login",
                summary_text="The source mentions login.",
                span_start=None,
                span_end=None,
            ),
            snapshot=make_snapshot(),
        )

        plan = EvidenceMutationPlanner().plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {edge.relationship_type for edge in plan.upsert_edges},
            {"DERIVED_FROM"},
        )

    def test_evidence_planner_leaves_unresolved_hints_unbound(self):
        observation = make_observation(
            kind=ObservationKind.GITHUB_ISSUE_COMMENT,
            source_kind="issue",
            text="This comment mentions an unknown symbol.",
        )
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="An unknown symbol is affected.",
                claim_kind="bug_report",
                source_hint="demo.app.unknown",
                quote_text="unknown symbol",
                summary_text="The source mentions an unknown symbol.",
                span_start=24,
                span_end=38,
                evidence_kind="bug_signal",
            ),
            snapshot=make_snapshot(),
        )

        plan = EvidenceMutationPlanner().plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {edge.relationship_type for edge in plan.upsert_edges},
            {"DERIVED_FROM"},
        )

    def test_evidence_planner_falls_back_to_observation_mentions_when_support_is_thin(self):
        observation = make_observation(text="This PR updates demo.app.main.")
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="main was updated.",
                claim_kind=None,
                claim_subkind="fix_or_mitigation",
                source_hint=None,
                quote_text="updates",
                span_start=None,
                span_end=None,
                summary_text="The source indicates an update happened.",
            ),
            snapshot=make_snapshot(),
        )

        prepared = EvidenceMutationPlanner().prepare_validated_claim_bundle(
            observation=observation,
            bundle=validation.validated_claims[0],
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {record_id for item in prepared for record_id in item.about_record_ids},
            {"symbol:demo:demo.app.main@r1"},
        )

    def test_evidence_planner_persists_stance_fields_on_records(self):
        observation = make_observation()
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="Rollback handling should be tightened.",
                claim_kind=None,
                claim_subkind="desired_behavior_or_requirement",
                assertion_status="proposed",
                claim_frame="author_statement",
                source_hint=None,
                quote_text="Tighten rollback handling",
                span_start=None,
                span_end=None,
                evidence_kind="requirement_detail",
                support_source="observation_title",
                summary_text="The title states the requested change.",
            ),
            snapshot=make_snapshot(),
        )

        plan = EvidenceMutationPlanner().plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=make_snapshot(),
        )

        node_properties = plan.upsert_nodes[0].properties
        search_properties = plan.upsert_search_documents[0].properties
        self.assertEqual(node_properties["support_source"], "observation_title")
        self.assertEqual(search_properties["support_source"], "observation_title")
        self.assertNotIn("claim_subkind", node_properties)
        self.assertNotIn("assertion_status", node_properties)
        self.assertNotIn("claim_frame", node_properties)
        self.assertNotIn("claim_subkind", search_properties)
        self.assertNotIn("assertion_status", search_properties)
        self.assertNotIn("claim_frame", search_properties)


class CurrentEvidenceReaderTests(unittest.TestCase):
    def test_reader_backfills_legacy_claim_fields_from_metadata_and_observation(self):
        backend_driver = FakeBackendDriver(
            [
                make_existing_evidence_row(
                    top_level_claims=False,
                    observed_at="2026-03-24T00:00:00+00:00",
                )
            ]
        )

        facts = CurrentEvidenceReader(backend_driver=backend_driver).load_current_facts(
            repo_id="demo",
            claim_kind="state_claim",
            about_record_ids=("symbol:demo:demo.app.main@r1",),
        )

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].claim_text, "demo.app.main is disabled.")
        self.assertEqual(facts[0].claim_kind, "state_claim")
        self.assertEqual(facts[0].observed_at, "2026-03-24T00:00:00+00:00")

    def test_reader_uses_backend_sync_bridge_runner(self):
        backend_driver = FakeBackendDriver([])

        with patch(
            "context_graph.provenance.invalidation.run_backend_coroutine_sync",
            return_value=[],
        ) as runner:
            CurrentEvidenceReader(backend_driver=backend_driver).load_current_facts(
                repo_id="demo",
                claim_kind="state_claim",
                about_record_ids=("symbol:demo:demo.app.main@r1",),
            )

        self.assertEqual(runner.call_count, 1)


class EvidenceInvalidationPlannerTests(unittest.TestCase):
    def setUp(self):
        self.observation = make_observation(
            text="This PR says demo.app.main is enabled now.",
        )
        self.snapshot = make_snapshot()
        self.validation = ClaimBundleValidator().validate(
            observation=self.observation,
            extraction=make_extraction_response(
                claim_text="demo.app.main is enabled.",
                claim_kind="state_claim",
                quote_text="demo.app.main is enabled",
                summary_text="The source says demo.app.main is enabled.",
                span_start=None,
                span_end=None,
                evidence_kind="state_detail",
            ),
            snapshot=self.snapshot,
        )

    def test_new_contradictory_fact_invalidates_older_current_evidence(self):
        existing_fact_key = canonical_claim_key(
            claim_kind="state_claim",
            claim_text="demo.app.main is disabled.",
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver(
                [
                    make_existing_evidence_row(
                        claim_text="demo.app.main is disabled.",
                        claim_kind="state_claim",
                        observed_at="2026-03-25T00:00:00+00:00",
                    )
                ]
            ),
            fact_conflict_resolution_service=FactConflictResolutionService(
                llm_client=PromptMapLLMClient(
                    {
                        "provenance.resolve_fact_conflicts.compare_against_current.v1": {
                            "contradicted_fact_ids": [existing_fact_key]
                        }
                    }
                )
            ),
        )

        result = planner.plan_validated_claims(
            observation=self.observation,
            validation=self.validation,
            snapshot=self.snapshot,
        )

        self.assertEqual(result.invalidated_count, 1)
        nodes = {node.record_id: node for node in result.plan.upsert_nodes}
        claim_nodes = [
            node
            for node in result.plan.upsert_nodes
            if node.record_type == "claim"
        ]
        self.assertEqual(len(claim_nodes), 2)
        new_claim = next(
            node
            for node in claim_nodes
            if node.properties["claim_text"] == "demo.app.main is enabled."
        )
        old_claim = next(
            node
            for node in claim_nodes
            if node.properties["claim_text"] == "demo.app.main is disabled."
        )
        self.assertTrue(new_claim.properties["is_current"])
        self.assertFalse(old_claim.properties["is_current"])
        self.assertEqual(new_claim.properties["valid_from"], self.observation.observed_at)
        self.assertEqual(old_claim.properties["valid_to"], self.observation.observed_at)
        self.assertEqual(old_claim.properties["invalidated_by_claim_id"], new_claim.record_id)
        contradicts_edges = [
            edge
            for edge in result.plan.upsert_edges
            if edge.relationship_type == "CONTRADICTS"
        ]
        self.assertEqual(len(contradicts_edges), 1)
        self.assertEqual(contradicts_edges[0].source_record_id, new_claim.record_id)
        self.assertEqual(contradicts_edges[0].target_record_id, old_claim.record_id)
        support_edges = [
            edge
            for edge in result.plan.upsert_edges
            if edge.relationship_type == "SUPPORTS_CLAIM"
        ]
        self.assertEqual(len(support_edges), 2)

    def test_older_new_fact_is_inserted_already_invalidated(self):
        existing_fact_key = canonical_claim_key(
            claim_kind="state_claim",
            claim_text="demo.app.main is disabled.",
        )
        old_observation = make_observation(
            text="This PR says demo.app.main is enabled now.",
        )
        old_observation = Observation(
            observation_id=old_observation.observation_id,
            repo_id=old_observation.repo_id,
            observation_kind=old_observation.observation_kind,
            source_kind=old_observation.source_kind,
            source_id=old_observation.source_id,
            observed_at="2026-03-24T00:00:00+00:00",
            author=old_observation.author,
            title=old_observation.title,
            text=old_observation.text,
            metadata=old_observation.metadata,
        )
        validation = ClaimBundleValidator().validate(
            observation=old_observation,
            extraction=make_extraction_response(
                claim_text="demo.app.main is enabled.",
                claim_kind="state_claim",
                quote_text="demo.app.main is enabled",
                summary_text="The source says demo.app.main is enabled.",
                span_start=None,
                span_end=None,
                evidence_kind="state_detail",
            ),
            snapshot=self.snapshot,
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver(
                [
                    make_existing_evidence_row(
                        claim_text="demo.app.main is disabled.",
                        claim_kind="state_claim",
                        observed_at="2026-03-25T00:00:00+00:00",
                    )
                ]
            ),
            fact_conflict_resolution_service=FactConflictResolutionService(
                llm_client=PromptMapLLMClient(
                    {
                        "provenance.resolve_fact_conflicts.compare_against_current.v1": {
                            "contradicted_fact_ids": [existing_fact_key]
                        }
                    }
                )
            ),
        )

        result = planner.plan_validated_claims(
            observation=old_observation,
            validation=validation,
            snapshot=self.snapshot,
        )

        self.assertEqual(result.invalidated_count, 0)
        new_claim = next(
            node
            for node in result.plan.upsert_nodes
            if node.record_type == "claim"
            and node.properties["claim_text"] == "demo.app.main is enabled."
        )
        new_evidence = next(
            node
            for node in result.plan.upsert_nodes
            if node.record_type == "evidence_record"
        )
        existing_claim = next(
            node
            for node in result.plan.upsert_nodes
            if node.record_type == "claim"
            and node.properties["claim_text"] == "demo.app.main is disabled."
        )
        self.assertFalse(new_claim.properties["is_current"])
        self.assertEqual(new_claim.properties["valid_from"], old_observation.observed_at)
        self.assertEqual(new_claim.properties["valid_to"], "2026-03-25T00:00:00+00:00")
        self.assertTrue(existing_claim.properties["is_current"])
        self.assertNotIn("is_current", new_evidence.properties)
        self.assertNotIn("invalidated_by_fact_key", new_evidence.properties)
        self.assertEqual(new_evidence.properties["observation_id"], old_observation.observation_id)
        contradicts_edges = [
            edge
            for edge in result.plan.upsert_edges
            if edge.relationship_type == "CONTRADICTS"
        ]
        self.assertEqual(len(contradicts_edges), 1)
        self.assertEqual(contradicts_edges[0].source_record_id, new_claim.record_id)
        self.assertEqual(contradicts_edges[0].target_record_id, existing_claim.record_id)

    def test_exact_duplicate_fact_key_does_not_invalidate_anything(self):
        duplicate_fact_key = canonical_claim_key(
            claim_kind="state_claim",
            claim_text="demo.app.main is enabled.",
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver(
                [
                    make_existing_evidence_row(
                        evidence_id="evidence:demo:duplicate",
                        claim_text="demo.app.main is enabled.",
                        claim_kind="state_claim",
                        fact_key=duplicate_fact_key,
                        observed_at="2026-03-25T00:00:00+00:00",
                    )
                ]
            ),
            fact_conflict_resolution_service=FactConflictResolutionService(
                llm_client=PromptMapLLMClient(
                    {
                        "provenance.resolve_fact_conflicts.compare_against_current.v1": {
                            "contradicted_fact_ids": []
                        }
                    }
                )
            ),
        )

        result = planner.plan_validated_claims(
            observation=self.observation,
            validation=self.validation,
            snapshot=self.snapshot,
        )

        self.assertEqual(result.invalidated_count, 0)
        self.assertEqual(
            {edge.relationship_type for edge in result.plan.upsert_edges},
            {"DERIVED_FROM", "ABOUT", "SUPPORTS_CLAIM"},
        )
        self.assertTrue(
            any(node.record_type == "claim" for node in result.plan.upsert_nodes)
        )

    def test_different_about_targets_do_not_invalidate(self):
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver(
                [
                    make_existing_evidence_row(
                        about_record_ids=("symbol:demo:demo.auth.AuthService@r1",),
                    )
                ]
            ),
            fact_conflict_resolution_service=FactConflictResolutionService(
                llm_client=PromptMapLLMClient(
                    {
                        "provenance.resolve_fact_conflicts.compare_against_current.v1": {
                            "contradicted_fact_ids": ["unused"]
                        }
                    }
                )
            ),
        )

        result = planner.plan_validated_claims(
            observation=self.observation,
            validation=self.validation,
            snapshot=self.snapshot,
        )

        self.assertEqual(result.invalidated_count, 0)
        self.assertEqual(
            {edge.relationship_type for edge in result.plan.upsert_edges},
            {"DERIVED_FROM", "ABOUT", "SUPPORTS_CLAIM"},
        )
        self.assertFalse(
            any(edge.relationship_type == "CONTRADICTS" for edge in result.plan.upsert_edges)
        )

    def test_rejected_claims_are_persisted_as_non_current_without_conflict_checks(self):
        backend_driver = FakeBackendDriver(
            [
                make_existing_evidence_row(
                    claim_text="demo.app.main is disabled.",
                    claim_kind="state_claim",
                )
            ]
        )
        resolution_client = PromptMapLLMClient(
            {
                "provenance.resolve_fact_conflicts.compare_against_current.v1": {
                    "contradicted_fact_ids": ["unused"]
                }
            }
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=backend_driver,
            fact_conflict_resolution_service=FactConflictResolutionService(
                llm_client=resolution_client
            ),
        )
        rejected_validation = ClaimBundleValidator().validate(
            observation=self.observation,
            extraction=make_extraction_response(
                claim_text="demo.app.main is disabled.",
                claim_kind="state_claim",
                claim_subkind="invariant_or_constraint",
                assertion_status="rejected",
                claim_frame="reported_observation",
                quote_text="demo.app.main is enabled now",
                summary_text="The observation rejects an older disabled state.",
                span_start=None,
                span_end=None,
                evidence_kind="state_detail",
            ),
            snapshot=self.snapshot,
        )

        result = planner.plan_validated_claims(
            observation=self.observation,
            validation=rejected_validation,
            snapshot=self.snapshot,
        )

        self.assertEqual(result.invalidated_count, 0)
        self.assertEqual(backend_driver.calls, [])
        self.assertEqual(resolution_client.calls, [])
        self.assertEqual(len(result.plan.upsert_nodes), 2)
        claim_node = next(node for node in result.plan.upsert_nodes if node.record_type == "claim")
        evidence_node = next(node for node in result.plan.upsert_nodes if node.record_type == "evidence_record")
        self.assertFalse(claim_node.properties["is_current"])
        self.assertEqual(claim_node.properties["valid_from"], self.observation.observed_at)
        self.assertEqual(claim_node.properties["valid_to"], self.observation.observed_at)
        self.assertNotIn("is_current", evidence_node.properties)
        self.assertTrue(
            any(edge.relationship_type == "SUPPORTS_CLAIM" for edge in result.plan.upsert_edges)
        )

    def test_exact_match_updates_valid_from_when_older_support_arrives_late(self):
        observation = make_observation(text="This PR says demo.app.main is enabled now.")
        observation = Observation(
            observation_id=observation.observation_id,
            repo_id=observation.repo_id,
            observation_kind=observation.observation_kind,
            source_kind=observation.source_kind,
            source_id=observation.source_id,
            observed_at="2026-03-24T00:00:00+00:00",
            author=observation.author,
            title=observation.title,
            text=observation.text,
            metadata=observation.metadata,
            ingested_at=observation.ingested_at,
            thread_kind=observation.thread_kind,
            thread_id=observation.thread_id,
        )
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="demo.app.main is enabled.",
                claim_kind="state_claim",
                quote_text="demo.app.main is enabled",
                summary_text="The source says demo.app.main is enabled.",
                span_start=None,
                span_end=None,
                evidence_kind="state_detail",
            ),
            snapshot=self.snapshot,
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver(
                [
                    make_existing_claim_row(
                        claim_id="claim:demo:enabled",
                        claim_text="demo.app.main is enabled.",
                        latest_observed_at="2026-03-25T00:00:00+00:00",
                        first_observed_at="2026-03-25T00:00:00+00:00",
                    )
                ]
            ),
        )

        result = planner.plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=self.snapshot,
        )

        claim_node = next(node for node in result.plan.upsert_nodes if node.record_type == "claim")
        self.assertEqual(claim_node.record_id, "claim:demo:enabled")
        self.assertEqual(claim_node.properties["first_observed_at"], "2026-03-24T00:00:00+00:00")
        self.assertEqual(claim_node.properties["valid_from"], "2026-03-24T00:00:00+00:00")
        self.assertEqual(claim_node.properties["latest_observed_at"], "2026-03-25T00:00:00+00:00")

    def test_same_observation_pending_claim_can_be_invalidated(self):
        observation = make_observation(
            text=(
                "`MAX_RETRIES` was increased from 3 to 5 in `tasks.py` due to database reconnect delay issues. "
                "Correction: `MAX_RETRIES` in `tasks.py` was left at 3."
            )
        )
        extraction = ClaimEvidenceExtractionResponse.model_validate(
            {
                "claims": [
                    {
                        "claim_text": "MAX_RETRIES was increased to 5 due to database reconnect delay issues.",
                        "claim_subkind": "fix_or_mitigation",
                        "assertion_status": "observed",
                        "claim_frame": "author_statement",
                        "about_hints": [],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "change_summary",
                                "quote_text": "`MAX_RETRIES` was increased from 3 to 5 in `tasks.py` due to database reconnect delay issues.",
                                "summary_text": "The source says MAX_RETRIES was increased.",
                                "span_start": None,
                                "span_end": None,
                                "about_hints": [],
                                "support_kind": "direct_statement",
                                "support_source": "observation_text",
                                "support_role": "states_change",
                                "support_rationale": "This quote states that MAX_RETRIES was increased to 5.",
                                "metadata": {},
                            }
                        ],
                    },
                    {
                        "claim_text": "MAX_RETRIES in tasks.py was left at 3.",
                        "claim_subkind": "invariant_or_constraint",
                        "assertion_status": "observed",
                        "claim_frame": "author_statement",
                        "about_hints": [],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "state_detail",
                                "quote_text": "`MAX_RETRIES` in `tasks.py` was left at 3.",
                                "summary_text": "The source says MAX_RETRIES stayed at 3.",
                                "span_start": None,
                                "span_end": None,
                                "about_hints": [],
                                "support_kind": "direct_statement",
                                "support_source": "observation_text",
                                "support_role": "states_constraint",
                                "support_rationale": "This quote states that MAX_RETRIES remained at 3.",
                                "metadata": {},
                            }
                        ],
                    },
                ]
            }
        )
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=extraction,
            snapshot=None,
        )
        first_bundle = validation.validated_claims[0]
        first_canonical_key = canonical_claim_key(
            claim_kind=first_bundle.claim_kind,
            claim_text=first_bundle.claim_text,
            about_record_ids=first_bundle.resolved_about_record_ids,
        )
        first_claim_id = claim_record_id(
            repo_id=observation.repo_id,
            canonical_key=first_canonical_key,
            about_record_ids=first_bundle.resolved_about_record_ids,
        )
        llm_client = PromptMapLLMClient(
            {
                "provenance.relate_claims.resolve_new_claim_against_candidates.v1": {
                    "relations": [
                        {
                            "candidate_claim_id": first_claim_id,
                            "resolution_type": "contradicts",
                            "confidence": "high",
                            "rationale": "Leaving MAX_RETRIES at 3 contradicts the earlier increase to 5.",
                        }
                    ]
                }
            }
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver([]),
            claim_resolution_service=ClaimResolutionService(llm_client=llm_client),
        )

        result = planner.plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=None,
        )

        self.assertEqual(result.invalidated_count, 1)
        self.assertEqual(len(llm_client.calls), 1)
        claim_nodes = {
            node.properties["claim_text"]: node
            for node in result.plan.upsert_nodes
            if node.record_type == "claim"
        }
        self.assertEqual(len(claim_nodes), 2)
        increased_claim = claim_nodes["MAX_RETRIES was increased to 5 due to database reconnect delay issues."]
        left_claim = claim_nodes["MAX_RETRIES in tasks.py was left at 3."]
        self.assertFalse(increased_claim.properties["is_current"])
        self.assertEqual(increased_claim.properties["invalidated_by_claim_id"], left_claim.record_id)
        self.assertTrue(left_claim.properties["is_current"])
        contradicts_edge = next(
            edge for edge in result.plan.upsert_edges if edge.relationship_type == "CONTRADICTS"
        )
        self.assertEqual(contradicts_edge.source_record_id, left_claim.record_id)
        self.assertEqual(contradicts_edge.target_record_id, increased_claim.record_id)
        relation_profile = next(
            profile for profile in result.profiles if profile.profile_kind == SEMANTIC_RELATION_PROFILE_KIND
        )
        self.assertEqual(relation_profile.profile_target_id, contradicts_edge.record_id)
        self.assertEqual(relation_profile.relation_type, "contradicts")
        self.assertEqual(
            relation_profile.value_text,
            "Leaving MAX_RETRIES at 3 contradicts the earlier increase to 5.",
        )
        self.assertFalse(relation_profile.is_active)
        self.assertEqual(
            relation_profile.metadata["resolution_confidence"],
            "high",
        )

    def test_same_run_same_as_reuses_pending_claim(self):
        observation = make_observation(
            text=(
                "Rotated `SECRET_KEY` in `auth.py` to a new value. "
                "Restatement: rotated `SECRET_KEY` in `auth.py` to a fresh value."
            )
        )
        extraction = ClaimEvidenceExtractionResponse.model_validate(
            {
                "claims": [
                    {
                        "claim_text": "The `SECRET_KEY` in `auth.py` was rotated to a new value.",
                        "claim_subkind": "fix_or_mitigation",
                        "assertion_status": "observed",
                        "claim_frame": "author_statement",
                        "about_hints": [],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "change_summary",
                                "quote_text": "Rotated `SECRET_KEY` in `auth.py` to a new value.",
                                "summary_text": "The source says SECRET_KEY was rotated.",
                                "span_start": None,
                                "span_end": None,
                                "about_hints": [],
                                "support_kind": "direct_statement",
                                "support_source": "observation_text",
                                "support_role": "states_change",
                                "support_rationale": "This quote explicitly states the SECRET_KEY rotation.",
                                "metadata": {},
                            }
                        ],
                    },
                    {
                        "claim_text": "Rotated `SECRET_KEY` in `auth.py` to a fresh value.",
                        "claim_subkind": "fix_or_mitigation",
                        "assertion_status": "observed",
                        "claim_frame": "author_statement",
                        "about_hints": [],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "change_summary",
                                "quote_text": "rotated `SECRET_KEY` in `auth.py` to a fresh value.",
                                "summary_text": "The source restates the SECRET_KEY rotation.",
                                "span_start": None,
                                "span_end": None,
                                "about_hints": [],
                                "support_kind": "direct_statement",
                                "support_source": "observation_text",
                                "support_role": "states_change",
                                "support_rationale": "This quote restates the same SECRET_KEY rotation.",
                                "metadata": {},
                            }
                        ],
                    },
                ]
            }
        )
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=extraction,
            snapshot=None,
        )
        first_bundle = validation.validated_claims[0]
        first_canonical_key = canonical_claim_key(
            claim_kind=first_bundle.claim_kind,
            claim_text=first_bundle.claim_text,
            about_record_ids=first_bundle.resolved_about_record_ids,
        )
        first_claim_id = claim_record_id(
            repo_id=observation.repo_id,
            canonical_key=first_canonical_key,
            about_record_ids=first_bundle.resolved_about_record_ids,
        )
        llm_client = PromptMapLLMClient(
            {
                "provenance.relate_claims.resolve_new_claim_against_candidates.v1": {
                    "relations": [
                        {
                            "candidate_claim_id": first_claim_id,
                            "resolution_type": "same_as",
                            "confidence": "high",
                            "rationale": "These statements describe the same SECRET_KEY rotation.",
                        }
                    ]
                }
            }
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver([]),
            claim_resolution_service=ClaimResolutionService(llm_client=llm_client),
        )

        result = planner.plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=None,
        )

        self.assertEqual(len(llm_client.calls), 1)
        claim_nodes = [
            node for node in result.plan.upsert_nodes if node.record_type == "claim"
        ]
        self.assertEqual(len(claim_nodes), 1)
        support_edges = [
            edge for edge in result.plan.upsert_edges if edge.relationship_type == "SUPPORTS_CLAIM"
        ]
        self.assertEqual(len(support_edges), 2)
        self.assertTrue(all(edge.target_record_id == first_claim_id for edge in support_edges))
        self.assertFalse(
            any(profile.profile_kind == SEMANTIC_RELATION_PROFILE_KIND for profile in result.profiles)
        )

    def test_same_session_late_arriving_older_claim_is_non_current(self):
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver([]),
            claim_resolution_service=ClaimResolutionService(
                llm_client=PromptMapLLMClient(
                    {
                        "provenance.relate_claims.resolve_new_claim_against_candidates.v1": {
                            "relations": [
                                {
                                    "candidate_claim_id": "claim:demo:disabled-pending",
                                    "resolution_type": "contradicts",
                                    "confidence": "high",
                                    "rationale": "Enabled contradicts the newer disabled claim.",
                                }
                            ]
                        }
                    }
                )
            ),
        )
        session = planner.begin_claim_session(repo_id="demo")
        newer_observation = make_observation(text="This PR says demo.app.main is disabled now.")
        newer_observation = Observation(
            observation_id=newer_observation.observation_id,
            repo_id=newer_observation.repo_id,
            observation_kind=newer_observation.observation_kind,
            source_kind=newer_observation.source_kind,
            source_id=newer_observation.source_id,
            observed_at="2026-03-26T00:00:00+00:00",
            author=newer_observation.author,
            title=newer_observation.title,
            text=newer_observation.text,
            metadata=newer_observation.metadata,
            ingested_at=newer_observation.ingested_at,
            thread_kind=newer_observation.thread_kind,
            thread_id=newer_observation.thread_id,
        )
        newer_validation = ClaimBundleValidator().validate(
            observation=newer_observation,
            extraction=make_extraction_response(
                claim_text="demo.app.main is disabled.",
                claim_kind="state_claim",
                quote_text="demo.app.main is disabled",
                summary_text="The source says demo.app.main is disabled.",
                span_start=None,
                span_end=None,
                evidence_kind="state_detail",
            ),
            snapshot=self.snapshot,
        )
        newer_bundle = newer_validation.validated_claims[0]
        newer_canonical_key = canonical_claim_key(
            claim_kind=newer_bundle.claim_kind,
            claim_text=newer_bundle.claim_text,
            about_record_ids=newer_bundle.resolved_about_record_ids,
        )
        newer_claim_id = claim_record_id(
            repo_id=newer_observation.repo_id,
            canonical_key=newer_canonical_key,
            about_record_ids=newer_bundle.resolved_about_record_ids,
        )
        planner.claim_mutation_planner.claim_resolution_service.llm_client.responses_by_prompt[
            "provenance.relate_claims.resolve_new_claim_against_candidates.v1"
        ][0]["relations"][0]["candidate_claim_id"] = newer_claim_id
        planner.plan_validated_claims(
            observation=newer_observation,
            validation=newer_validation,
            snapshot=self.snapshot,
            session=session,
        )

        older_observation = make_observation(text="This PR says demo.app.main is enabled now.")
        older_observation = Observation(
            observation_id=older_observation.observation_id,
            repo_id=older_observation.repo_id,
            observation_kind=older_observation.observation_kind,
            source_kind=older_observation.source_kind,
            source_id=older_observation.source_id,
            observed_at="2026-03-25T00:00:00+00:00",
            author=older_observation.author,
            title=older_observation.title,
            text=older_observation.text,
            metadata=older_observation.metadata,
            ingested_at=older_observation.ingested_at,
            thread_kind=older_observation.thread_kind,
            thread_id=older_observation.thread_id,
        )
        older_validation = ClaimBundleValidator().validate(
            observation=older_observation,
            extraction=make_extraction_response(
                claim_text="demo.app.main is enabled.",
                claim_kind="state_claim",
                quote_text="demo.app.main is enabled",
                summary_text="The source says demo.app.main is enabled.",
                span_start=None,
                span_end=None,
                evidence_kind="state_detail",
            ),
            snapshot=self.snapshot,
        )

        result = planner.plan_validated_claims(
            observation=older_observation,
            validation=older_validation,
            snapshot=self.snapshot,
            session=session,
        )

        self.assertEqual(result.invalidated_count, 0)
        older_claim = next(
            node
            for node in result.plan.upsert_nodes
            if node.record_type == "claim" and node.properties["claim_text"] == "demo.app.main is enabled."
        )
        self.assertFalse(older_claim.properties["is_current"])
        self.assertEqual(older_claim.properties["valid_to"], "2026-03-26T00:00:00+00:00")
        contradicts_edge = next(
            edge for edge in result.plan.upsert_edges if edge.relationship_type == "CONTRADICTS"
        )
        self.assertEqual(contradicts_edge.target_record_id, newer_claim_id)
        relation_profile = next(
            profile for profile in result.profiles if profile.profile_kind == SEMANTIC_RELATION_PROFILE_KIND
        )
        self.assertFalse(relation_profile.is_active)

    def test_same_session_legacy_backfill_runs_once(self):
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver(
                [
                    make_existing_evidence_row(
                        evidence_id="evidence:demo:legacy",
                        claim_text="demo.app.main is enabled.",
                        claim_kind="state_claim",
                    )
                ]
            ),
        )
        session = planner.begin_claim_session(repo_id="demo")

        first_result = planner.plan_validated_claims(
            observation=self.observation,
            validation=self.validation,
            snapshot=self.snapshot,
            session=session,
        )
        second_result = planner.plan_validated_claims(
            observation=self.observation,
            validation=self.validation,
            snapshot=self.snapshot,
            session=session,
        )

        first_legacy_support_edges = [
            edge
            for edge in first_result.plan.upsert_edges
            if edge.relationship_type == "SUPPORTS_CLAIM"
            and edge.source_record_id == "evidence:demo:legacy"
        ]
        second_legacy_support_edges = [
            edge
            for edge in second_result.plan.upsert_edges
            if edge.relationship_type == "SUPPORTS_CLAIM"
            and edge.source_record_id == "evidence:demo:legacy"
        ]
        self.assertEqual(len(first_legacy_support_edges), 1)
        self.assertEqual(second_legacy_support_edges, [])

    def test_same_thread_candidates_are_resolved_before_global_candidates(self):
        llm_client = PromptMapLLMClient(
            {
                "provenance.relate_claims.resolve_new_claim_against_candidates.v1": {
                    "relations": [
                        {
                            "candidate_claim_id": "claim:demo:thread",
                            "resolution_type": "same_as",
                            "confidence": "high",
                            "rationale": "These claims express the same scoped proposition.",
                        }
                    ]
                }
            }
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver(
                [
                    make_existing_claim_row(
                        claim_id="claim:demo:thread",
                        claim_text="demo.app.main is enabled for PR #7.",
                        source_id="github:pr:demo:7",
                        thread_kind="pull_request",
                        thread_id="7",
                    ),
                    make_existing_claim_row(
                        claim_id="claim:demo:global",
                        claim_text="demo.app.main is enabled for issue #9.",
                        source_kind="issue",
                        source_id="github:issue:demo:9",
                        thread_kind="issue",
                        thread_id="9",
                    ),
                ]
            ),
            claim_resolution_service=ClaimResolutionService(llm_client=llm_client),
        )

        result = planner.plan_validated_claims(
            observation=self.observation,
            validation=self.validation,
            snapshot=self.snapshot,
        )

        self.assertEqual(len(llm_client.calls), 1)
        prompt = llm_client.calls[0]["messages"][1].content
        self.assertIn("claim:demo:thread", prompt)
        self.assertNotIn("claim:demo:global", prompt)
        support_edge = next(
            edge for edge in result.plan.upsert_edges if edge.relationship_type == "SUPPORTS_CLAIM"
        )
        self.assertEqual(support_edge.target_record_id, "claim:demo:thread")

    def test_cross_kind_same_thread_candidate_is_compared_for_resolution(self):
        observation = make_observation(
            text="Correction: `MAX_RETRIES` in `tasks.py` was left at 3 because retry count was not the issue.",
        )
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="MAX_RETRIES in tasks.py was left at 3 because retry count was not the issue.",
                claim_kind="state_claim",
                source_hint=None,
                quote_text="`MAX_RETRIES` in `tasks.py` was left at 3 because retry count was not the issue",
                span_start=None,
                span_end=None,
                evidence_kind="state_detail",
                support_rationale="This quote states that MAX_RETRIES stayed at 3 and explains why.",
            ),
            snapshot=None,
        )
        llm_client = PromptMapLLMClient(
            {
                "provenance.relate_claims.resolve_new_claim_against_candidates.v1": {
                    "relations": [
                        {
                            "candidate_claim_id": "claim:demo:max-retries-change",
                            "resolution_type": "contradicts",
                            "confidence": "high",
                            "rationale": "Leaving MAX_RETRIES at 3 conflicts with the earlier increase to 5.",
                        }
                    ]
                }
            }
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver(
                [
                    make_existing_claim_row(
                        claim_id="claim:demo:max-retries-change",
                        claim_text="`MAX_RETRIES` was increased from 3 to 5 in `tasks.py`.",
                        claim_kind="change_claim",
                        claim_subkind="fix_or_mitigation",
                        about_record_ids=(),
                    )
                ]
            ),
            claim_resolution_service=ClaimResolutionService(llm_client=llm_client),
        )

        result = planner.plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=None,
        )

        self.assertEqual(len(llm_client.calls), 1)
        prompt = llm_client.calls[0]["messages"][1].content
        self.assertIn("claim:demo:max-retries-change", prompt)
        self.assertTrue(
            any(edge.relationship_type == "CONTRADICTS" for edge in result.plan.upsert_edges)
        )

    def test_same_thread_lexical_candidate_without_about_overlap_is_compared(self):
        observation = make_observation(
            text="Rotated `SECRET_KEY` in `auth.py` to a new value for tighter security.",
        )
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="The `SECRET_KEY` in `auth.py` was rotated to a new value.",
                claim_kind="change_claim",
                source_hint=None,
                quote_text="Rotated `SECRET_KEY` in `auth.py` to a new value",
                span_start=None,
                span_end=None,
                evidence_kind="change_summary",
                support_rationale="This quote explicitly states that SECRET_KEY in auth.py was rotated.",
            ),
            snapshot=None,
        )
        llm_client = PromptMapLLMClient(
            {
                "provenance.relate_claims.resolve_new_claim_against_candidates.v1": {
                    "relations": [
                        {
                            "candidate_claim_id": "claim:demo:secret-thread",
                            "resolution_type": "same_as",
                            "confidence": "high",
                            "rationale": "These are equivalent statements about the same SECRET_KEY rotation.",
                        }
                    ]
                }
            }
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver(
                [
                    make_existing_claim_row(
                        claim_id="claim:demo:secret-thread",
                        claim_text="Rotated `SECRET_KEY` in `auth.py` to a fresh value.",
                        claim_kind="change_claim",
                        claim_subkind="fix_or_mitigation",
                        about_record_ids=(),
                    )
                ]
            ),
            claim_resolution_service=ClaimResolutionService(llm_client=llm_client),
        )

        result = planner.plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=None,
        )

        self.assertEqual(len(llm_client.calls), 1)
        prompt = llm_client.calls[0]["messages"][1].content
        self.assertIn("claim:demo:secret-thread", prompt)
        support_edge = next(
            edge for edge in result.plan.upsert_edges if edge.relationship_type == "SUPPORTS_CLAIM"
        )
        self.assertEqual(support_edge.target_record_id, "claim:demo:secret-thread")

    def test_cross_thread_lexical_candidate_is_compared_in_global_pass(self):
        observation = make_observation(
            text="Rotated `SECRET_KEY` in `auth.py` to a new value for tighter security.",
        )
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="The `SECRET_KEY` in `auth.py` was rotated to a new value.",
                claim_kind="change_claim",
                source_hint=None,
                quote_text="Rotated `SECRET_KEY` in `auth.py` to a new value",
                span_start=None,
                span_end=None,
                evidence_kind="change_summary",
                support_rationale="This quote explicitly states that SECRET_KEY in auth.py was rotated.",
            ),
            snapshot=None,
        )
        llm_client = PromptMapLLMClient(
            {
                "provenance.relate_claims.resolve_new_claim_against_candidates.v1": {
                    "relations": [
                        {
                            "candidate_claim_id": "claim:demo:secret-global",
                            "resolution_type": "same_as",
                            "confidence": "high",
                            "rationale": "These are equivalent statements about the same SECRET_KEY rotation.",
                        }
                    ]
                }
            }
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver(
                [
                    make_existing_claim_row(
                        claim_id="claim:demo:secret-global",
                        claim_text="Rotated `SECRET_KEY` in `auth.py` to a fresh value.",
                        claim_kind="change_claim",
                        claim_subkind="fix_or_mitigation",
                        source_kind="issue",
                        source_id="github:issue:demo:9",
                        thread_kind="issue",
                        thread_id="9",
                        about_record_ids=(),
                    )
                ]
            ),
            claim_resolution_service=ClaimResolutionService(llm_client=llm_client),
        )

        result = planner.plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=None,
        )

        self.assertEqual(len(llm_client.calls), 1)
        prompt = llm_client.calls[0]["messages"][1].content
        self.assertIn("claim:demo:secret-global", prompt)
        support_edge = next(
            edge for edge in result.plan.upsert_edges if edge.relationship_type == "SUPPORTS_CLAIM"
        )
        self.assertEqual(support_edge.target_record_id, "claim:demo:secret-global")

    def test_same_file_overlap_alone_does_not_trigger_resolution(self):
        observation = make_observation(
            text="Added logging to `tasks.py` for task execution visibility.",
        )
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="Added logging to `tasks.py` for task execution visibility.",
                claim_kind="change_claim",
                source_hint=None,
                quote_text="Added logging to `tasks.py` for task execution visibility",
                span_start=None,
                span_end=None,
                evidence_kind="change_summary",
                support_rationale="This quote describes the logging change in tasks.py.",
            ),
            snapshot=None,
        )
        llm_client = PromptMapLLMClient(
            {
                "provenance.relate_claims.resolve_new_claim_against_candidates.v1": {
                    "relations": []
                }
            }
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver(
                [
                    make_existing_claim_row(
                        claim_id="claim:demo:file-only",
                        claim_text="Refactored `tasks.py` imports for readability.",
                        claim_kind="change_claim",
                        claim_subkind="fix_or_mitigation",
                        source_kind="issue",
                        source_id="github:issue:demo:9",
                        thread_kind="issue",
                        thread_id="9",
                        about_record_ids=(),
                    )
                ]
            ),
            claim_resolution_service=ClaimResolutionService(llm_client=llm_client),
        )

        planner.plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=None,
        )

        self.assertEqual(llm_client.calls, [])

    def test_same_numeric_literal_alone_does_not_trigger_resolution(self):
        observation = make_observation(
            text="Configured retry jitter to 5 milliseconds in the scheduler.",
        )
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="Configured retry jitter to 5 milliseconds in the scheduler.",
                claim_kind="change_claim",
                source_hint=None,
                quote_text="Configured retry jitter to 5 milliseconds in the scheduler",
                span_start=None,
                span_end=None,
                evidence_kind="change_summary",
                support_rationale="This quote states the scheduler jitter value.",
            ),
            snapshot=None,
        )
        llm_client = PromptMapLLMClient(
            {
                "provenance.relate_claims.resolve_new_claim_against_candidates.v1": {
                    "relations": []
                }
            }
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver(
                [
                    make_existing_claim_row(
                        claim_id="claim:demo:number-only",
                        claim_text="Set worker concurrency to 5 for cache warmup.",
                        claim_kind="change_claim",
                        claim_subkind="fix_or_mitigation",
                        source_kind="issue",
                        source_id="github:issue:demo:9",
                        thread_kind="issue",
                        thread_id="9",
                        about_record_ids=(),
                    )
                ]
            ),
            claim_resolution_service=ClaimResolutionService(llm_client=llm_client),
        )

        planner.plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=None,
        )

        self.assertEqual(llm_client.calls, [])

    def test_semantic_profile_candidate_expansion_can_surface_claim_for_resolution(self):
        observation = make_observation(
            text="Auth now needs a refresh token for login.",
        )
        validation = ClaimBundleValidator().validate(
            observation=observation,
            extraction=make_extraction_response(
                claim_text="Auth now needs a refresh token for login.",
                claim_kind="state_claim",
                source_hint=None,
                quote_text="Auth now needs a refresh token for login",
                span_start=None,
                span_end=None,
                evidence_kind="change_summary",
                support_rationale="This quote states the refresh-token requirement.",
            ),
            snapshot=None,
        )
        existing_claim_row = make_existing_claim_row(
            claim_id="claim:demo:auth-refresh",
            claim_text="Authentication now requires a refresh token for login.",
            claim_kind="state_claim",
            claim_subkind="desired_behavior_or_requirement",
            about_record_ids=(),
            source_kind="issue",
            source_id="github:issue:demo:18",
            thread_kind="issue",
            thread_id="18",
        )
        metadata = InMemoryMetadataStore()
        existing_claim = ClaimRecord(
            claim_id="claim:demo:auth-refresh",
            repo_id="demo",
            canonical_key=canonical_claim_key(
                claim_kind="state_claim",
                claim_text="Authentication now requires a refresh token for login.",
                about_record_ids=(),
            ),
            claim_text="Authentication now requires a refresh token for login.",
            claim_kind="state_claim",
            claim_subkind="desired_behavior_or_requirement",
            first_observed_at="2026-03-25T00:00:00+00:00",
            latest_observed_at="2026-03-25T00:00:00+00:00",
            is_current=True,
            ingested_at="2026-03-28T00:00:00Z",
            valid_from="2026-03-25T00:00:00+00:00",
            thread_kind="issue",
            thread_id="18",
            metadata={},
        )
        metadata.upsert_profiles(
            (
                semantic_claim_profile_from_claim(
                    existing_claim,
                    generated_keys=("auth refresh token",),
                ),
            )
        )
        vector_store = InMemoryProfileVectorStore()
        indexer = ProfileVectorIndexer(
            vector_store=vector_store,
            embedding_provider=AuthEmbeddingProvider(),
        )
        indexer.sync_repo(repo_id="demo", metadata_store=metadata)
        candidate_retriever = ClaimResolutionCandidateRetriever(
            backend_driver=FakeBackendDriver([existing_claim_row]),
            semantic_claim_search_service=ClaimSemanticRetrievalService(
                metadata_store=metadata,
                vector_store=vector_store,
                embedding_provider=AuthEmbeddingProvider(),
                query_parser=FakeQueryParser(("auth refresh token",)),
            ),
        )
        llm_client = PromptMapLLMClient(
            {
                "provenance.relate_claims.resolve_new_claim_against_candidates.v1": {
                    "relations": [
                        {
                            "candidate_claim_id": "claim:demo:auth-refresh",
                            "resolution_type": "same_as",
                            "confidence": "high",
                            "rationale": "The semantic phrasing differs, but the requirement is the same.",
                        }
                    ]
                }
            }
        )
        planner = EvidenceInvalidationPlanner(
            backend_driver=FakeBackendDriver([existing_claim_row]),
            claim_candidate_retriever=candidate_retriever,
            claim_resolution_service=ClaimResolutionService(llm_client=llm_client),
        )

        result = planner.plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=None,
        )

        self.assertEqual(len(llm_client.calls), 1)
        prompt = llm_client.calls[0]["messages"][1].content
        self.assertIn("claim:demo:auth-refresh", prompt)
        support_edge = next(
            edge for edge in result.plan.upsert_edges if edge.relationship_type == "SUPPORTS_CLAIM"
        )
        self.assertEqual(support_edge.target_record_id, "claim:demo:auth-refresh")


class PromptScaffoldingTests(unittest.TestCase):
    def test_prompt_builders_return_message_lists_and_unique_prompt_names(self):
        observation_payload = {
            "observation_id": "observation:git_commit_message:demo:abc123",
            "repo_id": "demo",
            "observation_kind": "git_commit_message",
            "source_kind": "commit",
            "source_id": "commit:demo:abc123",
            "observed_at": "2026-03-26T00:00:05+00:00",
            "author": "Dev",
            "title": "Add helper support",
            "text": "Add helper support\n\nbody text",
            "metadata": {"commit_sha": "abc123"},
        }
        extraction_messages = extract_claims_and_evidence.from_commit_message(
            {"observation": observation_payload, "code_context": {}, "source_context": {}}
        )
        review_messages = extract_claims_and_evidence.from_github_pr_review(
            {"observation": observation_payload, "code_context": {}, "source_context": {}}
        )
        review_comment_messages = extract_claims_and_evidence.from_github_pr_review_comment(
            {"observation": observation_payload, "code_context": {}, "source_context": {}}
        )
        issue_comment_messages = extract_claims_and_evidence.from_github_issue_comment(
            {"observation": observation_payload, "code_context": {}, "source_context": {}}
        )
        claim_messages = normalize_claims.from_evidence_set(
            {"topic": {"scope": "demo"}, "evidence": [], "target_context": {}}
        )
        relation_messages = relate_claims.compare_claims({"claims": [{"claim_text": "A"}]})
        fact_conflict_messages = resolve_fact_conflicts.compare_against_current(
            {
                "new_fact": {"claim_text": "A", "claim_kind": "state_claim"},
                "current_facts": [{"candidate_id": "f1", "claim_text": "B", "claim_kind": "state_claim"}],
            }
        )
        artifact_messages = synthesize_research_artifact.from_code_and_evidence(
            {"repo_region": {"path": "app.py"}, "code_context": {}, "evidence": [], "claims": []}
        )

        self.assertEqual(len(extraction_messages), 2)
        self.assertEqual(len(review_messages), 2)
        self.assertEqual(len(review_comment_messages), 2)
        self.assertEqual(len(issue_comment_messages), 2)
        self.assertEqual(len(claim_messages), 2)
        self.assertEqual(len(relation_messages), 2)
        self.assertEqual(len(fact_conflict_messages), 2)
        self.assertEqual(len(artifact_messages), 2)


class ClaimSemanticProfileTests(unittest.TestCase):
    def test_semantic_claim_profile_includes_raw_text_and_generated_keys(self):
        claim = ClaimRecord(
            claim_id="claim:demo:secret",
            repo_id="demo",
            canonical_key="canonical:secret",
            claim_text="The `SECRET_KEY` in `auth.py` was rotated to a new value.",
            claim_kind="change_claim",
            claim_subkind="fix_or_mitigation",
            first_observed_at="2026-03-26T00:00:00+00:00",
            latest_observed_at="2026-03-26T00:00:00+00:00",
            is_current=True,
            ingested_at="2026-03-28T00:00:00Z",
            valid_from="2026-03-26T00:00:00+00:00",
            thread_kind="pull_request",
            thread_id="7",
            metadata={},
        )

        profile = semantic_claim_profile_from_claim(
            claim,
            about_record_ids=(
                "file:demo:demo.auth.__file__@r1",
                "variable:demo:demo.auth.SECRET_KEY@r1",
            ),
            support_texts=("Rotated `SECRET_KEY` in `auth.py` during incident response.",),
            generated_keys=("credential rotation",),
        )

        self.assertEqual(profile.profile_kind, SEMANTIC_CLAIM_PROFILE_KIND)
        self.assertEqual(profile.profile_target_id, claim.claim_id)
        self.assertEqual(profile.metadata["claim_id"], claim.claim_id)
        self.assertEqual(
            profile.keys[0],
            ProfileKeyDefinition(
                text="The `SECRET_KEY` in `auth.py` was rotated to a new value.",
                origin=PROFILE_KEY_ORIGIN_RAW_TEXT,
            ),
        )
        self.assertIn(
            ProfileKeyDefinition(
                text="credential rotation",
                origin=PROFILE_KEY_ORIGIN_GENERATED_SEMANTIC,
            ),
            profile.keys,
        )

    def test_semantic_relation_profile_uses_raw_text_only(self):
        profile = semantic_relation_profile_from_spec(
            RelationProfileSpec(
                repo_id="demo",
                profile_target_id="claim_relation:claim:1:claim:2:contradicts",
                relation_type="contradicts",
                source_record_id="claim:1",
                source_record_type="claim",
                target_record_id="claim:2",
                target_record_type="claim",
                value_text="  Leaving MAX_RETRIES at 3 contradicts the earlier increase to 5.  ",
                metadata={"resolution_confidence": "high"},
                updated_at="2026-03-26T00:00:00+00:00",
            )
        )

        self.assertIsNotNone(profile)
        assert profile is not None
        self.assertEqual(profile.profile_kind, SEMANTIC_RELATION_PROFILE_KIND)
        self.assertEqual(profile.profile_target_kind, "Relation")
        self.assertEqual(profile.relation_type, "contradicts")
        self.assertEqual(profile.source_node_id, "claim:1")
        self.assertEqual(profile.source_node_kind, "claim")
        self.assertEqual(profile.target_node_id, "claim:2")
        self.assertEqual(profile.target_node_kind, "claim")
        self.assertEqual(
            profile.keys,
            (
                ProfileKeyDefinition(
                    text="Leaving MAX_RETRIES at 3 contradicts the earlier increase to 5.",
                    origin=PROFILE_KEY_ORIGIN_RAW_TEXT,
                ),
            ),
        )
        self.assertEqual(
            profile.metadata["relation_record_id"],
            "claim_relation:claim:1:claim:2:contradicts",
        )
        self.assertEqual(profile.metadata["text_field"], "value_text")

    def test_semantic_relation_profile_returns_none_for_blank_text(self):
        profile = semantic_relation_profile_from_spec(
            RelationProfileSpec(
                repo_id="demo",
                profile_target_id="claim_relation:claim:1:claim:2:contradicts",
                relation_type="contradicts",
                source_record_id="claim:1",
                source_record_type="claim",
                target_record_id="claim:2",
                target_record_type="claim",
                value_text="   ",
                updated_at="2026-03-26T00:00:00+00:00",
            )
        )

        self.assertIsNone(profile)

    def test_claim_semantic_retrieval_service_filters_to_claim_profiles(self):
        metadata = InMemoryMetadataStore()
        claim = ClaimRecord(
            claim_id="claim:demo:secret",
            repo_id="demo",
            canonical_key="canonical:secret",
            claim_text="The `SECRET_KEY` in `auth.py` was rotated to a new value.",
            claim_kind="change_claim",
            claim_subkind="fix_or_mitigation",
            first_observed_at="2026-03-26T00:00:00+00:00",
            latest_observed_at="2026-03-26T00:00:00+00:00",
            is_current=True,
            ingested_at="2026-03-28T00:00:00Z",
            valid_from="2026-03-26T00:00:00+00:00",
            thread_kind="pull_request",
            thread_id="7",
            metadata={},
        )
        claim_profile = semantic_claim_profile_from_claim(
            claim,
            about_record_ids=(
                "file:demo:demo.auth.__file__@r1",
                "variable:demo:demo.auth.SECRET_KEY@r1",
            ),
            support_texts=("Rotated `SECRET_KEY` in `auth.py` during incident response.",),
        )
        fact_profile = ProfileRecord(
            profile_id="profile:demo:semantic_fact_profile:fact:1",
            repo_id="demo",
            profile_kind="semantic_fact_profile",
            profile_target_kind="FactEdge",
            profile_target_id="fact:1",
            relation_type="JUSTIFIES",
            source_node_id="symbol:demo:demo.auth.main",
            source_node_kind="Function",
            target_node_id="symbol:demo:demo.auth.rotate",
            target_node_kind="Function",
            target_revision_id=None,
            value_text="auth rotation was documented elsewhere",
            keys=(
                ProfileKeyDefinition(
                    "SECRET_KEY auth.py rotated",
                    PROFILE_KEY_ORIGIN_RAW_TEXT,
                ),
            ),
            metadata={"fact_id": "fact:1"},
            is_active=True,
            updated_at="2026-03-26T00:00:00+00:00",
        )
        metadata.upsert_profiles((claim_profile, fact_profile))

        vector_store = InMemoryProfileVectorStore()
        indexer = ProfileVectorIndexer(
            vector_store=vector_store,
            embedding_provider=FakeEmbeddingProvider(),
        )
        indexed = indexer.sync_repo(repo_id="demo", metadata_store=metadata)

        service = ClaimSemanticRetrievalService(
            metadata_store=metadata,
            vector_store=vector_store,
            embedding_provider=FakeEmbeddingProvider(),
            query_parser=FakeQueryParser(("SECRET_KEY auth.py rotated",)),
        )
        result = service.search(
            repo_id="demo",
            query="Where was SECRET_KEY rotated?",
            limit=5,
        )

        self.assertGreater(indexed, 0)
        self.assertEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].claim_id, claim.claim_id)
        self.assertEqual(result.hits[0].profile_id, claim_profile.profile_id)
        self.assertEqual(result.profiles[0].profile_kind, SEMANTIC_CLAIM_PROFILE_KIND)
        prompt_names = {
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_COMMIT_MESSAGE.prompt_name,
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_PR_BODY.prompt_name,
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_ISSUE_BODY.prompt_name,
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_PR_REVIEW.prompt_name,
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_PR_REVIEW_COMMENT.prompt_name,
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GITHUB_ISSUE_COMMENT.prompt_name,
            extract_claims_and_evidence.EXTRACT_CLAIMS_AND_EVIDENCE_FROM_GENERIC_OBSERVATION.prompt_name,
            normalize_claims.NORMALIZE_CLAIMS_FROM_EVIDENCE_SET.prompt_name,
            relate_claims.RELATE_CLAIMS_COMPARE.prompt_name,
            resolve_fact_conflicts.RESOLVE_FACT_CONFLICTS_COMPARE.prompt_name,
            synthesize_research_artifact.SYNTHESIZE_RESEARCH_ARTIFACT_FROM_CODE_AND_EVIDENCE.prompt_name,
        }
        self.assertEqual(len(prompt_names), 11)

    def test_response_models_validate_structured_outputs(self):
        extraction = make_extraction_response()
        claims = ClaimDerivationResponse.model_validate(
            {
                "claims": [
                    {
                        "claim_text": "Helper support was added.",
                        "claim_kind": "change_claim",
                        "about_hints": ["demo.app.main"],
                        "supported_evidence_ids": ["e1"],
                        "derivation_kind": "normalized_from_evidence",
                        "metadata": {},
                    }
                ]
            }
        )
        relations = ClaimRelationResponse.model_validate(
            {
                "relations": [
                    {
                        "relation_type": "supports",
                        "relation_rationale": "Both claims say the same thing.",
                        "confidence_note": "high overlap",
                        "metadata": {},
                    }
                ]
            }
        )
        artifacts = ResearchArtifactSynthesisResponse.model_validate(
            {
                "artifacts": [
                    {
                        "artifact_kind": "subsystem_summary",
                        "title": "Helper Rollout",
                        "summary": "Summarizes the helper rollout.",
                        "key_points": ["Helper support exists."],
                        "evidence_refs": ["e1"],
                        "about_refs": ["demo.app.main"],
                        "metadata": {},
                    }
                ]
            }
        )

        self.assertEqual(len(extraction.claims), 1)
        self.assertEqual(len(extraction.claims[0].supporting_evidence), 1)
        self.assertEqual(len(claims.claims), 1)
        self.assertEqual(len(relations.relations), 1)
        self.assertEqual(len(artifacts.artifacts), 1)

        evidence_dedup = EvidenceDedupResult.model_validate(
            {
                "groups": [
                    {
                        "canonical_key": "obs-1|change_summary",
                        "candidates": [
                            {
                                "observation_id": "obs-1",
                                "evidence_kind": "change_summary",
                                "quote_text": "Add helper support",
                                "summary_text": "The commit adds helper support.",
                                "span_start": 0,
                                "span_end": 18,
                                "support_kind": "direct_statement",
                                "about_record_ids": ["symbol:demo:demo.app.main@r1"],
                                "claim_kind": "change_claim",
                                "claim_text": "Helper support was added.",
                                "metadata": {},
                            }
                        ],
                    }
                ]
            }
        )
        claim_canonicalization = ClaimCanonicalizationResult.model_validate(
            {
                "groups": [
                    {
                        "canonical_key": "change_claim|helper support was added.|symbol:demo:demo.app.main@r1",
                        "candidates": [
                            {
                                "observation_id": "obs-1",
                                "claim_text": "Helper support was added.",
                                "claim_kind": "change_claim",
                                "evidence_ids": ["e1"],
                                "about_record_ids": ["symbol:demo:demo.app.main@r1"],
                                "metadata": {},
                            }
                        ],
                    }
                ]
            }
        )

        self.assertEqual(len(evidence_dedup.groups), 1)
        self.assertEqual(len(claim_canonicalization.groups), 1)

    def test_services_build_typed_prompt_requests(self):
        observation = Observation(
            observation_id="observation:git_commit_message:demo:abc123",
            repo_id="demo",
            observation_kind=ObservationKind.GIT_COMMIT_MESSAGE,
            source_kind="commit",
            source_id="commit:demo:abc123",
            observed_at="2026-03-26T00:00:05+00:00",
            author="Dev",
            title="Add helper support",
            text="Add helper support\n\nbody text",
            metadata={"commit_sha": "abc123"},
        )
        extraction_request = ClaimEvidenceExtractionService().build_request(observation)
        claim_request = ClaimDerivationService().build_request(
            [
                EvidenceCandidate(
                    evidence_kind="change_summary",
                    quote_text="Add helper support",
                    summary_text="The commit adds helper support.",
                    about_hints=["demo.app.main"],
                    support_kind="direct_statement",
                )
            ]
        )
        relation_request = ClaimRelationService().build_request(
            [
                ClaimCandidate(
                    claim_text="Helper support was added.",
                    claim_kind="change_claim",
                    about_hints=["demo.app.main"],
                    supported_evidence_ids=["e1"],
                    derivation_kind="normalized_from_evidence",
                ),
                ClaimCandidate(
                    claim_text="The helper exists in main.",
                    claim_kind="state_claim",
                    about_hints=["demo.app.main"],
                    supported_evidence_ids=["e1"],
                    derivation_kind="normalized_from_evidence",
                ),
            ]
        )
        conflict_request = FactConflictResolutionService().build_request(
            new_fact={"claim_text": "Helper support was added.", "claim_kind": "change_claim"},
            current_facts=[
                {
                    "candidate_id": "f1",
                    "claim_text": "Helper support was removed.",
                    "claim_kind": "change_claim",
                }
            ],
        )
        artifact_request = ResearchArtifactSynthesisService().build_request(
            repo_region={"path": "app.py"},
            code_context={"symbols": ["demo.app.main"]},
            evidence=[
                EvidenceCandidate(
                    evidence_kind="change_summary",
                    quote_text="Add helper support",
                    summary_text="The commit adds helper support.",
                    about_hints=["demo.app.main"],
                    support_kind="direct_statement",
                )
            ],
        )

        self.assertEqual(
            extraction_request.prompt_name,
            "provenance.extract_claims_and_evidence.from_commit_message.v1",
        )
        self.assertIs(extraction_request.response_model, ClaimEvidenceExtractionResponse)
        self.assertIs(claim_request.response_model, ClaimDerivationResponse)
        self.assertIs(relation_request.response_model, ClaimRelationResponse)
        self.assertEqual(
            conflict_request.prompt_name,
            "provenance.resolve_fact_conflicts.compare_against_current.v1",
        )
        self.assertIs(artifact_request.response_model, ResearchArtifactSynthesisResponse)

    def test_extraction_request_sanitizes_internal_fields(self):
        observation = Observation(
            observation_id="observation:github_pr_body:demo:7",
            repo_id="demo",
            observation_kind=ObservationKind.GITHUB_PR_BODY,
            source_kind="pull_request",
            source_id="github:pr:demo:7",
            observed_at="2026-03-26T00:01:00+00:00",
            author="octocat",
            title="Tighten rollback handling",
            text="Updated `init_db()` in `db.py` to pass `CONNECTION_TIMEOUT`.",
            metadata={
                "pull_request_number": 7,
                "source_id": "should-not-leak",
                "record_id": "backend-record-123",
                "revision_id": "r123",
                "changed_files": ["db.py"],
            },
        )

        request = ClaimEvidenceExtractionService().build_request(
            observation,
            source_context={
                "pull_request_number": 7,
                "source_id": "source-should-not-leak",
                "pr_title": "Tighten rollback handling",
            },
            code_context={
                "changed_files": ["db.py"],
                "mentioned_entities": [
                    {
                        "name": "init_db",
                        "kind": "Function",
                        "file_path": "db.py",
                        "node_id": "symbol:demo:init_db",
                    }
                ],
                "touched_entities": [
                    {
                        "name": "db.py",
                        "kind": "File",
                        "file_path": "db.py",
                        "record_id": "record:db",
                    }
                ],
                "revision_id": "r123",
            },
        )
        prompt = request.messages[1].content

        self.assertIn('"source_type": "github_pr_body"', prompt)
        self.assertIn('"pr_number": 7', prompt)
        self.assertIn('"mentioned_entities"', prompt)
        self.assertIn('"name": "init_db"', prompt)
        self.assertNotIn('"observation_id"', prompt)
        self.assertNotIn('"repo_id"', prompt)
        self.assertNotIn('"source_id"', prompt)
        self.assertNotIn('"record_id"', prompt)
        self.assertNotIn('"revision_id"', prompt)
        self.assertNotIn('"node_id"', prompt)

    def test_dedup_scaffolding_services_pregroup_deterministically(self):
        evidence_service = EvidenceDedupService()
        claim_service = ClaimCanonicalizationService()

        evidence_result = evidence_service.pregroup(
            [
                EvidenceDedupCandidate(
                    observation_id="obs-1",
                    evidence_kind="change_summary",
                    quote_text="Add helper support",
                    summary_text="The commit adds helper support.",
                    about_record_ids=["symbol:demo:demo.app.main@r1"],
                    support_kind="direct_statement",
                    claim_kind="change_claim",
                    claim_text="Helper support was added.",
                ),
                EvidenceDedupCandidate(
                    observation_id="obs-1",
                    evidence_kind="change_summary",
                    quote_text="  Add   helper support ",
                    summary_text="The commit adds helper support.",
                    about_record_ids=["symbol:demo:demo.app.main@r1"],
                    support_kind="direct_statement",
                    claim_kind="change_claim",
                    claim_text="  Helper support   was added. ",
                ),
            ]
        )

        self.assertEqual(len(evidence_result.groups), 1)
        self.assertEqual(len(evidence_result.groups[0].candidates), 2)

        claim_result = claim_service.pregroup(
            [
                ClaimDedupCandidate(
                    observation_id="obs-1",
                    claim_text="Helper support was added.",
                    claim_kind="change_claim",
                    evidence_ids=["e1"],
                    about_record_ids=["symbol:demo:demo.app.main@r1"],
                ),
                ClaimDedupCandidate(
                    observation_id="obs-2",
                    claim_text="  Helper support   was added. ",
                    claim_kind="change_claim",
                    evidence_ids=["e2"],
                    about_record_ids=["symbol:demo:demo.app.main@r1"],
                ),
            ]
        )

        self.assertEqual(len(claim_result.groups), 1)
        self.assertEqual(len(claim_result.groups[0].candidates), 2)

    def test_claim_evidence_extraction_service_executes_with_llm_client(self):
        llm_client = FakeLLMClient(
            {
                "claims": [
                    {
                        "claim_text": "A regression test is required.",
                        "claim_subkind": "reviewer_objection_or_requested_change",
                        "about_hints": ["demo.app.main"],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "review_feedback",
                                "quote_text": "Please add a regression test.",
                                "summary_text": "The reviewer asks for a regression test.",
                                "span_start": 0,
                                "span_end": 29,
                                "about_hints": ["demo.app.main"],
                                "support_kind": "direct_statement",
                                "metadata": {},
                            }
                        ],
                    }
                ]
            }
        )
        service = ClaimEvidenceExtractionService(llm_client=llm_client)
        observation = Observation(
            observation_id="observation:github_pr_review:demo:800",
            repo_id="demo",
            observation_kind=ObservationKind.GITHUB_PR_REVIEW,
            source_kind="pull_request",
            source_id="github:pr:demo:7",
            observed_at="2026-03-26T00:02:00+00:00",
            author="reviewer",
            title="Review on PR #7: Tighten rollback handling",
            text="Please add a regression test.",
            metadata={"review_id": 800},
        )

        response = service.extract(observation)

        self.assertEqual(len(response.claims), 1)
        self.assertEqual(len(response.claims[0].supporting_evidence), 1)
        self.assertEqual(
            llm_client.calls[0]["prompt_name"],
            "provenance.extract_claims_and_evidence.from_github_pr_review.v1",
        )
        self.assertIs(llm_client.calls[0]["response_model"], ClaimEvidenceExtractionResponse)

    def test_fact_conflict_resolution_service_executes_with_llm_client(self):
        llm_client = FakeLLMClient({"contradicted_fact_ids": ["fact-1"]})
        service = FactConflictResolutionService(llm_client=llm_client)

        response = service.resolve(
            repo_id="demo",
            new_fact={"claim_text": "demo.app.main is enabled.", "claim_kind": "state_claim"},
            current_facts=[
                {
                    "candidate_id": "fact-1",
                    "claim_text": "demo.app.main is disabled.",
                    "claim_kind": "state_claim",
                }
            ],
        )

        self.assertEqual(response.contradicted_fact_ids, ["fact-1"])
        self.assertEqual(
            llm_client.calls[0]["prompt_name"],
            "provenance.resolve_fact_conflicts.compare_against_current.v1",
        )


class ClaimProfileKeyGenerationServiceTests(unittest.TestCase):
    def test_filters_duplicate_and_code_anchor_like_generated_keys(self):
        service = ClaimProfileKeyGenerationService(
            llm_client=FakeLLMClient(
                {
                    "retrieval_keys": [
                        "credential rotation",
                        "src/auth.py",
                        "AUTH_SECRET",
                        "credential rotation",
                        "The SECRET_KEY was rotated",
                    ]
                }
            )
        )

        keys = service.generate(
            repo_id="demo",
            claim_text="The SECRET_KEY was rotated",
            claim_subkind="fix_or_mitigation",
            support_texts=("Rotated SECRET_KEY in auth.py during incident response.",),
        )

        self.assertEqual(keys, ("credential rotation",))

    def test_returns_empty_tuple_when_generation_fails(self):
        class RaisingLLM:
            async def generate_response(self, messages, **kwargs):
                _ = messages, kwargs
                raise RuntimeError("boom")

        service = ClaimProfileKeyGenerationService(llm_client=RaisingLLM())
        self.assertEqual(
            service.generate(
                repo_id="demo",
                claim_text="Authentication now requires a refresh token.",
                claim_subkind="rollout_migration_or_dependency",
                support_texts=("Authentication now requires a refresh token.",),
            ),
            (),
        )


class ObservationSemanticProfileTests(unittest.TestCase):
    def test_observation_profile_uses_title_plus_body_and_truncates(self):
        profile = semantic_observation_profile_from_observation(
            Observation(
                observation_id="observation:demo:1",
                repo_id="demo",
                observation_kind=ObservationKind.GITHUB_PR_BODY,
                source_kind="pull_request",
                source_id="github:pr:demo:7",
                observed_at="2026-03-26T00:00:00+00:00",
                author="octocat",
                title="Authentication follow-up",
                text="  auth now requires token rotation.  " + ("x" * 5000),
                metadata={},
                ingested_at="2026-03-28T00:00:00Z",
                thread_kind="pull_request",
                thread_id="7",
            )
        )

        self.assertEqual(profile.profile_kind, SEMANTIC_OBSERVATION_PROFILE_KIND)
        self.assertTrue(profile.value_text.startswith("Authentication follow-up\n\nauth now requires token rotation."))
        self.assertEqual(len(profile.value_text), 4000)
        self.assertEqual(
            profile.keys,
            (
                ProfileKeyDefinition(
                    text=profile.value_text,
                    origin=PROFILE_KEY_ORIGIN_RAW_TEXT,
                ),
            ),
        )


class SemanticProvenanceSearchTests(unittest.TestCase):
    def test_fused_search_returns_claims_before_observations(self):
        metadata = InMemoryMetadataStore()
        claim = ClaimRecord(
            claim_id="claim:demo:auth-1",
            repo_id="demo",
            canonical_key="canonical:auth-1",
            claim_text="Authentication now requires a refresh token.",
            claim_kind="change_claim",
            claim_subkind="rollout_migration_or_dependency",
            first_observed_at="2026-03-26T00:00:00+00:00",
            latest_observed_at="2026-03-26T00:00:00+00:00",
            is_current=True,
            ingested_at="2026-03-28T00:00:00Z",
            valid_from="2026-03-26T00:00:00+00:00",
            metadata={},
        )
        claim_profile = semantic_claim_profile_from_claim(
            claim,
            generated_keys=("auth flow",),
        )
        observation_profile = semantic_observation_profile_from_observation(
            Observation(
                observation_id="observation:demo:auth-obs",
                repo_id="demo",
                observation_kind=ObservationKind.GITHUB_PR_BODY,
                source_kind="pull_request",
                source_id="github:pr:demo:8",
                observed_at="2026-03-26T00:05:00+00:00",
                author="octocat",
                title="Auth migration",
                text="Authentication now requires a refresh token for login sessions.",
                metadata={},
                ingested_at="2026-03-28T00:00:00Z",
                thread_kind="pull_request",
                thread_id="8",
            )
        )
        metadata.upsert_profiles((claim_profile, observation_profile))

        vector_store = InMemoryProfileVectorStore()
        indexer = ProfileVectorIndexer(
            vector_store=vector_store,
            embedding_provider=AuthEmbeddingProvider(),
        )
        indexer.sync_repo(repo_id="demo", metadata_store=metadata)

        base_search = ProfileSearchService(
            metadata_store=metadata,
            vector_store=vector_store,
            embedding_provider=AuthEmbeddingProvider(),
            query_parser=FakeQueryParser(("authentication migration",)),
        )
        fused_search = SemanticProvenanceSearchService(
            profile_search_service=base_search,
        )

        result = fused_search.search(
            repo_id="demo",
            query="auth migration",
            limit=5,
        )

        self.assertEqual(result.profiles[0].profile_kind, SEMANTIC_CLAIM_PROFILE_KIND)
        self.assertEqual(result.profiles[1].profile_kind, SEMANTIC_OBSERVATION_PROFILE_KIND)
        self.assertEqual(result.hits[0].key_origin, PROFILE_KEY_ORIGIN_GENERATED_SEMANTIC)


class IngestionSemanticProfilePersistenceTests(unittest.TestCase):
    def test_commit_indexing_persists_profiles_only_when_execute_true(self):
        snapshot = make_snapshot()
        commit = make_commit(
            sha="r1",
            parents=(),
            changed_files=("app.py",),
            subject="Authentication now requires a refresh token.",
            body="",
        )
        prompt_map = {
            "provenance.extract_claims_and_evidence.from_commit_message.v1": {
                "claims": [
                    {
                        "claim_text": "Authentication now requires a refresh token.",
                        "claim_subkind": "rollout_migration_or_dependency",
                        "about_hints": ["demo.app.main"],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "change_summary",
                                "quote_text": "Authentication now requires a refresh token.",
                                "summary_text": "The commit updates authentication requirements.",
                                "span_start": None,
                                "span_end": None,
                                "about_hints": ["demo.app.main"],
                                "support_kind": "direct_statement",
                                "metadata": {},
                            }
                        ],
                    }
                ]
            },
            "context_graph.provenance.generate_claim_profile_keys": {
                "retrieval_keys": ["auth migration"]
            },
        }

        preview_metadata = InMemoryMetadataStore()
        preview_indexer = FakeProfileIndexer()
        preview_service = CommitIndexingService(
            metadata_store=preview_metadata,
            backend_driver=None,
            snapshot_producer=FakeSnapshotProducer(snapshot),
            git_metadata=FakeGitMetadata(commit),
            llm_client=PromptMapLLMClient(prompt_map),
            profile_indexer=preview_indexer,
        )
        preview_service.apply_commit(
            "/tmp/demo",
            commit_sha="r1",
            repo_id="demo",
            execute=False,
        )
        self.assertEqual(preview_metadata.load_profiles("demo"), ())
        self.assertEqual(preview_indexer.calls, [])

        execute_metadata = InMemoryMetadataStore()
        execute_indexer = FakeProfileIndexer()
        real_writer = TemporalGraphWriter(
            metadata_store=execute_metadata,
            backend_driver=None,
        )

        class StubWriter:
            def plan_snapshot(self, snapshot):
                return real_writer.plan_snapshot(snapshot)

            def apply_planned_snapshot(self, snapshot, diff, operations, *, execute=False):
                _ = execute
                real_writer.finalize_snapshot(snapshot, diff)
                from context_graph.models import SnapshotApplyResult

                return SnapshotApplyResult(
                    snapshot=snapshot,
                    diff=diff,
                    operations_emitted=operations.operation_count(),
                    operations_applied=operations.operation_count(),
                )

        execute_service = CommitIndexingService(
            metadata_store=execute_metadata,
            backend_driver=None,
            snapshot_producer=FakeSnapshotProducer(snapshot),
            git_metadata=FakeGitMetadata(commit),
            writer=StubWriter(),
            llm_client=PromptMapLLMClient(prompt_map),
            profile_indexer=execute_indexer,
        )
        execute_service.apply_commit(
            "/tmp/demo",
            commit_sha="r1",
            repo_id="demo",
            execute=True,
        )

        stored_profiles = execute_metadata.load_profiles("demo")
        self.assertEqual(
            {profile.profile_kind for profile in stored_profiles},
            {SEMANTIC_CLAIM_PROFILE_KIND, SEMANTIC_OBSERVATION_PROFILE_KIND},
        )
        self.assertEqual(len(execute_indexer.calls), 1)

    def test_github_indexing_persists_profiles_only_when_execute_true(self):
        metadata = InMemoryMetadataStore()
        github_api = FakeGitHubApi(
            commit_prs=[{"number": 7}],
            pull_request_payload={
                "id": 700,
                "number": 7,
                "html_url": "https://github.com/octo/demo/pull/7",
                "title": "Auth migration",
                "state": "open",
                "user": {"login": "octocat"},
                "created_at": "2026-03-26T00:00:00+00:00",
                "updated_at": "2026-03-26T00:01:00+00:00",
                "body": "Authentication now requires a refresh token.",
            }
        )
        prompt_map = {
            "provenance.extract_claims_and_evidence.from_github_pr_body.v1": {
                "claims": [
                    {
                        "claim_text": "Authentication now requires a refresh token.",
                        "claim_subkind": "rollout_migration_or_dependency",
                        "about_hints": ["demo.app.main"],
                        "confidence": "high",
                        "derivation_kind": "direct_from_observation",
                        "metadata": {},
                        "supporting_evidence": [
                            {
                                "evidence_kind": "change_summary",
                                "quote_text": "Authentication now requires a refresh token.",
                                "summary_text": "The PR updates authentication requirements.",
                                "span_start": None,
                                "span_end": None,
                                "about_hints": ["demo.app.main"],
                                "support_kind": "direct_statement",
                                "metadata": {},
                            }
                        ],
                    }
                ]
            },
            "context_graph.provenance.generate_claim_profile_keys": {
                "retrieval_keys": ["auth migration"]
            },
        }
        preview_indexer = FakeProfileIndexer()
        preview_service = GitHubIndexer(
            github_api=github_api,
            metadata_store=metadata,
            backend_driver=FakeBackendLifecycleDriver(),
            llm_client=PromptMapLLMClient(prompt_map),
            profile_indexer=preview_indexer,
        )
        repo_ref = GitHubRepoRef(
            owner="octo",
            name="demo",
            canonical_repo="octo/demo",
            clone_url="https://github.com/octo/demo.git",
            html_url="https://github.com/octo/demo",
            default_branch="main",
            source="api",
        )

        preview_service.sync_for_commits(
            repo_id="demo",
            repo_ref=repo_ref,
            commit_shas=("abc1234",),
            snapshot=make_snapshot(),
            execute=False,
        )
        self.assertEqual(metadata.load_profiles("demo"), ())
        self.assertEqual(preview_indexer.calls, [])

        execute_indexer = FakeProfileIndexer()
        execute_service = GitHubIndexer(
            github_api=github_api,
            metadata_store=metadata,
            backend_driver=FakeBackendLifecycleDriver(),
            llm_client=PromptMapLLMClient(prompt_map),
            profile_indexer=execute_indexer,
        )
        execute_service.sync_for_commits(
            repo_id="demo",
            repo_ref=repo_ref,
            commit_shas=("abc1234",),
            snapshot=make_snapshot(),
            execute=True,
        )

        stored_profiles = metadata.load_profiles("demo")
        self.assertEqual(
            {profile.profile_kind for profile in stored_profiles},
            {SEMANTIC_CLAIM_PROFILE_KIND, SEMANTIC_OBSERVATION_PROFILE_KIND},
        )
        self.assertEqual(len(execute_indexer.calls), 1)


class IngestionInvalidationTests(unittest.TestCase):
    def test_commit_evidence_planning_reports_invalidated_count(self):
        llm_client = PromptMapLLMClient(
            {
                "provenance.extract_claims_and_evidence.from_commit_message.v1": {
                    "claims": [
                        {
                            "claim_text": "demo.app.main is enabled.",
                            "claim_subkind": "invariant_or_constraint",
                            "about_hints": ["demo.app.main"],
                            "confidence": "high",
                            "derivation_kind": "direct_from_observation",
                            "metadata": {},
                            "supporting_evidence": [
                                {
                                    "evidence_kind": "state_detail",
                                    "quote_text": "demo.app.main is enabled",
                                    "summary_text": "The commit says demo.app.main is enabled.",
                                    "span_start": None,
                                    "span_end": None,
                                    "about_hints": ["demo.app.main"],
                                    "support_kind": "direct_statement",
                                    "metadata": {},
                                }
                            ],
                        }
                    ]
                },
                "provenance.resolve_fact_conflicts.compare_against_current.v1": {
                    "contradicted_fact_ids": [
                        canonical_claim_key(
                            claim_kind="state_claim",
                            claim_text="demo.app.main is disabled.",
                        )
                    ]
                },
            }
        )
        service = CommitIndexingService(
            metadata_store=FakeMetadataStore(),
            backend_driver=FakeBackendDriver(
                [
                    make_existing_evidence_row(
                        claim_text="demo.app.main is disabled.",
                        claim_kind="state_claim",
                        observed_at="2026-03-25T00:00:00+00:00",
                    )
                ]
            ),
            llm_client=llm_client,
        )
        commit = make_commit(subject="Enable main", body="demo.app.main is enabled")
        planned_observation = ObservationMutationPlanner().build_commit_message_observation(
            repo_id="demo",
            commit=commit,
        )

        plan, counts = service._plan_commit_evidence(
            planned_observation=planned_observation,
            snapshot=make_snapshot(),
            commit=commit,
        )

        self.assertEqual(counts["invalidated"], 1)
        self.assertTrue(
            any(edge.relationship_type == "CONTRADICTS" for edge in plan.upsert_edges)
        )

    def test_github_evidence_planning_reports_invalidated_count(self):
        llm_client = PromptMapLLMClient(
            {
                "provenance.extract_claims_and_evidence.from_github_pr_body.v1": {
                    "claims": [
                        {
                            "claim_text": "demo.app.main is enabled.",
                            "claim_subkind": "invariant_or_constraint",
                            "about_hints": ["demo.app.main"],
                            "confidence": "high",
                            "derivation_kind": "direct_from_observation",
                            "metadata": {},
                            "supporting_evidence": [
                                {
                                    "evidence_kind": "state_detail",
                                    "quote_text": "demo.app.main is enabled",
                                    "summary_text": "The PR says demo.app.main is enabled.",
                                    "span_start": None,
                                    "span_end": None,
                                    "about_hints": ["demo.app.main"],
                                    "support_kind": "direct_statement",
                                    "metadata": {},
                                }
                            ],
                        }
                    ]
                },
                "provenance.resolve_fact_conflicts.compare_against_current.v1": {
                    "contradicted_fact_ids": [
                        canonical_claim_key(
                            claim_kind="state_claim",
                            claim_text="demo.app.main is disabled.",
                        )
                    ]
                },
            }
        )
        indexer = GitHubIndexer(
            github_api=object(),
            backend_driver=FakeBackendDriver(
                [
                    make_existing_evidence_row(
                        claim_text="demo.app.main is disabled.",
                        claim_kind="state_claim",
                        observed_at="2026-03-25T00:00:00+00:00",
                    )
                ]
            ),
            llm_client=llm_client,
        )
        planned_observations = ObservationMutationPlanner().build_github_observations(
            bundle=GitHubProvenanceBundle(
                repo_id="demo",
                github_repo="octo/demo",
                entities=(),
                project_links=(),
                commit_links=(),
                entity_links=(),
                observation_sources=(
                    GitHubObservationSource(
                        observation_kind=ObservationKind.GITHUB_PR_BODY,
                        source_kind="pull_request",
                        source_id="github:pr:demo:7",
                        source_record_type="github_pull_request",
                        external_id="700",
                        number=7,
                        url="https://github.com/octo/demo/pull/7",
                        title="Enable main",
                        author_login="octocat",
                        created_at="2026-03-26T00:00:00+00:00",
                        updated_at="2026-03-26T00:01:00+00:00",
                        text="demo.app.main is enabled",
                        metadata={"pull_request_number": 7},
                    ),
                ),
            )
        )

        plan, counts = indexer._plan_evidence_operations(
            planned_observations=planned_observations,
            snapshot=make_snapshot(),
        )

        self.assertEqual(counts["invalidated"], 1)
        self.assertTrue(
            any(edge.relationship_type == "CONTRADICTS" for edge in plan.upsert_edges)
        )

    def test_github_evidence_planning_reuses_pending_claim_session_across_observations(self):
        bundle = GitHubProvenanceBundle(
            repo_id="demo",
            github_repo="octo/demo",
            entities=(),
            project_links=(),
            commit_links=(),
            entity_links=(),
            observation_sources=(
                GitHubObservationSource(
                    observation_kind=ObservationKind.GITHUB_PR_BODY,
                    source_kind="pull_request",
                    source_id="github:pr:demo:7",
                    source_record_type="github_pull_request",
                    external_id="700",
                    number=7,
                    url="https://github.com/octo/demo/pull/7",
                    title="Adjust retries",
                    author_login="octocat",
                    created_at="2026-03-26T00:00:00+00:00",
                    updated_at="2026-03-26T00:01:00+00:00",
                    text="`MAX_RETRIES` was increased from 3 to 5 in `tasks.py` due to database reconnect delay issues.",
                    metadata={"pull_request_number": 7},
                ),
                GitHubObservationSource(
                    observation_kind=ObservationKind.GITHUB_PR_REVIEW,
                    source_kind="pull_request",
                    source_id="github:pr:demo:7",
                    source_record_type="github_pull_request",
                    external_id="701",
                    number=7,
                    url="https://github.com/octo/demo/pull/7#pullrequestreview-701",
                    title="Review on PR #7: Adjust retries",
                    author_login="reviewer",
                    created_at="2026-03-26T00:02:00+00:00",
                    updated_at="2026-03-26T00:02:00+00:00",
                    text="Correction: `MAX_RETRIES` in `tasks.py` was left at 3.",
                    metadata={"review_id": 701},
                ),
            ),
        )
        planned_observations = ObservationMutationPlanner().build_github_observations(bundle=bundle)
        body_extraction = {
            "claims": [
                {
                    "claim_text": "MAX_RETRIES was increased to 5 due to database reconnect delay issues.",
                    "claim_subkind": "fix_or_mitigation",
                    "assertion_status": "observed",
                    "claim_frame": "author_statement",
                    "about_hints": [],
                    "confidence": "high",
                    "derivation_kind": "direct_from_observation",
                    "metadata": {},
                    "supporting_evidence": [
                        {
                            "evidence_kind": "change_summary",
                            "quote_text": "`MAX_RETRIES` was increased from 3 to 5 in `tasks.py` due to database reconnect delay issues.",
                            "summary_text": "The PR says MAX_RETRIES was increased.",
                            "span_start": None,
                            "span_end": None,
                            "about_hints": [],
                            "support_kind": "direct_statement",
                            "support_source": "observation_text",
                            "support_role": "states_change",
                            "support_rationale": "This quote states that MAX_RETRIES was increased to 5.",
                            "metadata": {},
                        }
                    ],
                }
            ]
        }
        def resolve_pending_batch_claim(*, messages, **kwargs):
            _ = kwargs
            prompt = messages[1].content
            candidates_json = prompt.split("Candidate existing claims:\n", 1)[1].split(
                "\n\nFor each candidate claim",
                1,
            )[0]
            candidates = json.loads(candidates_json)
            return {
                "relations": [
                    {
                        "candidate_claim_id": candidates[0]["candidate_claim_id"],
                        "resolution_type": "contradicts",
                        "confidence": "high",
                        "rationale": "Leaving MAX_RETRIES at 3 contradicts the earlier increase to 5.",
                    }
                ]
            }

        llm_client = PromptMapLLMClient(
            {
                "provenance.extract_claims_and_evidence.from_github_pr_body.v1": body_extraction,
                "provenance.extract_claims_and_evidence.from_github_pr_review.v1": {
                    "claims": [
                        {
                            "claim_text": "MAX_RETRIES in tasks.py was left at 3.",
                            "claim_subkind": "invariant_or_constraint",
                            "assertion_status": "observed",
                            "claim_frame": "reviewer_feedback",
                            "about_hints": [],
                            "confidence": "high",
                            "derivation_kind": "direct_from_observation",
                            "metadata": {},
                            "supporting_evidence": [
                                {
                                    "evidence_kind": "state_detail",
                                    "quote_text": "`MAX_RETRIES` in `tasks.py` was left at 3.",
                                    "summary_text": "The review says MAX_RETRIES stayed at 3.",
                                    "span_start": None,
                                    "span_end": None,
                                    "about_hints": [],
                                    "support_kind": "direct_statement",
                                    "support_source": "observation_text",
                                    "support_role": "states_constraint",
                                    "support_rationale": "This quote states that MAX_RETRIES remained at 3.",
                                    "metadata": {},
                                }
                            ],
                        }
                    ]
                },
                "provenance.relate_claims.resolve_new_claim_against_candidates.v1": resolve_pending_batch_claim,
            }
        )
        indexer = GitHubIndexer(
            github_api=object(),
            backend_driver=FakeBackendDriver([]),
            llm_client=llm_client,
        )

        plan, counts = indexer._plan_evidence_operations(
            planned_observations=planned_observations,
            snapshot=make_harness_like_snapshot(),
        )

        self.assertEqual(counts["invalidated"], 1)
        self.assertTrue(
            any(edge.relationship_type == "CONTRADICTS" for edge in plan.upsert_edges)
        )
        relation_calls = [
            call
            for call in llm_client.calls
            if call["prompt_name"] == "provenance.relate_claims.resolve_new_claim_against_candidates.v1"
        ]
        self.assertEqual(len(relation_calls), 1)

    def test_issue_closed_observations_do_not_trigger_claim_extraction(self):
        llm_client = PromptMapLLMClient({})
        indexer = GitHubIndexer(
            github_api=object(),
            backend_driver=FakeBackendDriver([]),
            llm_client=llm_client,
        )
        planned_observations = ObservationMutationPlanner().build_github_observations(
            bundle=GitHubProvenanceBundle(
                repo_id="demo",
                github_repo="octo/demo",
                entities=(),
                project_links=(),
                commit_links=(),
                entity_links=(),
                observation_sources=(
                    GitHubObservationSource(
                        observation_kind=ObservationKind.GITHUB_ISSUE_CLOSED,
                        source_kind="issue",
                        source_id="github:issue:demo:42",
                        source_record_type="github_issue",
                        external_id="4200:closed",
                        number=42,
                        url="https://github.com/octo/demo/issues/42",
                        title="Issue #42 closed: Rollout complete",
                        author_login="maintainer",
                        created_at="2026-03-27T20:59:00Z",
                        updated_at="2026-03-27T20:59:00Z",
                        text="Issue #42 was closed. State reason: completed.",
                        metadata={"issue_number": 42, "closed_at": "2026-03-27T20:59:00Z"},
                    ),
                ),
            )
        )

        plan, counts = indexer._plan_evidence_operations(
            planned_observations=planned_observations,
            snapshot=make_snapshot(),
        )

        self.assertTrue(plan.is_empty())
        self.assertEqual(counts["extracted"], 0)
        self.assertEqual(llm_client.calls, [])


if __name__ == "__main__":
    unittest.main()
