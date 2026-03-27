from __future__ import annotations

import json
import unittest

from context_graph.code_identity import CodeIdentityCanonicalizer, SnapshotCodeResolver
from context_graph.ingestion.commit_indexing import CommitIndexingService
from context_graph.ingestion.git.github_client import GitHubRepoRef
from context_graph.ingestion.git.github_indexer import GitHubIndexer, GitHubObservationSource, GitHubProvenanceBundle
from context_graph.ingestion.git.git_metadata import CommitInfo
from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.provenance import (
    ClaimCanonicalizationResult,
    ClaimCanonicalizationService,
    ClaimBundleValidator,
    ClaimCandidate,
    ClaimDedupCandidate,
    ClaimDerivationResponse,
    ClaimDerivationService,
    ClaimEvidenceExtractionResponse,
    ClaimEvidenceExtractionService,
    CurrentEvidenceReader,
    ClaimRelationResponse,
    ClaimRelationService,
    EvidenceCandidate,
    EvidenceDedupCandidate,
    EvidenceDedupResult,
    EvidenceDedupService,
    EvidenceInvalidationPlanner,
    EvidenceMutationPlanner,
    FactConflictResolutionService,
    Observation,
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
from context_graph.provenance.grounding import resolve_about_record_ids
from context_graph.provenance.canonicalization import canonical_claim_key


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


def make_commit(*, subject: str = "Add helper support", body: str = "body text") -> CommitInfo:
    return CommitInfo(
        sha="abc123",
        parent_shas=("parent1",),
        branch="main",
        author_name="Dev",
        author_email="dev@example.com",
        authored_at="2026-03-26T00:00:00+00:00",
        committed_at="2026-03-26T00:00:05+00:00",
        subject=subject,
        body=body,
        changed_files=("app.py",),
    )


def make_observation(
    *,
    kind: ObservationKind = ObservationKind.GITHUB_PR_BODY,
    source_kind: str = "pull_request",
    text: str = "This PR updates demo.app.main.",
) -> Observation:
    return Observation(
        observation_id="observation:github_pr_body:demo:7",
        repo_id="demo",
        observation_kind=kind,
        source_kind=source_kind,
        source_id="github:pr:demo:7" if source_kind == "pull_request" else "github:issue:demo:9",
        observed_at="2026-03-26T00:01:00+00:00",
        author="octocat",
        title="Tighten rollback handling",
        text=text,
        metadata={"pull_request_number": 7},
    )


def make_extraction_response(
    *,
    claim_text: str = "demo.app.main was updated.",
    claim_kind: str = "change_claim",
    confidence: str = "high",
    source_hint: str | None = "demo.app.main",
    quote_text: str = "updates demo.app.main",
    span_start: int | None = 8,
    span_end: int | None = 29,
    evidence_kind: str = "change_summary",
    support_kind: str = "direct_statement",
    summary_text: str | None = None,
) -> ClaimEvidenceExtractionResponse:
    return ClaimEvidenceExtractionResponse.model_validate(
        {
            "claims": [
                {
                    "claim_text": claim_text,
                    "claim_kind": claim_kind,
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
        "fact_key": resolved_fact_key if top_level_claims else None,
        "claim_text": claim_text if top_level_claims else None,
        "claim_kind": claim_kind if top_level_claims else None,
        "claim_confidence": "high" if top_level_claims else None,
        "claim_derivation_kind": "direct_from_observation" if top_level_claims else None,
        "observation_id": "observation:github_pr_body:demo:5" if top_level_claims else None,
        "observed_at": observed_at if top_level_claims else None,
        "is_current": is_current,
        "metadata_json": json.dumps(
            {
                "claim_text": claim_text,
                "claim_kind": claim_kind,
                "claim_confidence": "high",
                "claim_derivation_kind": "direct_from_observation",
            }
        ),
    }
    return {
        "evidence_id": evidence_id,
        "evidence_properties": evidence_properties,
        "about_record_ids": list(about_record_ids),
        "observation_record_ids": ["observation:github_pr_body:demo:5"],
        "observation_properties": [{"observed_at": observed_at}] if observed_at else [{}],
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
        return responses.pop(0)


class FakeBackendDriver:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    async def execute_query(self, query, **kwargs):
        self.calls.append({"query": query, "kwargs": kwargs})
        return list(self.rows)


class FakeMetadataStore:
    def bootstrap(self):
        return None


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
                        "claim_kind": "change_claim",
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

    def test_validator_rejects_invalid_spans(self):
        result = self.validator.validate(
            observation=self.observation,
            extraction=make_extraction_response(span_start=100, span_end=120),
            snapshot=self.snapshot,
        )

        self.assertEqual(result.rejected_claims[0].reason_code, "invalid_evidence_span")

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
            extraction=make_extraction_response(claim_text="Maybe demo.app.main was updated."),
            snapshot=self.snapshot,
        )
        low_confidence = self.validator.validate(
            observation=self.observation,
            extraction=make_extraction_response(confidence="low"),
            snapshot=self.snapshot,
        )

        self.assertEqual(speculative.rejected_claims[0].reason_code, "speculative_claim")
        self.assertEqual(low_confidence.rejected_claims[0].reason_code, "insufficient_confidence")


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

        plan = EvidenceMutationPlanner().plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {
                edge.target_record_id
                for edge in plan.upsert_edges
                if edge.relationship_type == "ABOUT"
            },
            {"file:demo:demo.app.__file__@r1"},
        )

    def test_evidence_planner_grounds_exact_file_paths_from_text(self):
        observation = make_observation(text="This PR updates the auth entrypoint.")
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

        plan = EvidenceMutationPlanner().plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {
                edge.target_record_id
                for edge in plan.upsert_edges
                if edge.relationship_type == "ABOUT"
            },
            {"file:demo:demo.auth.main.__file__@r1"},
        )

    def test_evidence_planner_grounds_exact_qualified_names_from_text(self):
        observation = make_observation(text="This PR updates the main function.")
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

        plan = EvidenceMutationPlanner().plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {
                edge.target_record_id
                for edge in plan.upsert_edges
                if edge.relationship_type == "ABOUT"
            },
            {"symbol:demo:demo.app.main@r1"},
        )

    def test_evidence_planner_grounds_dotted_partial_suffixes_from_text(self):
        observation = make_observation(text="This PR updates the auth login method.")
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

        plan = EvidenceMutationPlanner().plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {
                edge.target_record_id
                for edge in plan.upsert_edges
                if edge.relationship_type == "ABOUT"
            },
            {"symbol:demo:demo.auth.AuthService.login@r1"},
        )

    def test_evidence_planner_grounds_unique_bare_symbol_names_from_text(self):
        observation = make_observation(text="This PR updates the auth service.")
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

        plan = EvidenceMutationPlanner().plan_validated_claims(
            observation=observation,
            validation=validation,
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {
                edge.target_record_id
                for edge in plan.upsert_edges
                if edge.relationship_type == "ABOUT"
            },
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
            {"DERIVED_FROM", "ABOUT"},
        )
        self.assertEqual(
            {
                edge.target_record_id
                for edge in plan.upsert_edges
                if edge.relationship_type == "ABOUT"
            },
            {"symbol:demo:demo.app.main@r1"},
        )
        self.assertEqual(plan.upsert_search_documents[0].properties["claim_kind"], "change_claim")

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
        existing_node = nodes["evidence:demo:existing"]
        self.assertFalse(existing_node.properties["is_current"])
        self.assertEqual(
            existing_node.properties["invalidated_by_fact_key"],
            canonical_claim_key(claim_kind="state_claim", claim_text="demo.app.main is enabled."),
        )
        new_node = next(
            node
            for node in result.plan.upsert_nodes
            if node.record_id != "evidence:demo:existing"
        )
        self.assertTrue(new_node.properties["is_current"])
        supersedes_edges = [
            edge
            for edge in result.plan.upsert_edges
            if edge.relationship_type == "SUPERSEDES"
        ]
        self.assertEqual(len(supersedes_edges), 1)
        self.assertEqual(supersedes_edges[0].target_record_id, "evidence:demo:existing")
        self.assertEqual(supersedes_edges[0].source_record_id, new_node.record_id)

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
        new_node = next(node for node in result.plan.upsert_nodes if node.record_id != "evidence:demo:existing")
        self.assertFalse(new_node.properties["is_current"])
        self.assertEqual(new_node.properties["invalidated_by_fact_key"], existing_fact_key)
        supersedes_edges = [
            edge
            for edge in result.plan.upsert_edges
            if edge.relationship_type == "SUPERSEDES"
        ]
        self.assertEqual(len(supersedes_edges), 1)
        self.assertEqual(supersedes_edges[0].source_record_id, "evidence:demo:existing")
        self.assertEqual(supersedes_edges[0].target_record_id, new_node.record_id)

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
            {"DERIVED_FROM", "ABOUT"},
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
            {"DERIVED_FROM", "ABOUT"},
        )


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
                        "claim_kind": "review_feedback",
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


class IngestionInvalidationTests(unittest.TestCase):
    def test_commit_evidence_planning_reports_invalidated_count(self):
        llm_client = PromptMapLLMClient(
            {
                "provenance.extract_claims_and_evidence.from_commit_message.v1": {
                    "claims": [
                        {
                            "claim_text": "demo.app.main is enabled.",
                            "claim_kind": "state_claim",
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
            any(edge.relationship_type == "SUPERSEDES" for edge in plan.upsert_edges)
        )

    def test_github_evidence_planning_reports_invalidated_count(self):
        llm_client = PromptMapLLMClient(
            {
                "provenance.extract_claims_and_evidence.from_github_pr_body.v1": {
                    "claims": [
                        {
                            "claim_text": "demo.app.main is enabled.",
                            "claim_kind": "state_claim",
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
            any(edge.relationship_type == "SUPERSEDES" for edge in plan.upsert_edges)
        )


if __name__ == "__main__":
    unittest.main()
