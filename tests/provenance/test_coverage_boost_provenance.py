"""Tests to boost coverage for provenance modules: invalidation, services, validation, etc."""

from __future__ import annotations

import unittest
from typing import Any, Mapping, Sequence
from unittest.mock import MagicMock, patch

from context_graph.provenance.invalidation import (
    CurrentEvidenceReader,
    EvidenceInvalidationPlanner,
    _coerce_bool,
    _close_unstarted_coroutine,
    _collapse_fact_group,
    _int_or_none,
    _parse_datetime,
    _parse_json_object,
    _parse_persisted_evidence_record,
    _string_or_none,
)
from context_graph.provenance.schema_registry import (
    DEFAULT_PROVENANCE_SCHEMA_REGISTRY,
    ProvenanceSchemaRegistry,
)
from context_graph.provenance.source_adapters import (
    AgentObservationAdapter,
    AgentObservationPayload,
    MarkdownObservationAdapter,
    _agent_source_id,
    _markdown_title,
)
from context_graph.provenance.prompt_context import (
    build_prompt_context,
    build_prompt_observation,
    build_prompt_source_context,
    build_deterministic_code_context,
    sanitize_prompt_code_context,
    _normalize_changed_files,
    _coerce_int,
)
from context_graph.provenance.claim_retrieval import (
    ClaimRetrievalFeatures,
    build_claim_retrieval_features,
    claim_profile_keys,
    grounded_code_terms,
    _claim_code_anchors,
    _claim_lexical_tokens,
)
from context_graph.provenance.services import (
    ClaimCanonicalizationService,
    ClaimDerivationService,
    ClaimEvidenceExtractionService,
    ClaimProfileKeyGenerationService,
    ClaimRelationService,
    ClaimResolutionService,
    EvidenceDedupService,
    FactConflictResolutionService,
    ResearchArtifactSynthesisService,
    _filter_generated_claim_profile_keys,
    _looks_like_code_anchor_phrase,
    _normalize_profile_key_text,
    _normalize_text_key,
)
from context_graph.provenance.models import (
    ClaimCandidate,
    ClaimDedupCandidate,
    EvidenceCandidate,
    EvidenceDedupCandidate,
    Observation,
    ObservationKind,
)


def _make_observation(**overrides) -> Observation:
    defaults = dict(
        observation_id="obs-1",
        repo_id="repo-1",
        observation_kind="git_commit_message",
        source_kind="commit",
        source_id="src-1",
        observed_at="2025-01-01T00:00:00Z",
        author=None,
        title="Test commit",
        text="Fixed a bug in the parser module",
        metadata={"changed_files": ["src/parser.py"]},
        context_kind="commit",
        context_id="abc123",
    )
    defaults.update(overrides)
    return Observation(**defaults)


class TestInvalidationHelpers(unittest.TestCase):
    def test_coerce_bool_none(self):
        self.assertTrue(_coerce_bool(None, default=True))
        self.assertFalse(_coerce_bool(None, default=False))

    def test_coerce_bool_string(self):
        self.assertTrue(_coerce_bool("true", default=False))
        self.assertTrue(_coerce_bool("1", default=False))
        self.assertTrue(_coerce_bool("yes", default=False))
        self.assertFalse(_coerce_bool("false", default=True))
        self.assertFalse(_coerce_bool("0", default=True))
        self.assertFalse(_coerce_bool("no", default=True))

    def test_coerce_bool_actual_bool(self):
        self.assertTrue(_coerce_bool(True, default=False))
        self.assertFalse(_coerce_bool(False, default=True))

    def test_string_or_none(self):
        self.assertIsNone(_string_or_none(None))
        self.assertIsNone(_string_or_none(""))
        self.assertIsNone(_string_or_none("   "))
        self.assertEqual(_string_or_none("hello"), "hello")

    def test_int_or_none(self):
        self.assertIsNone(_int_or_none(None))
        self.assertEqual(_int_or_none(42), 42)
        self.assertEqual(_int_or_none("42"), 42)
        self.assertIsNone(_int_or_none("abc"))

    def test_parse_datetime(self):
        self.assertIsNone(_parse_datetime(None))
        self.assertIsNone(_parse_datetime(""))
        self.assertIsNotNone(_parse_datetime("2025-01-01T00:00:00Z"))
        self.assertIsNone(_parse_datetime("not-a-date"))

    def test_parse_json_object(self):
        self.assertEqual(_parse_json_object(None), {})
        self.assertEqual(_parse_json_object(""), {})
        self.assertEqual(_parse_json_object("{}"), {})
        self.assertEqual(_parse_json_object('{"a": 1}'), {"a": 1})
        self.assertEqual(_parse_json_object("invalid"), {})
        self.assertEqual(_parse_json_object({"key": "val"}), {"key": "val"})
        self.assertEqual(_parse_json_object('"string"'), {})

    def test_close_unstarted_coroutine_non_coroutine(self):
        _close_unstarted_coroutine("not a coroutine")
        _close_unstarted_coroutine(None)

    def test_parse_persisted_evidence_record_missing_claim(self):
        row = {
            "evidence_id": "ev-1",
            "evidence_properties": {"repo_id": "repo-1"},
            "about_record_ids": [],
            "observation_record_ids": [],
            "observation_properties": [],
        }
        result = _parse_persisted_evidence_record(row)
        self.assertIsNone(result)

    def test_parse_persisted_evidence_record_valid(self):
        row = {
            "evidence_id": "ev-1",
            "evidence_properties": {
                "evidence_id": "ev-1",
                "repo_id": "repo-1",
                "evidence_kind": "quote",
                "quote_text": "some quote",
                "summary_text": "summary",
                "support_kind": "direct_statement",
                "claim_text": "The parser was fixed",
                "claim_kind": "change_claim",
                "is_current": True,
            },
            "about_record_ids": ["node-1"],
            "observation_record_ids": ["obs-1"],
            "observation_properties": [{"observation_id": "obs-1", "observed_at": "2025-01-01T00:00:00Z"}],
        }
        result = _parse_persisted_evidence_record(row)
        self.assertIsNotNone(result)
        self.assertEqual(result.evidence.claim_text, "The parser was fixed")

    def test_current_evidence_reader_no_backend(self):
        reader = CurrentEvidenceReader(backend_driver=None)
        facts = reader.load_current_facts(
            repo_id="repo-1",
            claim_kind="change_claim",
            about_record_ids=["node-1"],
        )
        self.assertEqual(facts, ())

    def test_current_evidence_reader_empty_about_ids(self):
        reader = CurrentEvidenceReader(backend_driver=MagicMock())
        facts = reader.load_current_facts(
            repo_id="repo-1",
            claim_kind="change_claim",
            about_record_ids=[],
        )
        self.assertEqual(facts, ())


class TestSchemaRegistry(unittest.TestCase):
    def test_observation_policy_with_source_kind(self):
        registry = DEFAULT_PROVENANCE_SCHEMA_REGISTRY
        policy = registry.observation_policy("git_commit_message", source_kind="commit")
        self.assertIsNotNone(policy)

    def test_observation_policy_without_source_kind(self):
        registry = DEFAULT_PROVENANCE_SCHEMA_REGISTRY
        policy = registry.observation_policy("git_commit_message")
        self.assertIsNotNone(policy)

    def test_claim_kind_allowed(self):
        registry = DEFAULT_PROVENANCE_SCHEMA_REGISTRY
        self.assertTrue(registry.claim_kind_allowed("change_claim", "commit"))
        self.assertFalse(registry.claim_kind_allowed("nonexistent_kind", "commit"))

    def test_claim_kind_for_subkind(self):
        registry = DEFAULT_PROVENANCE_SCHEMA_REGISTRY
        self.assertEqual(registry.claim_kind_for_subkind("bug_or_regression", "commit"), "bug_report")
        self.assertEqual(registry.claim_kind_for_subkind("fix_or_mitigation", "commit"), "change_claim")
        self.assertEqual(registry.claim_kind_for_subkind("invariant_or_constraint", "commit"), "state_claim")
        self.assertEqual(registry.claim_kind_for_subkind("desired_behavior_or_requirement", "commit"), "requirement")
        self.assertEqual(registry.claim_kind_for_subkind("reviewer_objection_or_requested_change", "pull_request"), "review_feedback")
        self.assertEqual(registry.claim_kind_for_subkind("reviewer_objection_or_requested_change", "issue"), "concern")
        self.assertEqual(registry.claim_kind_for_subkind("approval_or_acceptance", "commit"), "approval")
        self.assertEqual(registry.claim_kind_for_subkind("workaround_or_temporary_mitigation", "commit"), "workaround")
        self.assertEqual(registry.claim_kind_for_subkind("test_result_or_validation", "commit"), "test_expectation")
        self.assertEqual(registry.claim_kind_for_subkind("rollout_migration_or_dependency", "commit"), "scope_statement")
        self.assertEqual(registry.claim_kind_for_subkind("ownership_or_impact_scope", "commit"), "scope_statement")
        self.assertIsNone(registry.claim_kind_for_subkind("unknown_subkind", "commit"))

    def test_default_claim_subkind_for_claim_kind(self):
        registry = DEFAULT_PROVENANCE_SCHEMA_REGISTRY
        self.assertEqual(registry.default_claim_subkind_for_claim_kind("bug_report"), "bug_or_regression")
        self.assertEqual(registry.default_claim_subkind_for_claim_kind("change_claim"), "fix_or_mitigation")
        self.assertIsNone(registry.default_claim_subkind_for_claim_kind("unknown_kind"))

    def test_claim_kind_allowed_with_observation_kind(self):
        registry = DEFAULT_PROVENANCE_SCHEMA_REGISTRY
        result = registry.claim_kind_allowed("change_claim", "commit", observation_kind="commit_message")
        self.assertIsInstance(result, bool)


class TestSourceAdapters(unittest.TestCase):
    def test_markdown_title_extraction(self):
        self.assertEqual(_markdown_title("# Hello World"), "Hello World")
        self.assertEqual(_markdown_title("## Sub Title"), "Sub Title")
        self.assertIsNone(_markdown_title("No heading here"))

    def test_agent_source_id(self):
        result = _agent_source_id(
            repo_id="repo-1",
            source_kind="agent",
            context_id="task-1",
            observation_kind="agent_note",
        )
        self.assertIn("repo-1", result)
        self.assertIn("agent", result)
        self.assertIn("task-1", result)

    def test_agent_source_id_no_context(self):
        result = _agent_source_id(
            repo_id="repo-1",
            source_kind="agent",
            context_id=None,
            observation_kind="agent_note",
        )
        self.assertIn("agent_note", result)

    def test_agent_observation_adapter(self):
        adapter = AgentObservationAdapter()
        planner = MagicMock()
        planner.build_observation.return_value = MagicMock()
        payload = AgentObservationPayload(
            repo_id="repo-1",
            observation_text="Test observation",
            observation_kind="agent_note",
            title="Test",
            source_kind="agent",
        )
        result = adapter.build_observation(planner=planner, payload=payload)
        planner.build_observation.assert_called_once()

    def test_agent_observation_adapter_agent_observation_kind(self):
        adapter = AgentObservationAdapter()
        planner = MagicMock()
        planner.build_observation.return_value = MagicMock()
        payload = AgentObservationPayload(
            repo_id="repo-1",
            observation_text="Test observation",
            observation_kind="agent_observation",
            title="Test",
        )
        adapter.build_observation(planner=planner, payload=payload)
        call_kwargs = planner.build_observation.call_args
        self.assertIn("agent_note", str(call_kwargs))

    def test_markdown_observation_adapter_no_md_files(self):
        from context_graph.models.graph import GraphSnapshot
        adapter = MarkdownObservationAdapter()
        planner = MagicMock()
        snapshot = GraphSnapshot.from_dict({
            "repo_id": "repo-1",
            "revision_id": "rev-1",
            "root_path": "/tmp/repo",
            "generated_at": "2025-01-01T00:00:00Z",
            "nodes": [],
            "edges": [],
            "metadata": {},
        })
        result = adapter.build_observations(
            planner=planner,
            snapshot=snapshot,
            checkout_root="/tmp/nonexistent",
        )
        self.assertEqual(result, ())


class TestPromptContext(unittest.TestCase):
    def test_build_prompt_observation(self):
        obs = _make_observation()
        result = build_prompt_observation(obs)
        self.assertEqual(result["source_type"], "git_commit_message")
        self.assertIn("text", result)

    def test_build_prompt_observation_minimal(self):
        obs = _make_observation(title=None, observed_at=None, context_kind=None, context_id=None)
        result = build_prompt_observation(obs)
        self.assertNotIn("title", result)
        self.assertNotIn("observed_at", result)

    def test_build_prompt_source_context_commit(self):
        obs = _make_observation()
        result = build_prompt_source_context(obs)
        self.assertEqual(result["source_kind"], "commit")
        self.assertIn("changed_files", result)

    def test_build_prompt_source_context_pr(self):
        obs = _make_observation(
            source_kind="pull_request",
            observation_kind="github_pr_body",
            metadata={"pull_request_number": 42},
        )
        result = build_prompt_source_context(obs)
        self.assertEqual(result.get("pr_number"), 42)

    def test_build_prompt_source_context_issue(self):
        obs = _make_observation(
            source_kind="issue",
            observation_kind="github_issue_body",
            metadata={"issue_number": 7},
        )
        result = build_prompt_source_context(obs)
        self.assertEqual(result.get("issue_number"), 7)

    def test_build_prompt_source_context_document(self):
        obs = _make_observation(
            source_kind="document",
            observation_kind="markdown_document",
            metadata={"document_path": "README.md"},
        )
        result = build_prompt_source_context(obs)
        self.assertEqual(result.get("document_path"), "README.md")

    def test_sanitize_prompt_code_context(self):
        result = sanitize_prompt_code_context({
            "changed_files": ["a.py", "b.py"],
            "mentioned_entities": [{"name": "Foo", "kind": "Class"}],
            "touched_entities": [{"name": "bar", "kind": "Function", "file_path": "x.py"}],
        })
        self.assertIn("changed_files", result)
        self.assertIn("mentioned_entities", result)

    def test_normalize_changed_files(self):
        self.assertEqual(_normalize_changed_files(None), [])
        self.assertEqual(_normalize_changed_files(["a.py", "b.py"]), ["a.py", "b.py"])
        self.assertEqual(_normalize_changed_files("single.py"), ["single.py"])

    def test_coerce_int(self):
        self.assertIsNone(_coerce_int(None))
        self.assertEqual(_coerce_int(42), 42)
        self.assertEqual(_coerce_int("42"), 42)
        self.assertIsNone(_coerce_int("abc"))

    def test_build_deterministic_code_context_no_snapshot(self):
        obs = _make_observation()
        result = build_deterministic_code_context(obs, snapshot=None)
        self.assertIn("changed_files", result)

    def test_build_prompt_context_full(self):
        obs = _make_observation()
        result = build_prompt_context(obs)
        self.assertIn("observation", result)
        self.assertIn("code_context", result)
        self.assertIn("source_context", result)


class TestClaimRetrieval(unittest.TestCase):
    def test_build_claim_retrieval_features(self):
        features = build_claim_retrieval_features(
            claim_text="Fixed the parser bug in `src/parser.py`",
            support_texts=["The parser was crashing on empty input"],
        )
        self.assertIsInstance(features, ClaimRetrievalFeatures)
        self.assertTrue(len(features.lexical_tokens) > 0)
        self.assertTrue(len(features.code_anchors) > 0)

    def test_claim_profile_keys(self):
        keys = claim_profile_keys(
            claim_text="Fixed the parser bug",
            claim_kind="change_claim",
            claim_subkind="fix_or_mitigation",
        )
        self.assertTrue(len(keys) > 0)

    def test_grounded_code_terms(self):
        terms = grounded_code_terms(["code_node:repo-1:src.parser.__file__"])
        self.assertTrue(len(terms) > 0)

    def test_grounded_code_terms_empty(self):
        terms = grounded_code_terms([])
        self.assertEqual(terms, ())

    def test_claim_code_anchors(self):
        anchors = _claim_code_anchors("`MyClass.method` in src/module.py")
        self.assertTrue(len(anchors) > 0)

    def test_claim_lexical_tokens(self):
        tokens = _claim_lexical_tokens("Fixed the parser bug in the module")
        self.assertIn("fixed", tokens)
        self.assertIn("parser", tokens)
        self.assertIn("module", tokens)
        self.assertNotIn("the", tokens)


class TestServicesHelpers(unittest.TestCase):
    def test_normalize_text_key(self):
        self.assertEqual(_normalize_text_key("  Hello  World  "), "hello world")

    def test_normalize_profile_key_text(self):
        self.assertEqual(_normalize_profile_key_text(None), "")
        self.assertEqual(_normalize_profile_key_text("  hello  "), "hello")

    def test_looks_like_code_anchor_phrase(self):
        self.assertTrue(_looks_like_code_anchor_phrase("src/parser.py"))
        self.assertTrue(_looks_like_code_anchor_phrase("MyClass.method"))
        self.assertTrue(_looks_like_code_anchor_phrase("my_function_name"))
        self.assertTrue(_looks_like_code_anchor_phrase("MAX_RETRIES"))
        self.assertFalse(_looks_like_code_anchor_phrase("simple text"))
        self.assertFalse(_looks_like_code_anchor_phrase(""))

    def test_filter_generated_claim_profile_keys(self):
        keys = _filter_generated_claim_profile_keys(
            ["key one", "key two", "key one", ""],
            claim_text="key one",
            limit=3,
        )
        self.assertEqual(keys, ("key two",))

    def test_filter_generated_claim_profile_keys_code_anchors(self):
        keys = _filter_generated_claim_profile_keys(
            ["src/parser.py", "good key"],
            claim_text="something else",
            limit=3,
        )
        self.assertEqual(keys, ("good key",))

    def test_evidence_dedup_service(self):
        service = EvidenceDedupService()
        candidate = EvidenceDedupCandidate(
            observation_id="obs-1",
            evidence_kind="quote",
            support_kind="direct_statement",
            quote_text="some quote",
            summary_text="summary",
            span_start=0,
            span_end=10,
            about_record_ids=["node-1"],
            claim_kind="change_claim",
            claim_text="Fixed the bug",
        )
        result = service.pregroup([candidate, candidate])
        self.assertEqual(len(result.groups), 1)
        self.assertEqual(len(result.groups[0].candidates), 2)

    def test_claim_canonicalization_service(self):
        service = ClaimCanonicalizationService()
        candidate = ClaimDedupCandidate(
            observation_id="obs-1",
            claim_kind="change_claim",
            claim_text="Fixed the bug",
            about_record_ids=["node-1"],
        )
        result = service.pregroup([candidate])
        self.assertEqual(len(result.groups), 1)

    def test_claim_derivation_service_empty_evidence(self):
        service = ClaimDerivationService()
        with self.assertRaises(ValueError):
            service.build_request([])

    def test_claim_relation_service_too_few_claims(self):
        service = ClaimRelationService()
        with self.assertRaises(ValueError):
            service.build_request([ClaimCandidate(claim_text="one", claim_kind="change_claim")])

    def test_claim_resolution_service_empty_claim_text(self):
        service = ClaimResolutionService()
        with self.assertRaises(ValueError):
            service.build_request(new_claim={"claim_text": ""}, candidates=[{"a": 1}])

    def test_claim_resolution_service_empty_candidates(self):
        service = ClaimResolutionService()
        with self.assertRaises(ValueError):
            service.build_request(new_claim={"claim_text": "test"}, candidates=[])

    def test_claim_resolution_service_no_llm(self):
        service = ClaimResolutionService(llm_client=None)
        with self.assertRaises(RuntimeError):
            service.resolve(repo_id="repo-1", new_claim={"claim_text": "test"}, candidates=[{"a": 1}])

    def test_fact_conflict_resolution_service_empty_claim(self):
        service = FactConflictResolutionService()
        with self.assertRaises(ValueError):
            service.build_request(new_fact={"claim_text": ""}, current_facts=[{"a": 1}])

    def test_fact_conflict_resolution_service_empty_facts(self):
        service = FactConflictResolutionService()
        with self.assertRaises(ValueError):
            service.build_request(new_fact={"claim_text": "test"}, current_facts=[])

    def test_fact_conflict_resolution_service_no_llm(self):
        service = FactConflictResolutionService(llm_client=None)
        with self.assertRaises(RuntimeError):
            service.resolve(repo_id="repo-1", new_fact={"claim_text": "test"}, current_facts=[{"a": 1}])

    def test_claim_evidence_extraction_service_empty_text(self):
        service = ClaimEvidenceExtractionService()
        obs = _make_observation(text="")
        with self.assertRaises(ValueError):
            service.build_request(obs)

    def test_claim_evidence_extraction_service_no_llm(self):
        service = ClaimEvidenceExtractionService(llm_client=None)
        obs = _make_observation()
        with self.assertRaises(RuntimeError):
            service.extract(obs)

    def test_claim_profile_key_generation_service_no_llm(self):
        service = ClaimProfileKeyGenerationService(llm_client=None)
        result = service.generate(
            repo_id="repo-1",
            claim_text="test claim",
            claim_subkind="fix_or_mitigation",
        )
        self.assertEqual(result, ())

    def test_claim_profile_key_generation_service_empty_text(self):
        service = ClaimProfileKeyGenerationService(llm_client=MagicMock())
        result = service.generate(
            repo_id="repo-1",
            claim_text="",
            claim_subkind="fix_or_mitigation",
        )
        self.assertEqual(result, ())

    def test_research_artifact_synthesis_service_empty(self):
        service = ResearchArtifactSynthesisService()
        with self.assertRaises(ValueError):
            service.build_request()

    def test_research_artifact_synthesis_service_no_llm(self):
        service = ResearchArtifactSynthesisService(llm_client=None)
        with self.assertRaises(RuntimeError):
            service.synthesize(
                repo_id="repo-1",
                code_context={"some": "context"},
            )


if __name__ == "__main__":
    unittest.main()
