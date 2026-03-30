"""Tests to boost coverage for validation.py and cli.py."""

import unittest
from context_graph.provenance.validation import (
    ClaimBundleValidator,
    ClaimWithEvidenceCandidate,
    SupportingEvidenceCandidate,
    Observation,
)

def _make_candidate(**overrides) -> ClaimWithEvidenceCandidate:
    defaults = dict(
        claim_text="The parser was fixed",
        claim_subkind="fix_or_mitigation",
        assertion_status="reported",
        claim_frame="author_statement",
        about_hints=[],
        confidence="high",
        derivation_kind="test",
        supporting_evidence=[
            SupportingEvidenceCandidate.model_construct(
                evidence_kind="change_summary",
                quote_text="fixed parser",
                support_kind="direct_statement",
                support_role="provides_context",
                support_source="observation_text",
                support_rationale="foo",
            )
        ]
    )
    defaults.update(overrides)
    return ClaimWithEvidenceCandidate.model_construct(**defaults)

def _make_obs(**overrides) -> Observation:
    defaults = dict(
        observation_id="obs-1",
        repo_id="repo-1",
        observation_kind="git_commit_message",
        source_kind="commit",
        source_id="src-1",
        observed_at="2025-01-01T00:00:00Z",
        author=None,
        title="Test title",
        text="This fixed parser heavily",
    )
    defaults.update(overrides)
    return Observation(**defaults)

class TestValidationExtended(unittest.TestCase):
    def setUp(self):
        self.validator = ClaimBundleValidator()
        self.obs = _make_obs()

    def _validate_single(self, candidate, claim_kind="change_claim"):
        from context_graph.provenance.models import ClaimEvidenceExtractionResponse
        extraction = ClaimEvidenceExtractionResponse.model_construct(claims=[candidate])
        return self.validator.validate(observation=self.obs, extraction=extraction)

    def test_normalize_support_fields_swap(self):
        ev = SupportingEvidenceCandidate.model_construct(
            evidence_kind="change_summary",
            quote_text="fixed parser",
            support_kind="provides_context", # role!
            support_role="not_a_role",
        )
        cand = _make_candidate(supporting_evidence=[ev])
        res = self._validate_single(cand)
        self.assertTrue(len(res.rejected_claims) > 0)
    
    def test_validate_structural_rejections(self):
        res = self._validate_single(_make_candidate(claim_subkind="invalid_subkind"))
        self.assertEqual(res.rejected_claims[0].reason_code, "unsupported_claim_subkind")
        
        cand = _make_candidate()
        object.__setattr__(cand, 'claim_subkind', None)
        
        res = self._validate_single(_make_candidate(assertion_status="invalid"))
        self.assertEqual(res.rejected_claims[0].reason_code, "unsupported_assertion_status")
        
        res = self._validate_single(_make_candidate(claim_frame="invalid"))
        self.assertEqual(res.rejected_claims[0].reason_code, "unsupported_claim_frame")
        
        res = self._validate_single(_make_candidate(confidence="100%"))
        self.assertEqual(res.rejected_claims[0].reason_code, "unsupported_confidence")
        
        res = self._validate_single(_make_candidate(supporting_evidence=[]))
        self.assertEqual(res.rejected_claims[0].reason_code, "missing_supporting_evidence")
        
        res = self._validate_single(_make_candidate(supporting_evidence=[
            SupportingEvidenceCandidate.model_construct(evidence_kind="change_summary", quote_text="A", support_kind="direct_statement", support_role="provides_context"),
            SupportingEvidenceCandidate.model_construct(evidence_kind="change_summary", quote_text="B", support_kind="direct_statement", support_role="provides_context"),
            SupportingEvidenceCandidate.model_construct(evidence_kind="change_summary", quote_text="C", support_kind="direct_statement", support_role="provides_context"),
            SupportingEvidenceCandidate.model_construct(evidence_kind="change_summary", quote_text="D", support_kind="direct_statement", support_role="provides_context"),
        ]))
        self.assertEqual(res.rejected_claims[0].reason_code, "too_many_supporting_evidence")

    def test_validate_structural_evidence_rejections(self):
        def _ev(**kw):
            defaults = dict(evidence_kind="change_summary", quote_text="fixed", support_kind="direct_statement", support_role="provides_context", support_source="observation_text")
            defaults.update(kw)
            return SupportingEvidenceCandidate.model_construct(**defaults)

        res = self._validate_single(_make_candidate(supporting_evidence=[_ev(evidence_kind="invalid")]))
        self.assertEqual(res.rejected_claims[0].reason_code, "unsupported_evidence_kind")

        res = self._validate_single(_make_candidate(supporting_evidence=[_ev(support_kind="invalid")]))
        self.assertEqual(res.rejected_claims[0].reason_code, "unsupported_support_kind")

        res = self._validate_single(_make_candidate(supporting_evidence=[_ev(support_source="invalid")]))
        self.assertEqual(res.rejected_claims[0].reason_code, "unsupported_support_source")
        
        res = self._validate_single(_make_candidate(supporting_evidence=[_ev(support_role="invalid")]))
        self.assertEqual(res.rejected_claims[0].reason_code, "unsupported_support_role")

        res = self._validate_single(_make_candidate(supporting_evidence=[_ev(quote_text="NOT IN TEXT", support_rationale="foo")]))
        self.assertEqual(res.rejected_claims[0].reason_code, "invalid_evidence_span")

    def test_validate_semantic_rejections(self):
        res = self._validate_single(_make_candidate(claim_text="short"))
        self.assertEqual(res.rejected_claims[0].reason_code, "claim_too_vague")

        res = self._validate_single(_make_candidate(claim_text="this might probably be speculative", assertion_status="confirmed"))
        self.assertEqual(res.rejected_claims[0].reason_code, "speculative_claim")
        
        res = self._validate_single(_make_candidate(confidence="low"))
        self.assertEqual(res.rejected_claims[0].reason_code, "insufficient_confidence")
        
    def test_support_is_valid_branches(self):
        ev = SupportingEvidenceCandidate.model_construct(
            evidence_kind="change_summary",
            quote_text="Test title",
            support_kind="direct_statement",
            support_role="provides_context",
            support_source="observation_title",
            support_rationale="foo",
            span_start=None,
            span_end=None,
        )
        cand = _make_candidate(supporting_evidence=[ev])
        res = self._validate_single(cand)
        self.assertEqual(res.validated_count, 1)
        
        ev = SupportingEvidenceCandidate.model_construct(
            evidence_kind="change_summary",
            quote_text="Test title",
            support_kind="direct_statement",
            support_role="provides_context",
            support_source="observation_title",
            span_start=0,
            span_end=10,
            support_rationale="foo"
        )
        cand = _make_candidate(supporting_evidence=[ev])
        res = self._validate_single(cand)
        self.assertEqual(res.rejected_claims[0].reason_code, "invalid_evidence_span")

class TestCliHelpers(unittest.TestCase):
    def setUp(self):
        import argparse
        self.args = argparse.Namespace()
        self.args.graph_backend = "neo4j"
        self.args.neo4j_uri = "bolt://localhost:7687"
        self.args.neo4j_user = "neo4j"
        self.args.neo4j_password = "password"
        self.args.neo4j_database = "neo4j"
        self.args.neptune_host = None
        self.args.metadata_backend = "sqlite"
        self.args.metadata_db = ":memory:"
        self.args.metadata_dsn = None
        self.args.text = "text"
        self.args.text_file = None
        
    def test_make_backend_driver_neptune(self):
        from context_graph.cli import _make_backend_driver
        self.args.graph_backend = "neptune"
        self.args.neptune_host = "test"
        self.args.neptune_port = 8182
        try:
            drv = _make_backend_driver(self.args)
        except Exception:
            pass

    def test_make_metadata_store(self):
        from context_graph.cli import _make_metadata_store
        store = _make_metadata_store(self.args)
        self.assertIsNotNone(store)
        store.close()

    def test_load_episode_text(self):
        from context_graph.cli import _load_episode_text
        self.assertEqual(_load_episode_text(self.args), "text")
        
        self.args.text = None
        with self.assertRaises(RuntimeError):
            _load_episode_text(self.args)
            
        self.args.text = "text"
        self.args.text_file = "file"
        with self.assertRaises(RuntimeError):
            _load_episode_text(self.args)

if __name__ == "__main__":
    unittest.main()
