from __future__ import annotations

from context_graph.profiles import ProfileDeactivation
from context_graph.storage.backend.profile_vectors.store import (
    InMemoryProfileVectorStore,
    PgVectorProfileVectorStore,
    _cosine_similarity,
    _vector_literal,
)
from context_graph.storage.backend.profile_vectors.types import ProfileKeyRecord


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

    def cursor(self):
        return RecordingCursor(self)

    def commit(self):
        self.commits += 1


def make_record(
    profile_key_id: str,
    *,
    profile_id: str = "profile:1",
    repo_id: str = "demo",
    profile_kind: str = "semantic_fact_profile",
    relation_type: str | None = "JUSTIFIES",
    source_node_id: str | None = "main",
    target_node_id: str | None = "helper",
    key_text: str = "helper rollback",
    embedding: tuple[float, ...] = (1.0, 1.0, 0.0),
    is_active: bool = True,
) -> ProfileKeyRecord:
    return ProfileKeyRecord(
        profile_key_id=profile_key_id,
        profile_id=profile_id,
        repo_id=repo_id,
        profile_kind=profile_kind,
        relation_type=relation_type,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        key_text=key_text,
        embedding=embedding,
        is_active=is_active,
        valid_from_revision="r1",
        valid_to_revision=None,
        updated_at="2026-03-27T00:00:00+00:00",
    )


def test_in_memory_profile_vector_store_deactivation_reset_and_kind_filters() -> None:
    store = InMemoryProfileVectorStore()
    keep = make_record("pkey:1", profile_id="profile:1", profile_kind="semantic_fact_profile", embedding=(1.0, 0.0))
    deactivate = make_record("pkey:2", profile_id="profile:2", profile_kind="semantic_fact_profile", embedding=(0.0, 1.0))
    other_kind = make_record("pkey:3", profile_id="profile:3", profile_kind="code_relation_profile", embedding=(1.0, 1.0))
    replacement = make_record("pkey:4", profile_id="profile:4", profile_kind="semantic_fact_profile", embedding=(1.0, 1.0))
    store.upsert_profile_keys((keep, deactivate, other_kind, replacement))

    deactivation = ProfileDeactivation(
        repo_id="demo",
        profile_kind="semantic_fact_profile",
        relation_type="JUSTIFIES",
        source_node_id="main",
        target_node_id="helper",
        observed_at="2026-03-27T00:05:00+00:00",
        valid_to_revision="r2",
        replacement_profile_id="profile:4",
    )
    store.deactivate_profile_keys((deactivation,))

    hits = store.search_profile_keys(
        repo_id="demo",
        embedding=(1.0, 1.0),
        limit=10,
        active_only=False,
        profile_kinds=("semantic_fact_profile",),
    )
    assert [hit.profile_id for hit in hits] == ["profile:4", "profile:1", "profile:2"]
    assert store._records["pkey:2"].is_active is False
    assert store._records["pkey:2"].valid_to_revision == "r2"
    assert store._records["pkey:4"].is_active is True
    assert store.reset_repo_context("demo") == 4
    assert store.search_profile_keys(repo_id="demo", embedding=(1.0, 0.0), limit=5) == ()


def test_pgvector_profile_vector_store_search_sql_and_helper_functions() -> None:
    connection = RecordingConnection()
    store = PgVectorProfileVectorStore(connection, embedding_dim=3)
    connection.fetchall_values = [
        [
            ("profile:1", "pkey:1", "demo", "semantic_fact_profile", "helper rollback", 0.91),
            ("profile:2", "pkey:2", "demo", "semantic_fact_profile", "main helper", 0.67),
        ]
    ]

    hits = store.search_profile_keys(
        repo_id="demo",
        embedding=(1.0, 0.5, 0.0),
        limit=2,
        active_only=False,
        profile_kinds=("semantic_fact_profile",),
    )
    assert [hit.profile_id for hit in hits] == ["profile:1", "profile:2"]
    sql_text = "\n".join(sql for sql, _ in connection.executed if isinstance(sql, str))
    assert "ORDER BY embedding <=> CAST(%s AS vector)" in sql_text
    assert _vector_literal((1.0, 2.5, 3.0)) == "[1,2.5,3]"
    assert _cosine_similarity((1.0, 0.0), (1.0, 0.0)) == 1.0
    assert _cosine_similarity((0.0, 0.0), (1.0, 0.0)) == 0.0
    assert _cosine_similarity((), (1.0,)) == 0.0


def test_pgvector_profile_vector_store_bootstrap_upsert_deactivate_and_reset() -> None:
    connection = RecordingConnection()
    store = PgVectorProfileVectorStore(connection, embedding_dim=2)
    record = make_record("pkey:1", embedding=(1.0, 0.0))
    deactivation = ProfileDeactivation(
        repo_id="demo",
        profile_kind="semantic_fact_profile",
        relation_type="JUSTIFIES",
        source_node_id="main",
        target_node_id="helper",
        observed_at="2026-03-27T00:05:00+00:00",
        valid_to_revision="r2",
        replacement_profile_id=None,
    )

    store.bootstrap()
    store.upsert_profile_keys((record,))
    store.deactivate_profile_keys((deactivation,))
    connection.fetchone_values = [(1,)]
    deleted = store.reset_repo_context("demo")

    sql_text = "\n".join(sql for sql, _ in connection.executed if isinstance(sql, str))
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql_text
    assert "vector(2)" in sql_text
    assert "INSERT INTO cg_profile_keys" in sql_text
    assert "UPDATE cg_profile_keys" in sql_text
    assert "DELETE FROM cg_profile_keys WHERE repo_id = %s" in sql_text
    assert deleted == 1
    assert connection.commits >= 4
