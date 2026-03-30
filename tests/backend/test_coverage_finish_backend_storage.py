from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from context_graph.backend.metadata.store import (
    InMemoryMetadataStore,
    PostgresMetadataStore,
    SQLiteMetadataStore,
    _DbApiMetadataStore,
)
from context_graph.backend.metadata.types import RepoIndexState, RepoSourceState
from context_graph.backend.profile_vectors.store import (
    InMemoryProfileVectorStore,
    PgVectorProfileVectorStore,
    ProfileKeyRecord,
    _cosine_similarity,
    _current_vector_dimension,
)
from context_graph.models.profiles import ProfileDeactivation, ProfileRecord


def _make_profile(**overrides) -> ProfileRecord:
    defaults = dict(
        profile_id="profile-1",
        repo_id="repo-1",
        profile_kind="semantic_fact",
        profile_target_kind="Claim",
        profile_target_id="claim-1",
        relation_type="ABOUT",
        source_node_id="src-1",
        source_node_kind="File",
        target_node_id="tgt-1",
        target_node_kind="Function",
        target_revision_id="rev-1",
        value_text="value",
        keys=(),
        metadata={},
        is_active=True,
        valid_from_revision="rev-0",
        valid_to_revision=None,
        updated_at="2026-03-30T12:00:00Z",
    )
    defaults.update(overrides)
    return ProfileRecord(**defaults)


def _make_deactivation(**overrides) -> ProfileDeactivation:
    defaults = dict(
        repo_id="repo-1",
        profile_kind="semantic_fact",
        relation_type="ABOUT",
        source_node_id="src-1",
        target_node_id="tgt-1",
        replacement_profile_id=None,
        valid_to_revision="rev-2",
        observed_at="2026-03-30T13:00:00Z",
    )
    defaults.update(overrides)
    return ProfileDeactivation(**defaults)


class _ExplodingCursor:
    def execute(self, *_args, **_kwargs):
        raise RuntimeError("boom")

    def executemany(self, *_args, **_kwargs):
        raise RuntimeError("boom")


class _ExplodingConnection:
    def __init__(self) -> None:
        self.rollback = MagicMock()

    def cursor(self):
        return _ExplodingCursor()


class _DimensionCursor:
    def __init__(self, responses):
        self._responses = list(responses)

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return self._responses.pop(0)


def test_in_memory_metadata_store_covers_empty_upsert_mismatches_and_reset_counts() -> None:
    store = InMemoryMetadataStore()
    store.upsert_profiles(())

    store.upsert_profiles(
        (
            _make_profile(profile_id="match"),
            _make_profile(profile_id="relation-mismatch", relation_type="USES"),
            _make_profile(profile_id="source-mismatch", source_node_id="src-2"),
            _make_profile(profile_id="target-mismatch", target_node_id="tgt-2"),
        )
    )

    store.deactivate_profiles((_make_deactivation(),))
    profiles = {profile.profile_id: profile for profile in store.load_profiles("repo-1")}
    assert profiles["match"].is_active is False
    assert profiles["relation-mismatch"].is_active is True
    assert profiles["source-mismatch"].is_active is True
    assert profiles["target-mismatch"].is_active is True

    store.save_repo_index_state(
        RepoIndexState(
            repo_id="repo-1",
            root_path="/tmp/repo",
            latest_revision_id="rev-1",
            latest_indexed_at="2026-03-30T12:00:00Z",
            metadata={"nodes": 1},
        )
    )
    store.save_repo_source_state(
        RepoSourceState(
            repo_id="repo-1",
            provider="github",
            canonical_repo="octo/demo",
            clone_url="https://github.com/octo/demo.git",
            default_branch="main",
            clone_path="/tmp/repo",
            metadata={},
        )
    )
    store.record_index_run(repo_id="repo-1", revision_id="rev-1", status="ok", stats={"nodes": 1})

    deleted = store.reset_repo_context("repo-1")
    assert deleted == {
        "repo_state": 1,
        "repo_source": 1,
        "current_nodes": 0,
        "current_edges": 0,
        "profiles": 4,
        "index_runs": 1,
    }


def test_db_api_metadata_store_rollback_path_and_postgres_from_dsn_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _DbApiMetadataStore(connection=_ExplodingConnection(), placeholder="?")
    with pytest.raises(RuntimeError, match="boom"):
        store._executemany("INSERT INTO demo VALUES (?)", [(1,)])
    store.connection.rollback.assert_called_once()

    fake_psycopg = types.SimpleNamespace(
        ClientCursor=object,
        connect=lambda dsn, cursor_factory=None: ("psycopg", dsn, cursor_factory),
    )
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    created = PostgresMetadataStore.from_dsn("postgres://demo")
    assert created.connection[0] == "psycopg"

    monkeypatch.setitem(sys.modules, "psycopg", None)
    fake_psycopg2 = types.SimpleNamespace(connect=lambda dsn: ("psycopg2", dsn))
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    fallback = PostgresMetadataStore.from_dsn("postgres://demo")
    assert fallback.connection[0] == "psycopg2"

    monkeypatch.setitem(sys.modules, "psycopg2", None)
    with pytest.raises(RuntimeError, match="requires psycopg or psycopg2"):
        PostgresMetadataStore.from_dsn("postgres://demo")


def test_sqlite_metadata_store_empty_branches_and_repo_lock() -> None:
    store = SQLiteMetadataStore(":memory:")
    try:
        store.upsert_profiles(())
        store.deactivate_profiles(())
        with store.repo_lock("repo-1"):
            pass
    finally:
        store.close()


def test_in_memory_profile_vector_store_mismatch_branches_and_search_repo_filter() -> None:
    store = InMemoryProfileVectorStore()
    store.upsert_profile_keys(
        (
            ProfileKeyRecord(
                profile_key_id="deactivate-me",
                profile_id="profile-1",
                repo_id="repo-1",
                profile_kind="semantic_fact",
                relation_type="ABOUT",
                source_node_id="src-1",
                target_node_id="tgt-1",
                key_text="alpha",
                key_origin="raw_text",
                embedding=(1.0, 0.0),
                is_active=True,
                valid_from_revision=None,
                valid_to_revision=None,
                updated_at="2026-03-30T12:00:00Z",
            ),
            ProfileKeyRecord(
                profile_key_id="repo-mismatch",
                profile_id="profile-2",
                repo_id="repo-2",
                profile_kind="semantic_fact",
                relation_type="ABOUT",
                source_node_id="src-1",
                target_node_id="tgt-1",
                key_text="beta",
                key_origin="raw_text",
                embedding=(1.0, 0.0),
                is_active=True,
                valid_from_revision=None,
                valid_to_revision=None,
                updated_at="2026-03-30T12:00:00Z",
            ),
            ProfileKeyRecord(
                profile_key_id="kind-mismatch",
                profile_id="profile-3",
                repo_id="repo-1",
                profile_kind="other_kind",
                relation_type="ABOUT",
                source_node_id="src-1",
                target_node_id="tgt-1",
                key_text="gamma",
                key_origin="raw_text",
                embedding=(1.0, 0.0),
                is_active=True,
                valid_from_revision=None,
                valid_to_revision=None,
                updated_at="2026-03-30T12:00:00Z",
            ),
            ProfileKeyRecord(
                profile_key_id="relation-mismatch",
                profile_id="profile-4",
                repo_id="repo-1",
                profile_kind="semantic_fact",
                relation_type="USES",
                source_node_id="src-1",
                target_node_id="tgt-1",
                key_text="delta",
                key_origin="raw_text",
                embedding=(1.0, 0.0),
                is_active=True,
                valid_from_revision=None,
                valid_to_revision=None,
                updated_at="2026-03-30T12:00:00Z",
            ),
            ProfileKeyRecord(
                profile_key_id="source-mismatch",
                profile_id="profile-5",
                repo_id="repo-1",
                profile_kind="semantic_fact",
                relation_type="ABOUT",
                source_node_id="src-2",
                target_node_id="tgt-1",
                key_text="epsilon",
                key_origin="raw_text",
                embedding=(1.0, 0.0),
                is_active=True,
                valid_from_revision=None,
                valid_to_revision=None,
                updated_at="2026-03-30T12:00:00Z",
            ),
            ProfileKeyRecord(
                profile_key_id="target-mismatch",
                profile_id="profile-6",
                repo_id="repo-1",
                profile_kind="semantic_fact",
                relation_type="ABOUT",
                source_node_id="src-1",
                target_node_id="tgt-2",
                key_text="zeta",
                key_origin="raw_text",
                embedding=(1.0, 0.0),
                is_active=True,
                valid_from_revision=None,
                valid_to_revision=None,
                updated_at="2026-03-30T12:00:00Z",
            ),
            ProfileKeyRecord(
                profile_key_id="replacement",
                profile_id="replacement-profile",
                repo_id="repo-1",
                profile_kind="semantic_fact",
                relation_type="ABOUT",
                source_node_id="src-1",
                target_node_id="tgt-1",
                key_text="eta",
                key_origin="raw_text",
                embedding=(1.0, 0.0),
                is_active=True,
                valid_from_revision=None,
                valid_to_revision=None,
                updated_at="2026-03-30T12:00:00Z",
            ),
        )
    )

    store.deactivate_profile_keys(
        (
            ProfileDeactivation(
                repo_id="repo-1",
                profile_kind="semantic_fact",
                relation_type="ABOUT",
                source_node_id="src-1",
                target_node_id="tgt-1",
                replacement_profile_id="replacement-profile",
                valid_to_revision="rev-2",
                observed_at="2026-03-30T13:00:00Z",
            ),
        )
    )

    records = store._records
    assert records["deactivate-me"].is_active is False
    assert records["repo-mismatch"].is_active is True
    assert records["kind-mismatch"].is_active is True
    assert records["relation-mismatch"].is_active is True
    assert records["source-mismatch"].is_active is True
    assert records["target-mismatch"].is_active is True
    assert records["replacement"].is_active is True

    hits = store.search_profile_keys(
        repo_id="repo-1",
        embedding=(1.0, 0.0),
        limit=10,
        active_only=False,
    )
    assert all(hit.repo_id == "repo-1" for hit in hits)


def test_pg_vector_reset_repo_context_rolls_back_and_vector_helpers_cover_none_and_empty() -> None:
    cursor = MagicMock()
    cursor.execute.side_effect = RuntimeError("db exploded")
    connection = MagicMock()
    connection.cursor.return_value = cursor

    store = PgVectorProfileVectorStore(connection, embedding_dim=3)
    store.bootstrap = lambda: None

    with pytest.raises(RuntimeError, match="db exploded"):
        store.reset_repo_context("repo-1")
    connection.rollback.assert_called_once()

    assert _current_vector_dimension(
        _DimensionCursor([("cg_profile_keys",), (None,)]),
        "cg_profile_keys",
        "embedding_clients",
    ) is None
    assert _cosine_similarity([], [1.0]) == 0.0
