from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from context_graph.entity_nodes import make_entity_node
from context_graph.profiles import (
    FACT_EDGE_TARGET_KIND,
    SEMANTIC_FACT_PROFILE_KIND,
    _relation_label,
    _string_or_none,
    profile_deactivation_from_fact_invalidation,
    semantic_fact_profile_from_fact,
)


def test_semantic_fact_profile_from_fact_builds_profile_with_source_target_metadata() -> None:
    source = make_entity_node(
        node_id="symbol:demo:main",
        repo_id="demo",
        kind="Function",
        name="main",
        qualified_name="demo.app.main",
        file_path="app.py",
        aliases=("main",),
    )
    target = make_entity_node(
        node_id="symbol:demo:helper",
        repo_id="demo",
        kind="Function",
        name="helper",
        qualified_name="demo.app.helper",
        file_path="helpers.py",
        aliases=("helper",),
    )
    fact = SimpleNamespace(
        uuid="fact:1",
        group_id="demo",
        source_node_uuid=source.uuid,
        target_node_uuid=target.uuid,
        name="JUSTIFIES",
        fact="main uses helper for shared rollback logic",
        episodes=("episode:1",),
        created_at=datetime(2026, 3, 27, tzinfo=timezone.utc),
        valid_at=None,
        invalid_at=None,
        expired_at=None,
        attributes={},
        relationship_keywords=(),
    )

    profile = semantic_fact_profile_from_fact(fact, source=source, target=target)

    assert profile.profile_id == "profile:demo:semantic_fact_profile:fact:1"
    assert profile.profile_kind == SEMANTIC_FACT_PROFILE_KIND
    assert profile.profile_target_kind == FACT_EDGE_TARGET_KIND
    assert profile.relation_type == "JUSTIFIES"
    assert profile.source_node_kind == "Function"
    assert profile.target_node_kind == "Function"
    assert profile.is_active is True
    assert "source_file_path" in profile.metadata
    assert "target_file_path" in profile.metadata
    assert "main" in profile.keys
    assert "helper" in profile.keys
    assert profile.keys_json().startswith("[")
    assert '"fact_id":"fact:1"' in profile.metadata_json()


def test_semantic_fact_profile_prefers_relationship_keywords_and_fallback_metadata() -> None:
    fact = SimpleNamespace(
        uuid="fact:2",
        group_id="demo",
        source_node_uuid="node:source",
        target_node_uuid="node:target",
        name=None,
        fact="source relates to target",
        episodes=(),
        created_at="2026-03-27T00:00:00+00:00",
        valid_at=None,
        invalid_at=None,
        expired_at="2026-03-27T01:00:00+00:00",
        attributes={"source_kind": "Module", "target_kind": "File"},
        relationship_keywords=("rollback", "shared logic", "rollback"),
    )

    profile = semantic_fact_profile_from_fact(fact, source=None, target=None)

    assert profile.source_node_kind == "Module"
    assert profile.target_node_kind == "File"
    assert profile.is_active is False
    assert profile.updated_at == "2026-03-27T01:00:00+00:00"
    assert profile.keys == ("rollback", "shared logic")


def test_profile_deactivation_from_fact_invalidation_tracks_optional_replacement() -> None:
    invalidation = SimpleNamespace(
        repo_id="demo",
        relation_type="JUSTIFIES",
        source_node_id="node:source",
        target_node_id="node:target",
        observed_at="2026-03-27T00:00:00+00:00",
        replacement_fact_id="fact:new",
    )

    deactivation = profile_deactivation_from_fact_invalidation(invalidation)

    assert deactivation.repo_id == "demo"
    assert deactivation.profile_kind == SEMANTIC_FACT_PROFILE_KIND
    assert deactivation.replacement_profile_id == "profile:demo:semantic_fact_profile:fact:new"

    invalidation.replacement_fact_id = None
    deactivation = profile_deactivation_from_fact_invalidation(invalidation)
    assert deactivation.replacement_profile_id is None


def test_profile_helper_functions_normalize_relation_labels_and_strings() -> None:
    assert _relation_label("RELATES_TO") == "relates to"
    assert _relation_label(None) == "relates to"
    assert _string_or_none(None) is None
    assert _string_or_none("   ") is None
    assert _string_or_none(" value ") == "value"
