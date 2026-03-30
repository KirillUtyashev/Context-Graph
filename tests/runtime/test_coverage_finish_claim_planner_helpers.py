from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

from context_graph.backend import MutationPlan
from context_graph.provenance.claim_planner import (
    CandidateClaimState,
    CandidateRetrievalScore,
    ClaimMutationPlanner,
    ClaimPlanningSession,
    ClaimPromotionService,
    CurrentClaimReader,
    _candidate_fact_identifier,
    _candidate_has_strong_signal,
    _claim_kind_compatibility_score,
    _claim_with_backfill_ingested_at,
    _close_unstarted_coroutine,
    _coerce_bool,
    _compat_fact_key,
    _derive_thread_id,
    _derive_thread_kind,
    _int_or_none,
    _is_strictly_newer,
    _legacy_support_rationale,
    _legacy_support_role,
    _parse_datetime,
    _parse_json_object,
)
from context_graph.provenance.models import ClaimRecord, EvidenceRecord, Observation, PersistedEvidenceRecord
from context_graph.provenance.validation import ClaimValidationResult, ValidatedClaimBundle


def _make_claim(**overrides) -> ClaimRecord:
    defaults = dict(
        claim_id="claim-1",
        repo_id="repo-1",
        canonical_key="change_claim:rollback handling changed",
        claim_text="Rollback handling changed.",
        claim_kind="change_claim",
        claim_subkind="fix_or_mitigation",
        first_observed_at="2026-03-30T12:00:00Z",
        latest_observed_at="2026-03-30T12:00:00Z",
        is_current=True,
        ingested_at=None,
        valid_from="2026-03-30T12:00:00Z",
        valid_to=None,
        observation_id="obs-1",
        source_kind="pull_request",
        source_id="github:pr:repo-1:12",
        context_kind=None,
        context_id=None,
        thread_kind=None,
        thread_id=None,
        metadata={},
    )
    defaults.update(overrides)
    return ClaimRecord(**defaults)


def _make_observation(**overrides) -> Observation:
    defaults = dict(
        observation_id="obs-1",
        repo_id="repo-1",
        observation_kind="git_commit_message",
        source_kind="pull_request",
        source_id="github:pr:repo-1:12",
        observed_at="2026-03-30T12:00:00Z",
        author="octocat",
        title="Rollback handling",
        text="Rollback handling changed.",
        metadata={},
    )
    defaults.update(overrides)
    return Observation(**defaults)


def _make_evidence_record(*, evidence_kind: str, summary_text: str | None = "summary") -> PersistedEvidenceRecord:
    return PersistedEvidenceRecord(
        evidence=EvidenceRecord(
            evidence_id=f"evidence:{evidence_kind}",
            repo_id="repo-1",
            evidence_kind=evidence_kind,
            quote_text="quote",
            summary_text=summary_text,
            span_start=None,
            span_end=None,
            support_kind="direct_statement",
            support_source="observation_text",
            fact_key=None,
            claim_text="Rollback handling changed.",
            claim_kind="change_claim",
            claim_subkind="fix_or_mitigation",
            assertion_status="reported",
            claim_frame="author_statement",
            claim_confidence="high",
            claim_derivation_kind="direct_from_observation",
            observation_id="obs-1",
            observed_at="2026-03-30T12:00:00Z",
            metadata={},
        ),
        about_record_ids=(),
    )


def _make_validated_bundle() -> ValidatedClaimBundle:
    return ValidatedClaimBundle(
        claim_text="Rollback handling changed.",
        claim_kind="change_claim",
        claim_subkind="fix_or_mitigation",
        assertion_status="reported",
        claim_frame="author_statement",
        about_hints=(),
        resolved_about_record_ids=("file:repo-1:src/app.py@r1",),
        unresolved_about_hints=(),
        confidence="high",
        derivation_kind="direct_from_observation",
        metadata={},
        supporting_evidence=(),
    )


def _make_candidate(**overrides) -> CandidateClaimState:
    defaults = dict(
        claim=_make_claim(),
        about_record_ids=("file:repo-1:src/app.py@r1",),
        evidence_record_ids=(),
        support_texts=("Rollback handling changed.",),
        legacy_evidence_records=(),
        backfill_required=False,
        origin="persisted",
    )
    defaults.update(overrides)
    return CandidateClaimState(**defaults)


def _make_score(**overrides) -> CandidateRetrievalScore:
    defaults = dict(
        candidate=_make_candidate(),
        same_thread=False,
        about_overlap=0,
        lexical_overlap=0,
        code_anchor_overlap=0,
        numeric_anchor_overlap=0,
        kind_compatibility=0,
        retrieval_sources=(),
        total_score=0.0,
    )
    defaults.update(overrides)
    return CandidateRetrievalScore(**defaults)


def test_current_claim_reader_error_paths_and_legacy_claim_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    backend_driver = types.SimpleNamespace(execute_query=lambda *_args, **_kwargs: object())
    reader = CurrentClaimReader(backend_driver=backend_driver)

    with patch(
        "context_graph.provenance.claim_planner.run_backend_coroutine_sync",
        side_effect=RuntimeError("boom"),
    ):
        assert reader._load_claim_nodes(repo_id="repo-1") == ()
        assert reader._load_legacy_evidence_claims(repo_id="repo-1") == ()

    missing_claim_record = PersistedEvidenceRecord(
        evidence=EvidenceRecord(
            evidence_id="evidence:missing",
            repo_id="repo-1",
            evidence_kind="change_summary",
            quote_text="quote",
            summary_text="summary",
            span_start=None,
            span_end=None,
            support_kind="direct_statement",
            support_source="observation_text",
            fact_key=None,
            claim_text=None,
            claim_kind="change_claim",
            claim_subkind="fix_or_mitigation",
            assertion_status="reported",
            claim_frame="author_statement",
            claim_confidence="high",
            claim_derivation_kind="direct_from_observation",
            observation_id="obs-1",
            observed_at="2026-03-30T12:00:00Z",
            metadata={},
        ),
        about_record_ids=("file:repo-1:src/app.py@r1",),
    )
    with patch(
        "context_graph.provenance.claim_planner.run_backend_coroutine_sync",
        return_value=[{}],
    ), patch(
        "context_graph.provenance.claim_planner._parse_persisted_evidence_record",
        return_value=missing_claim_record,
    ):
        assert reader._load_legacy_evidence_claims(repo_id="repo-1") == ()


def test_claim_planning_session_seeding_discard_and_delegation() -> None:
    seeded = _make_candidate()
    session = ClaimPlanningSession(
        planner=MagicMock(),
        repo_id="repo-1",
        seeded_candidates=(seeded,),
    )
    assert session.current_candidates() == (seeded,)

    duplicate = _make_candidate()
    new_candidate = _make_candidate(claim=_make_claim(claim_id="claim-2", canonical_key="other"))
    lazy_session = ClaimPlanningSession(
        planner=MagicMock(),
        repo_id="repo-1",
        seed_loader=lambda: (duplicate, new_candidate),
    )
    lazy_session.record_current_candidate(duplicate)
    assert tuple(candidate.claim.claim_id for candidate in lazy_session.current_candidates()) == (
        "claim-2",
        "claim-1",
    )

    no_loader_session = ClaimPlanningSession(
        planner=MagicMock(),
        repo_id="repo-1",
        seed_loader=lambda: (),
    )
    no_loader_session._seed_loader = None
    no_loader_session._seeded = False
    no_loader_session._ensure_seeded()
    assert no_loader_session.current_candidates() == ()

    discard_session = ClaimPlanningSession(planner=MagicMock(), repo_id="repo-1")
    discard_session._current_by_claim_id[seeded.claim.claim_id] = seeded
    discard_session.discard_current_candidate(seeded.claim.claim_id)
    assert discard_session.current_candidates() == ()

    planner = MagicMock()
    planner._plan_validated_claim_bundle_in_session.return_value = "planned"
    delegated_session = ClaimPlanningSession(planner=planner, repo_id="repo-1")
    assert (
        delegated_session.plan_validated_claim_bundle(
            observation=_make_observation(),
            bundle=MagicMock(),
        )
        == "planned"
    )


def test_claim_mutation_planner_small_branches_and_promotion_service() -> None:
    bundle = _make_validated_bundle()
    validation = ClaimValidationResult(validated_claims=(bundle,))
    planner = ClaimMutationPlanner(
        backend_driver=None,
        evidence_planner=MagicMock(prepare_validated_claim_bundle=MagicMock(return_value=())),
    )
    session = ClaimPlanningSession(planner=planner, repo_id="repo-1")

    promotion_candidates = planner.build_promotion_candidates(validation)
    assert len(promotion_candidates) == 1
    assert promotion_candidates[0].claim_kind == "change_claim"

    empty_result = planner._plan_validated_claim_bundle_in_session(
        session=session,
        observation=_make_observation(),
        bundle=bundle,
    )
    assert isinstance(empty_result.plan, MutationPlan)
    assert empty_result.invalidated_count == 0

    assert planner._resolve_candidate_relations(
        observation=_make_observation(),
        new_claim=_make_claim(),
        about_record_ids=(),
        candidates=(),
        scope_label="thread",
    ) == ()

    assert planner._resolve_legacy_conflicts(
        observation=_make_observation(),
        new_claim=_make_claim(),
        about_record_ids=(),
        candidates=(_make_candidate(),),
        scope_label="thread",
    ) == ()

    planner_with_conflicts = ClaimMutationPlanner(
        backend_driver=None,
        fact_conflict_resolution_service=MagicMock(resolve=MagicMock(side_effect=RuntimeError("boom"))),
    )
    assert planner_with_conflicts._resolve_legacy_conflicts(
        observation=_make_observation(),
        new_claim=_make_claim(),
        about_record_ids=(),
        candidates=(_make_candidate(),),
        scope_label="thread",
    ) == ()

    promotion_service = ClaimPromotionService()
    assert isinstance(promotion_service.planner, ClaimMutationPlanner)
    assert len(promotion_service.prepare_candidates(validation)) == 1


def test_claim_planner_relation_and_helper_branches() -> None:
    planner = ClaimMutationPlanner(backend_driver=None)
    assert (
        planner._plan_claim_relation(
            repo_id="repo-1",
            source_claim_id="claim-1",
            target_claim_id="claim-2",
            relation_type="unknown_relation",
            rationale="why",
            confidence="high",
            observed_at="2026-03-30T12:00:00Z",
            is_active=True,
        ).operation_count()
        == 0
    )
    assert planner._build_claim_relation_profile(
        repo_id="repo-1",
        source_claim_id="claim-1",
        target_claim_id="claim-2",
        relation_type="unknown_relation",
        rationale="why",
        confidence="high",
        is_active=True,
        updated_at="2026-03-30T12:00:00Z",
    ) is None

    assert _candidate_has_strong_signal(_make_score(lexical_overlap=5)) is True
    assert _candidate_has_strong_signal(_make_score(same_thread=True, lexical_overlap=4)) is True
    assert _claim_kind_compatibility_score("approval", "bug_report") == 0

    assert _legacy_support_role(_make_evidence_record(evidence_kind="change_summary")) == "states_change"
    assert _legacy_support_role(_make_evidence_record(evidence_kind="bug_signal")) == "reports_observation"
    assert _legacy_support_role(_make_evidence_record(evidence_kind="requirement_detail")) == "requests_change"
    assert _legacy_support_role(_make_evidence_record(evidence_kind="misc")) == "provides_context"
    assert (
        _legacy_support_rationale(_make_evidence_record(evidence_kind="misc", summary_text="   "))
        == "Legacy evidence attached during claim backfill."
    )

    ingested_claim = _make_claim(ingested_at="2026-03-30T12:00:00Z")
    assert _claim_with_backfill_ingested_at(ingested_claim).ingested_at == "2026-03-30T12:00:00Z"
    assert _derive_thread_kind("pull_request") == "pull_request"
    assert _derive_thread_id(source_kind="pull_request", source_id="github:pr:repo-1:42") == "42"
    assert _derive_thread_id(source_kind="issue", source_id="github:issue:repo-1:7") == "7"
    assert _derive_thread_id(source_kind="commit", source_id="commit:abc123") == "abc123"
    assert _derive_thread_id(source_kind="other", source_id="custom-source") == "custom-source"

    candidate_without_fact_key = _make_candidate(legacy_evidence_records=(_make_evidence_record(evidence_kind="misc"),))
    assert _candidate_fact_identifier(candidate_without_fact_key) == _compat_fact_key(candidate_without_fact_key.claim)
    assert _compat_fact_key(_make_claim()).startswith("change_claim:")

    assert _is_strictly_newer(None, "2026-03-30T12:00:00Z") is False
    assert _parse_datetime("") is None
    assert _parse_datetime("not-a-date") is None
    assert _parse_json_object({"a": 1}) == {"a": 1}
    assert _coerce_bool(None, default=False) is False
    assert _coerce_bool("yes", default=False) is True
    assert _coerce_bool("no", default=True) is False
    assert _coerce_bool(7, default=False) is True
    assert _int_or_none(None) is None
    assert _int_or_none("12") == 12
    assert _int_or_none("bad") is None

    _close_unstarted_coroutine("not-a-coroutine")

    async def coro():
        return 1

    pending = coro()
    try:
        assert pending.cr_frame is not None
        _close_unstarted_coroutine(pending)
    finally:
        pending.close()

    fake_coroutine = types.SimpleNamespace(cr_frame=None, close=MagicMock(side_effect=RuntimeError("close failed")))
    with patch("context_graph.provenance.claim_planner.inspect.iscoroutine", return_value=True):
        _close_unstarted_coroutine(fake_coroutine)
