from __future__ import annotations

import unittest

from context_graph.ingestion.git.github_indexer import GitHubObservationSource, GitHubProvenanceBundle
from context_graph.ingestion.git.git_metadata import CommitInfo
from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.provenance import (
    ClaimCandidate,
    ClaimDerivationResponse,
    ClaimDerivationService,
    ClaimRelationResponse,
    ClaimRelationService,
    EvidenceCandidate,
    EvidenceExtractionResponse,
    EvidenceExtractionService,
    EvidenceMutationPlanner,
    Observation,
    ObservationKind,
    ObservationMutationPlanner,
    ResearchArtifactSynthesisResponse,
    ResearchArtifactSynthesisService,
)
from context_graph.provenance.prompts import (
    extract_evidence,
    normalize_claims,
    relate_claims,
    synthesize_research_artifact,
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
        nodes=(project, file_node, function_node),
        edges=(edge,),
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


class FakeLLMClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def generate_response(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return self.response


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


class EvidencePlannerTests(unittest.TestCase):
    def test_evidence_planner_emits_grounded_evidence_records(self):
        observation = Observation(
            observation_id="observation:github_pr_body:demo:7",
            repo_id="demo",
            observation_kind=ObservationKind.GITHUB_PR_BODY,
            source_kind="pull_request",
            source_id="github:pr:demo:7",
            observed_at="2026-03-26T00:01:00+00:00",
            author="octocat",
            title="Tighten rollback handling",
            text="This PR updates demo.app.main.",
            metadata={"pull_request_number": 7},
        )
        extraction = EvidenceExtractionResponse.model_validate(
            {
                "evidence": [
                    {
                        "evidence_kind": "change_summary",
                        "quote_text": "updates demo.app.main",
                        "summary_text": "The PR updates demo.app.main.",
                        "span_start": 8,
                        "span_end": 29,
                        "about_hints": ["demo.app.main"],
                        "support_kind": "direct_statement",
                        "metadata": {},
                    }
                ]
            }
        )

        plan = EvidenceMutationPlanner().plan_evidence_records(
            observation=observation,
            extraction=extraction,
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

    def test_evidence_planner_leaves_unresolved_hints_unbound(self):
        observation = Observation(
            observation_id="observation:github_issue_comment:demo:9",
            repo_id="demo",
            observation_kind=ObservationKind.GITHUB_ISSUE_COMMENT,
            source_kind="issue",
            source_id="github:issue:demo:9",
            observed_at="2026-03-26T00:04:00+00:00",
            author="octocat",
            title="Comment on issue #9",
            text="This comment mentions an unknown symbol.",
            metadata={"issue_number": 9},
        )
        extraction = EvidenceExtractionResponse.model_validate(
            {
                "evidence": [
                    {
                        "evidence_kind": "bug_report",
                        "quote_text": "unknown symbol",
                        "summary_text": "The comment references an unknown symbol.",
                        "span_start": 24,
                        "span_end": 38,
                        "about_hints": ["demo.app.unknown"],
                        "support_kind": "direct_statement",
                        "metadata": {},
                    }
                ]
            }
        )

        plan = EvidenceMutationPlanner().plan_evidence_records(
            observation=observation,
            extraction=extraction,
            snapshot=make_snapshot(),
        )

        self.assertEqual(
            {edge.relationship_type for edge in plan.upsert_edges},
            {"DERIVED_FROM"},
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
        evidence_messages = extract_evidence.from_commit_message(
            {"observation": observation_payload, "code_context": {}, "source_context": {}}
        )
        review_messages = extract_evidence.from_github_pr_review(
            {"observation": observation_payload, "code_context": {}, "source_context": {}}
        )
        review_comment_messages = extract_evidence.from_github_pr_review_comment(
            {"observation": observation_payload, "code_context": {}, "source_context": {}}
        )
        issue_comment_messages = extract_evidence.from_github_issue_comment(
            {"observation": observation_payload, "code_context": {}, "source_context": {}}
        )
        claim_messages = normalize_claims.from_evidence_set(
            {"topic": {"scope": "demo"}, "evidence": [], "target_context": {}}
        )
        relation_messages = relate_claims.compare_claims({"claims": [{"claim_text": "A"}]})
        artifact_messages = synthesize_research_artifact.from_code_and_evidence(
            {"repo_region": {"path": "app.py"}, "code_context": {}, "evidence": [], "claims": []}
        )

        self.assertEqual(len(evidence_messages), 2)
        self.assertEqual(len(review_messages), 2)
        self.assertEqual(len(review_comment_messages), 2)
        self.assertEqual(len(issue_comment_messages), 2)
        self.assertEqual(len(claim_messages), 2)
        self.assertEqual(len(relation_messages), 2)
        self.assertEqual(len(artifact_messages), 2)
        prompt_names = {
            extract_evidence.EXTRACT_EVIDENCE_FROM_COMMIT_MESSAGE.prompt_name,
            extract_evidence.EXTRACT_EVIDENCE_FROM_GITHUB_PR_BODY.prompt_name,
            extract_evidence.EXTRACT_EVIDENCE_FROM_GITHUB_ISSUE_BODY.prompt_name,
            extract_evidence.EXTRACT_EVIDENCE_FROM_GITHUB_PR_REVIEW.prompt_name,
            extract_evidence.EXTRACT_EVIDENCE_FROM_GITHUB_PR_REVIEW_COMMENT.prompt_name,
            extract_evidence.EXTRACT_EVIDENCE_FROM_GITHUB_ISSUE_COMMENT.prompt_name,
            extract_evidence.EXTRACT_EVIDENCE_FROM_GENERIC_OBSERVATION.prompt_name,
            normalize_claims.NORMALIZE_CLAIMS_FROM_EVIDENCE_SET.prompt_name,
            relate_claims.RELATE_CLAIMS_COMPARE.prompt_name,
            synthesize_research_artifact.SYNTHESIZE_RESEARCH_ARTIFACT_FROM_CODE_AND_EVIDENCE.prompt_name,
        }
        self.assertEqual(len(prompt_names), 10)

    def test_response_models_validate_structured_outputs(self):
        evidence = EvidenceExtractionResponse.model_validate(
            {
                "evidence": [
                    {
                        "evidence_kind": "change_summary",
                        "quote_text": "Add helper support",
                        "summary_text": "The commit adds helper support.",
                        "span_start": 0,
                        "span_end": 18,
                        "about_hints": ["demo.app.main"],
                        "support_kind": "direct_statement",
                        "metadata": {},
                    }
                ]
            }
        )
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

        self.assertEqual(len(evidence.evidence), 1)
        self.assertEqual(len(claims.claims), 1)
        self.assertEqual(len(relations.relations), 1)
        self.assertEqual(len(artifacts.artifacts), 1)

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
        evidence_request = EvidenceExtractionService().build_request(observation)
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
            evidence_request.prompt_name,
            "provenance.extract_evidence.from_commit_message.v1",
        )
        self.assertIs(evidence_request.response_model, EvidenceExtractionResponse)
        self.assertIs(claim_request.response_model, ClaimDerivationResponse)
        self.assertIs(relation_request.response_model, ClaimRelationResponse)
        self.assertIs(artifact_request.response_model, ResearchArtifactSynthesisResponse)

    def test_evidence_extraction_service_executes_with_llm_client(self):
        llm_client = FakeLLMClient(
            {
                "evidence": [
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
                ]
            }
        )
        service = EvidenceExtractionService(llm_client=llm_client)
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

        self.assertEqual(len(response.evidence), 1)
        self.assertEqual(
            llm_client.calls[0]["prompt_name"],
            "provenance.extract_evidence.from_github_pr_review.v1",
        )
        self.assertIs(llm_client.calls[0]["response_model"], EvidenceExtractionResponse)


if __name__ == "__main__":
    unittest.main()
