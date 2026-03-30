from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from context_graph.models.graph import CanonicalNode, GraphSnapshot
from context_graph.provenance.claim_retrieval import (
    ClaimSemanticRetrievalService,
    _claim_code_anchors,
    _dedupe_preserve_order,
    _string_or_none,
    grounded_code_terms,
    semantic_claim_profile_key_ids,
    semantic_claim_profile_key_records,
)
from context_graph.provenance.models import (
    ClaimWithEvidenceCandidate,
    Observation,
    ObservationKind,
    SupportingEvidenceCandidate,
)
from context_graph.provenance.prompt_context import (
    _normalize_changed_files,
    _normalize_entities,
    _observation_changed_files,
)
from context_graph.provenance.source_adapters import (
    AgentObservationAdapter,
    AgentObservationPayload,
    MarkdownObservationAdapter,
)
from context_graph.provenance.validation import ClaimBundleValidator


def _make_observation(**overrides) -> Observation:
    defaults = dict(
        observation_id="observation:git_commit_message:repo-1:abc123",
        repo_id="repo-1",
        observation_kind=ObservationKind.GIT_COMMIT_MESSAGE.value,
        source_kind="commit",
        source_id="commit:abc123",
        observed_at="2026-03-30T12:00:00Z",
        author="octocat",
        title="Improve rollback handling",
        text="Rollback handling changed permanently in src/app.py.",
        metadata={},
    )
    defaults.update(overrides)
    return Observation(**defaults)


def _make_markdown_snapshot() -> GraphSnapshot:
    return GraphSnapshot(
        repo_id="repo-1",
        revision_id="rev-1",
        root_path="/tmp/repo",
        generated_at="2026-03-30T12:00:00Z",
        nodes=(
            CanonicalNode(
                node_id="file:repo-1:docs/good.md",
                repo_id="repo-1",
                kind="File",
                name="good.md",
                file_path="docs/good.md",
            ),
            CanonicalNode(
                node_id="file:repo-1:docs/bad.md",
                repo_id="repo-1",
                kind="File",
                name="bad.md",
                file_path="docs/bad.md",
            ),
            CanonicalNode(
                node_id="file:repo-1:docs/empty.md",
                repo_id="repo-1",
                kind="File",
                name="empty.md",
                file_path="docs/empty.md",
            ),
            CanonicalNode(
                node_id="file:repo-1:docs/missing.md",
                repo_id="repo-1",
                kind="File",
                name="missing.md",
                file_path="docs/missing.md",
            ),
        ),
        edges=(),
        metadata={},
    )


def test_source_adapters_cover_missing_unicode_empty_and_agent_validation(tmp_path: Path) -> None:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "good.md").write_text("# Good\nUseful text", encoding="utf-8")
    (docs_dir / "empty.md").write_text("   \n", encoding="utf-8")
    (docs_dir / "bad.md").write_bytes(b"\xff# Bad\nStill readable")

    adapter = MarkdownObservationAdapter()
    snapshot = _make_markdown_snapshot()
    planner = MagicMock()
    planner.build_observation.side_effect = [SimpleNamespace(name="good"), SimpleNamespace(name="bad")]
    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args, **kwargs):
        if self.name == "bad.md" and "errors" not in kwargs:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad data")
        if self.name == "bad.md":
            return "# Bad\nRecovered"
        return original_read_text(self, *args, **kwargs)

    with patch.object(Path, "read_text", fake_read_text):
        planned = adapter.build_observations(
            planner=planner,
            snapshot=snapshot,
            checkout_root=str(tmp_path),
        )

    assert len(planned) == 2
    assert planner.build_observation.call_count == 2

    agent_adapter = AgentObservationAdapter()
    planner = MagicMock()
    planner.build_observation.return_value = None
    with pytest.raises(ValueError, match="require non-empty observation_text"):
        agent_adapter.build_observation(
            planner=planner,
            payload=AgentObservationPayload(
                repo_id="repo-1",
                observation_text="text",
                observation_kind="agent_note",
            ),
        )


def test_prompt_context_normalizers_cover_skip_duplicate_and_limit_branches() -> None:
    entities = _normalize_entities(
        [
            "not-a-mapping",
            {"name": "   ", "kind": "Class"},
            {"name": "Foo", "kind": "Class", "file_path": "src/foo.py"},
            {"name": "Foo", "kind": "Class", "file_path": "src/foo.py"},
            {"name": "Bar", "kind": "Function"},
            {"name": "Baz", "kind": "Function"},
            {"name": "Qux", "kind": "Function"},
            {"name": "Quux", "kind": "Function"},
            {"name": "Corge", "kind": "Function"},
            {"name": "Grault", "kind": "Function"},
            {"name": "Garply", "kind": "Function"},
            {"name": "Waldo", "kind": "Function"},
        ]
    )
    assert len(entities) == 8
    assert entities[0] == {"name": "Foo", "kind": "Class", "file_path": "src/foo.py"}

    changed_files = _normalize_changed_files(
        ["", "a.py", "a.py", *[f"file_{index}.py" for index in range(12)]]
    )
    assert len(changed_files) == 10
    assert changed_files[0] == "a.py"

    observation = _make_observation(metadata={"file_path": "src/app.py"})
    assert _observation_changed_files(observation) == ("src/app.py",)


def test_validation_helpers_cover_support_swaps_structural_errors_and_quote_guards() -> None:
    validator = ClaimBundleValidator()
    observation = _make_observation()
    evidence = SupportingEvidenceCandidate.model_construct(
        evidence_kind="change_summary",
        quote_text="Rollback handling changed permanently.",
        summary_text="This describes the change.",
        span_start=None,
        span_end=None,
        about_hints=[],
        support_kind="states_change",
        support_source="observation_text",
        support_role="direct_statement",
        support_rationale=None,
        metadata={},
    )
    candidate = ClaimWithEvidenceCandidate.model_construct(
        claim_text="Rollback handling changed permanently.",
        claim_subkind="fix_or_mitigation",
        assertion_status="reported",
        claim_frame="author_statement",
        about_hints=[],
        confidence="high",
        derivation_kind="direct_from_observation",
        metadata={},
        supporting_evidence=[evidence],
    )

    normalized = validator._normalize_support_fields(candidate)
    assert normalized.supporting_evidence[0].support_kind == "direct_statement"
    assert normalized.supporting_evidence[0].support_role == "states_change"

    rejected = validator._validate_structural(
        observation=observation,
        candidate=normalized,
        claim_kind="unsupported_kind",
        claim_subkind="fix_or_mitigation",
        assertion_status="reported",
        claim_frame="author_statement",
    )
    assert rejected is not None
    assert rejected.reason_code == "unsupported_claim_kind"

    assert validator._quote_matches("", "quote") is False
    assert validator._quote_matches("source", "   ") is False

    claim_kind, claim_subkind, assertion_status, claim_frame = validator._resolve_claim_shape(
        observation=observation,
        candidate=ClaimWithEvidenceCandidate.model_validate(
            {
                "claim_text": "Need more tests for rollback handling.",
                "claim_subkind": None,
                "confidence": "high",
                "derivation_kind": "direct_from_observation",
                "metadata": {},
                "supporting_evidence": [
                    {
                        "evidence_kind": "change_summary",
                        "quote_text": "Need more tests for rollback handling.",
                        "summary_text": "The note requests more tests.",
                        "support_kind": "direct_statement",
                        "metadata": {},
                    }
                ],
            }
        ),
    )
    assert claim_kind is None
    assert claim_subkind is None
    assert assertion_status is not None
    assert claim_frame is not None


def test_claim_retrieval_helpers_cover_profile_builders_and_anchor_filters() -> None:
    service = ClaimSemanticRetrievalService(
        metadata_store=MagicMock(),
        vector_store=MagicMock(),
        embedding_provider=MagicMock(),
        query_parser=MagicMock(),
    )
    claim = SimpleNamespace(claim_id="claim-1")

    with patch("context_graph.models.profiles.semantic_claim_profile_from_claim", return_value="profile"):
        assert service.build_profile(claim) == "profile"

    with patch(
        "context_graph.backend.profile_vector_services.build_profile_key_records",
        return_value=("record-1",),
    ), patch(
        "context_graph.models.profiles.semantic_claim_profile_from_claim",
        return_value=SimpleNamespace(profile_id="profile-1", keys=()),
    ):
        assert semantic_claim_profile_key_records(claim, embedding_provider=MagicMock()) == ("record-1",)

    with patch(
        "context_graph.backend.profile_vector_services.profile_key_id",
        side_effect=lambda profile_id, text, origin: f"{profile_id}:{text}:{origin}",
    ), patch(
        "context_graph.models.profiles.semantic_claim_profile_from_claim",
        return_value=SimpleNamespace(
            profile_id="profile-1",
            keys=(SimpleNamespace(text="alpha", origin="raw"),),
        ),
    ):
        assert semantic_claim_profile_key_ids(claim) == ("profile-1:alpha:raw",)

    assert grounded_code_terms(("", "claim:repo:pkg.mod.__file__")) == (
        "pkg.mod.__file__",
        "mod",
        "mod.__file__",
    )
    assert _claim_code_anchors("`   ` src/app.py") == ("src/app.py", "app.py")
    assert _dedupe_preserve_order((" ", "Value", "value")) == ("Value",)
    assert _string_or_none(None) is None
