from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.profiles import ProfileDeactivation, ProfileRecord
from context_graph.storage.backend.metadata.store import (
    PostgresMetadataStore,
    SQLiteMetadataStore,
    _profile_from_row,
    _stable_lock_id,
)
from context_graph.storage.backend.metadata.types import RepoIndexState, RepoSourceState


class RecordingCursor:
    def __init__(self, connection):
        self.connection = connection
        self._fetchone = None
        self._fetchall = []

    def execute(self, sql, params=()):
        self.connection.executed.append((sql, params))
        normalized = sql.lstrip().upper() if isinstance(sql, str) else ""
        if normalized.startswith("SELECT") and self.connection.fetchone_values:
            self._fetchone = self.connection.fetchone_values.pop(0)
        else:
            self._fetchone = (0,)
        if normalized.startswith("SELECT") and self.connection.fetchall_values:
            self._fetchall = self.connection.fetchall_values.pop(0)
        else:
            self._fetchall = []
        return self

    def executemany(self, sql, rows):
        materialized = list(rows)
        self.connection.executed.append((sql, materialized))
        return self

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall


class RecordingConnection:
    def __init__(self):
        self.executed: list[tuple[str, object]] = []
        self.fetchone_values: list[tuple] = []
        self.fetchall_values: list[list[tuple]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def make_snapshot(revision_id: str = "r1") -> GraphSnapshot:
    project = CanonicalNode("project:demo", "demo", "Project", "demo", qualified_name="demo")
    file_node = CanonicalNode(
        "file:demo:demo.app.__file__",
        "demo",
        "File",
        "app.py",
        qualified_name="demo.app.__file__",
        file_path="app.py",
    )
    edge = CanonicalEdge(
        "edge:demo:contains",
        "demo",
        "CONTAINS_FILE",
        project.node_id,
        file_node.node_id,
    )
    return GraphSnapshot(
        repo_id="demo",
        revision_id=revision_id,
        root_path="/tmp/demo",
        generated_at="2026-03-27T00:00:00+00:00",
        nodes=(project, file_node),
        edges=(edge,),
        metadata={"source": "unit-test"},
    )


def make_profile(profile_id: str = "profile:demo:1", *, active: bool = True) -> ProfileRecord:
    return ProfileRecord(
        profile_id=profile_id,
        repo_id="demo",
        profile_kind="semantic_fact_profile",
        profile_target_kind="FactEdge",
        profile_target_id="fact:1",
        relation_type="JUSTIFIES",
        source_node_id="source",
        source_node_kind="Function",
        target_node_id="target",
        target_node_kind="Function",
        target_revision_id=None,
        value_text="main uses helper",
        keys=("main helper",),
        metadata={"episode_ids": ["turn-1"]},
        is_active=active,
        valid_from_revision="r1",
        valid_to_revision=None,
        updated_at="2026-03-27T00:00:00+00:00",
    )


def test_sqlite_metadata_store_round_trips_repo_index_and_source_state() -> None:
    store = SQLiteMetadataStore(":memory:")
    snapshot = make_snapshot()
    repo_state = RepoIndexState(
        repo_id="demo",
        root_path="/tmp/demo",
        latest_revision_id="r1",
        latest_indexed_at="2026-03-27T00:00:00+00:00",
        metadata={"source": "unit-test"},
        latest_commit_sha="abc123",
        latest_branch="main",
        last_commit_indexed_at="2026-03-27T00:01:00+00:00",
    )
    source_state = RepoSourceState(
        repo_id="demo",
        provider="github",
        canonical_repo="openai/context-graph",
        clone_url="https://github.com/openai/context-graph.git",
        default_branch="main",
        clone_path="/tmp/demo",
        last_fetch_at="2026-03-27T00:00:00+00:00",
        last_fetched_head_sha="deadbeef",
        last_github_sync_at="2026-03-27T00:02:00+00:00",
        last_github_sync_status="success",
        last_github_sync_error=None,
        metadata={"source": "github_repo"},
    )

    store.save_latest_snapshot(snapshot)
    store.save_repo_index_state(repo_state)
    store.save_repo_source_state(source_state)

    reloaded_snapshot = store.load_latest_snapshot("demo")
    assert reloaded_snapshot is not None
    assert reloaded_snapshot.revision_id == "r1"
    assert store.load_repo_index_state("demo") == repo_state
    assert store.load_repo_source_state("demo") == source_state


def test_sqlite_metadata_store_profile_upsert_deactivate_and_reset_paths() -> None:
    store = SQLiteMetadataStore(":memory:")
    snapshot = make_snapshot()
    active = make_profile("profile:demo:1", active=True)
    replacement = ProfileRecord(
        profile_id="profile:demo:2",
        repo_id="demo",
        profile_kind="semantic_fact_profile",
        profile_target_kind="FactEdge",
        profile_target_id="fact:2",
        relation_type="JUSTIFIES",
        source_node_id="source",
        source_node_kind="Function",
        target_node_id="target",
        target_node_kind="Function",
        target_revision_id=None,
        value_text="updated helper",
        keys=("helper",),
        metadata={},
        is_active=True,
        valid_from_revision="r2",
        valid_to_revision=None,
        updated_at="2026-03-27T00:10:00+00:00",
    )
    deactivation = ProfileDeactivation(
        repo_id="demo",
        profile_kind="semantic_fact_profile",
        relation_type="JUSTIFIES",
        source_node_id="source",
        target_node_id="target",
        observed_at="2026-03-27T00:20:00+00:00",
        valid_to_revision="r2",
        replacement_profile_id="profile:demo:2",
    )

    store.save_latest_snapshot(snapshot)
    store.upsert_profiles((active, replacement))
    store.deactivate_profiles((deactivation,))
    active_profiles = store.load_profiles("demo", active_only=True)
    all_profiles = store.load_profiles("demo", active_only=False)

    assert [profile.profile_id for profile in active_profiles] == ["profile:demo:2"]
    assert len(all_profiles) == 2
    inactive = next(profile for profile in all_profiles if profile.profile_id == "profile:demo:1")
    assert inactive.is_active is False
    assert inactive.valid_to_revision == "r2"
    assert inactive.updated_at == "2026-03-27T00:20:00+00:00"

    store.record_index_run(repo_id="demo", revision_id="r1", status="applied", stats={"nodes": 2})
    deleted = store.reset_repo_context("demo")
    assert deleted["profiles"] == 2
    assert deleted["repo_state"] == 1
    assert deleted["repo_source"] == 0
    assert deleted["index_runs"] == 1


def test_postgres_metadata_store_loaders_and_writers_use_expected_sql_shapes() -> None:
    connection = RecordingConnection()
    store = PostgresMetadataStore(connection)

    connection.fetchone_values = [
        ("/tmp/demo", "r1", "2026-03-27T00:00:00+00:00", json.dumps({"source": "unit-test"})),
        (0,),
        (0,),
        (
            "/tmp/demo",
            "r2",
            "2026-03-27T00:01:00+00:00",
            json.dumps({"source": "git"}),
            "abc123",
            "main",
            "2026-03-27T00:01:00+00:00",
        ),
        (
            "github",
            "openai/context-graph",
            "https://github.com/openai/context-graph.git",
            "main",
            "/tmp/demo",
            "2026-03-27T00:00:00+00:00",
            "deadbeef",
            None,
            None,
            None,
            json.dumps({"source": "github_repo"}),
        ),
    ]
    connection.fetchall_values = [
        [],
        [[json.dumps(node.to_dict())] for node in make_snapshot().nodes],
        [[json.dumps(edge.to_dict())] for edge in make_snapshot().edges],
    ]

    snapshot = store.load_latest_snapshot("demo")
    assert snapshot is not None
    assert snapshot.repo_id == "demo"
    assert snapshot.revision_id == "r1"

    repo_state = store.load_repo_index_state("demo")
    assert repo_state is not None
    assert repo_state.latest_commit_sha == "abc123"

    source_state = store.load_repo_source_state("demo")
    assert source_state is not None
    assert source_state.canonical_repo == "openai/context-graph"

    store.save_repo_source_state(source_state)
    store.save_repo_index_state(repo_state)
    assert connection.commits >= 2


def test_postgres_metadata_store_handles_duplicate_column_bootstrap_and_advisory_lock() -> None:
    class DuplicateColumnStore(PostgresMetadataStore):
        def __init__(self, connection):
            super().__init__(connection)
            self.alter_calls = 0

        def _execute(self, sql: str, params=()):
            if "ALTER TABLE cg_repo_state ADD COLUMN" in sql:
                self.alter_calls += 1
                raise RuntimeError("duplicate column name")
            return super()._execute(sql, params)

    connection = RecordingConnection()
    store = DuplicateColumnStore(connection)
    store.bootstrap()
    assert connection.rollbacks >= 1

    with store.repo_lock("openai/context-graph"):
        pass
    sql_text = "\n".join(sql for sql, _ in connection.executed if isinstance(sql, str))
    assert "SELECT pg_advisory_lock(%s)" in sql_text
    assert "SELECT pg_advisory_unlock(%s)" in sql_text


def test_postgres_metadata_store_from_dsn_falls_back_and_profile_row_helper_round_trips() -> None:
    fake_psycopg = SimpleNamespace(
        ClientCursor=object,
        connect=lambda dsn, cursor_factory=None: ("psycopg", dsn, cursor_factory),
    )
    with pytest.MonkeyPatch().context() as mp:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "psycopg":
                return fake_psycopg
            return real_import(name, *args, **kwargs)

        mp.setattr(builtins, "__import__", fake_import)
        store = PostgresMetadataStore.from_dsn("postgresql://example")
    assert store.connection[0] == "psycopg"

    row = (
        "profile:1",
        "demo",
        "semantic_fact_profile",
        "FactEdge",
        "fact:1",
        "JUSTIFIES",
        "source",
        "Function",
        "target",
        "Function",
        None,
        "main uses helper",
        '["main","helper"]',
        '{"source":"unit-test"}',
        1,
        "r1",
        None,
        "2026-03-27T00:00:00+00:00",
    )
    profile = _profile_from_row(row)
    assert profile.profile_id == "profile:1"
    assert profile.keys == ("main", "helper")
    assert profile.metadata["source"] == "unit-test"
    assert _stable_lock_id("openai/context-graph") == _stable_lock_id("openai/context-graph")
