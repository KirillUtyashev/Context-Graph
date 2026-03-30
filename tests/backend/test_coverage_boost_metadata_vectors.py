"""Tests to boost coverage for metadata store and profile vector store modules."""

from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import contextmanager
from typing import Any, Iterator, Sequence
from unittest.mock import MagicMock, patch

from context_graph.backend.metadata.store import (
    InMemoryMetadataStore,
    SQLiteMetadataStore,
    _profile_from_row,
    _rollback_connection,
    _stable_lock_id,
)
from context_graph.backend.profile_vectors.store import (
    InMemoryProfileVectorStore,
    _cosine_similarity,
    _vector_literal,
)
from context_graph.backend.metadata.types import RepoIndexState, RepoSourceState
from context_graph.models.graph import GraphSnapshot
from context_graph.models.profiles import (
    ProfileDeactivation,
    ProfileKeyDefinition,
    ProfileRecord,
)


def _make_profile(**overrides) -> ProfileRecord:
    defaults = dict(
        profile_id="prof-1",
        repo_id="repo-1",
        profile_kind="semantic_fact",
        profile_target_kind="claim",
        profile_target_id="claim-1",
        relation_type="ABOUT",
        source_node_id="node-src",
        source_node_kind="File",
        target_node_id="node-tgt",
        target_node_kind="Function",
        target_revision_id="rev-1",
        value_text="some value",
        keys=(),
        metadata={},
        is_active=True,
        valid_from_revision="rev-0",
        valid_to_revision=None,
        updated_at="2025-01-01T00:00:00Z",
    )
    defaults.update(overrides)
    return ProfileRecord(**defaults)


class TestInMemoryMetadataDeactivateProfiles(unittest.TestCase):
    """Cover InMemoryMetadataStore.deactivate_profiles branch logic."""

    def test_deactivate_empty_list_is_noop(self):
        store = InMemoryMetadataStore()
        store.deactivate_profiles([])
        self.assertEqual(store.load_profiles("repo-1"), ())

    def test_deactivate_matching_profile(self):
        store = InMemoryMetadataStore()
        profile = _make_profile()
        store.upsert_profiles([profile])
        deactivation = ProfileDeactivation(
            repo_id="repo-1",
            profile_kind="semantic_fact",
            relation_type="ABOUT",
            source_node_id="node-src",
            target_node_id="node-tgt",
            replacement_profile_id=None,
            valid_to_revision="rev-2",
            observed_at="2025-02-01T00:00:00Z",
        )
        store.deactivate_profiles([deactivation])
        profiles = store.load_profiles("repo-1")
        self.assertEqual(len(profiles), 1)
        self.assertFalse(profiles[0].is_active)
        self.assertEqual(profiles[0].valid_to_revision, "rev-2")

    def test_deactivate_skips_replacement_profile(self):
        store = InMemoryMetadataStore()
        p1 = _make_profile(profile_id="prof-1")
        p2 = _make_profile(profile_id="prof-2")
        store.upsert_profiles([p1, p2])
        deactivation = ProfileDeactivation(
            repo_id="repo-1",
            profile_kind="semantic_fact",
            relation_type="ABOUT",
            source_node_id="node-src",
            target_node_id="node-tgt",
            replacement_profile_id="prof-2",
            valid_to_revision="rev-2",
            observed_at="2025-02-01T00:00:00Z",
        )
        store.deactivate_profiles([deactivation])
        profiles = store.load_profiles("repo-1")
        active = [p for p in profiles if p.is_active]
        inactive = [p for p in profiles if not p.is_active]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].profile_id, "prof-2")
        self.assertEqual(len(inactive), 1)
        self.assertEqual(inactive[0].profile_id, "prof-1")

    def test_deactivate_skips_inactive_profile(self):
        store = InMemoryMetadataStore()
        profile = _make_profile(is_active=False)
        store.upsert_profiles([profile])
        deactivation = ProfileDeactivation(
            repo_id="repo-1",
            profile_kind="semantic_fact",
            relation_type="ABOUT",
            source_node_id="node-src",
            target_node_id="node-tgt",
            replacement_profile_id=None,
            valid_to_revision="rev-2",
            observed_at="2025-02-01T00:00:00Z",
        )
        store.deactivate_profiles([deactivation])
        profiles = store.load_profiles("repo-1")
        self.assertFalse(profiles[0].is_active)

    def test_deactivate_skips_mismatched_kind(self):
        store = InMemoryMetadataStore()
        profile = _make_profile(profile_kind="semantic_claim")
        store.upsert_profiles([profile])
        deactivation = ProfileDeactivation(
            repo_id="repo-1",
            profile_kind="semantic_fact",
            relation_type="ABOUT",
            source_node_id="node-src",
            target_node_id="node-tgt",
            replacement_profile_id=None,
            valid_to_revision="rev-2",
            observed_at="2025-02-01T00:00:00Z",
        )
        store.deactivate_profiles([deactivation])
        profiles = store.load_profiles("repo-1")
        self.assertTrue(profiles[0].is_active)

    def test_deactivate_nonexistent_repo_is_noop(self):
        store = InMemoryMetadataStore()
        deactivation = ProfileDeactivation(
            repo_id="nonexistent",
            profile_kind="semantic_fact",
            relation_type="ABOUT",
            source_node_id="node-src",
            target_node_id="node-tgt",
            replacement_profile_id=None,
            valid_to_revision="rev-2",
            observed_at="2025-02-01T00:00:00Z",
        )
        store.deactivate_profiles([deactivation])


class TestSQLiteMetadataStoreExtended(unittest.TestCase):
    """Cover _DbApiMetadataStore methods via SQLite."""

    def setUp(self):
        self.store = SQLiteMetadataStore(":memory:")
        self.store.bootstrap()

    def tearDown(self):
        self.store.close()

    def test_save_and_load_repo_index_state(self):
        state = RepoIndexState(
            repo_id="repo-1",
            root_path="/tmp/repo",
            latest_revision_id="rev-1",
            latest_indexed_at="2025-01-01T00:00:00Z",
            metadata={"nodes": 10},
            latest_commit_sha="abc123",
            latest_branch="main",
            last_commit_indexed_at="2025-01-01T00:00:00Z",
        )
        self.store.save_repo_index_state(state)
        loaded = self.store.load_repo_index_state("repo-1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.repo_id, "repo-1")
        self.assertEqual(loaded.latest_commit_sha, "abc123")
        self.assertEqual(loaded.latest_branch, "main")

    def test_load_repo_index_state_missing(self):
        loaded = self.store.load_repo_index_state("nonexistent")
        self.assertIsNone(loaded)

    def test_save_and_load_repo_source_state(self):
        state = RepoSourceState(
            repo_id="repo-1",
            provider="github",
            canonical_repo="owner/repo",
            clone_url="https://github.com/owner/repo.git",
            default_branch="main",
            clone_path="/tmp/clone",
            last_fetch_at="2025-01-01T00:00:00Z",
            last_fetched_head_sha="abc123",
            last_github_sync_at="2025-01-01T00:00:00Z",
            last_github_sync_status="success",
            last_github_sync_error=None,
            metadata={"key": "value"},
        )
        self.store.save_repo_source_state(state)
        loaded = self.store.load_repo_source_state("repo-1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.provider, "github")
        self.assertEqual(loaded.canonical_repo, "owner/repo")

    def test_load_repo_source_state_missing(self):
        loaded = self.store.load_repo_source_state("nonexistent")
        self.assertIsNone(loaded)

    def test_save_and_load_snapshot(self):
        snapshot = GraphSnapshot.from_dict({
            "repo_id": "repo-1",
            "revision_id": "rev-1",
            "root_path": "/tmp/repo",
            "generated_at": "2025-01-01T00:00:00Z",
            "nodes": [],
            "edges": [],
            "metadata": {"stats": True},
        })
        self.store.save_latest_snapshot(snapshot)
        loaded = self.store.load_latest_snapshot("repo-1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.repo_id, "repo-1")

    def test_load_snapshot_missing(self):
        loaded = self.store.load_latest_snapshot("nonexistent")
        self.assertIsNone(loaded)

    def test_upsert_and_load_profiles(self):
        profile = _make_profile()
        self.store.upsert_profiles([profile])
        loaded = self.store.load_profiles("repo-1")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].profile_id, "prof-1")

    def test_load_profiles_active_only(self):
        p1 = _make_profile(profile_id="prof-1", is_active=True)
        p2 = _make_profile(profile_id="prof-2", is_active=False)
        self.store.upsert_profiles([p1, p2])
        active = self.store.load_profiles("repo-1", active_only=True)
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0].is_active)

    def test_deactivate_profiles_sql(self):
        profile = _make_profile()
        self.store.upsert_profiles([profile])
        deactivation = ProfileDeactivation(
            repo_id="repo-1",
            profile_kind="semantic_fact",
            relation_type="ABOUT",
            source_node_id="node-src",
            target_node_id="node-tgt",
            replacement_profile_id=None,
            valid_to_revision="rev-2",
            observed_at="2025-02-01T00:00:00Z",
        )
        self.store.deactivate_profiles([deactivation])
        loaded = self.store.load_profiles("repo-1")
        self.assertEqual(len(loaded), 1)
        self.assertFalse(loaded[0].is_active)

    def test_record_index_run(self):
        self.store.record_index_run(
            repo_id="repo-1",
            revision_id="rev-1",
            status="success",
            stats={"nodes": 5},
        )

    def test_reset_repo_context(self):
        snapshot = GraphSnapshot.from_dict({
            "repo_id": "repo-1",
            "revision_id": "rev-1",
            "root_path": "/tmp/repo",
            "generated_at": "2025-01-01T00:00:00Z",
            "nodes": [],
            "edges": [],
            "metadata": {},
        })
        self.store.save_latest_snapshot(snapshot)
        self.store.upsert_profiles([_make_profile()])
        result = self.store.reset_repo_context("repo-1")
        self.assertIn("profiles", result)
        self.assertIn("repo_state", result)
        self.assertGreaterEqual(result["profiles"], 1)


class TestMetadataHelpers(unittest.TestCase):
    def test_stable_lock_id_deterministic(self):
        id1 = _stable_lock_id("repo-1")
        id2 = _stable_lock_id("repo-1")
        self.assertEqual(id1, id2)
        self.assertIsInstance(id1, int)

    def test_rollback_connection_with_rollback(self):
        conn = MagicMock()
        _rollback_connection(conn)
        conn.rollback.assert_called_once()

    def test_rollback_connection_without_rollback(self):
        conn = object()
        _rollback_connection(conn)  # should not raise

    def test_profile_from_row(self):
        row = (
            "prof-1", "repo-1", "semantic_fact", "claim", "claim-1",
            "ABOUT", "node-src", "File", "node-tgt", "Function",
            "rev-1", "some value", "[]", "{}", 1, "rev-0", None, "2025-01-01T00:00:00Z",
        )
        profile = _profile_from_row(row)
        self.assertEqual(profile.profile_id, "prof-1")
        self.assertTrue(profile.is_active)


class TestInMemoryProfileVectorStoreExtended(unittest.TestCase):
    """Cover InMemoryProfileVectorStore deactivation and search branches."""

    def test_deactivate_profile_keys(self):
        store = InMemoryProfileVectorStore()
        store.bootstrap()
        from context_graph.backend.profile_vectors.store import ProfileKeyRecord
        record = ProfileKeyRecord(
            profile_key_id="pk-1",
            profile_id="prof-1",
            repo_id="repo-1",
            profile_kind="semantic_claim",
            relation_type="ABOUT",
            source_node_id="node-src",
            target_node_id="node-tgt",
            key_text="test key",
            key_origin="claim_text",
            embedding=[0.1, 0.2, 0.3],
            is_active=True,
            valid_from_revision=None,
            valid_to_revision=None,
            updated_at="2025-01-01T00:00:00Z",
        )
        store.upsert_profile_keys([record])
        deactivation = ProfileDeactivation(
            repo_id="repo-1",
            profile_kind="semantic_claim",
            relation_type="ABOUT",
            source_node_id="node-src",
            target_node_id="node-tgt",
            replacement_profile_id=None,
            valid_to_revision="rev-2",
            observed_at="2025-02-01T00:00:00Z",
        )
        store.deactivate_profile_keys([deactivation])
        results = store.search_profile_keys(
            repo_id="repo-1",
            embedding=[0.1, 0.2, 0.3],
            limit=10,
            active_only=True,
        )
        self.assertEqual(len(results), 0)

    def test_search_inactive_included(self):
        store = InMemoryProfileVectorStore()
        store.bootstrap()
        from context_graph.backend.profile_vectors.store import ProfileKeyRecord
        record = ProfileKeyRecord(
            profile_key_id="pk-1",
            profile_id="prof-1",
            repo_id="repo-1",
            profile_kind="semantic_claim",
            relation_type=None,
            source_node_id=None,
            target_node_id=None,
            key_text="test key",
            key_origin="claim_text",
            embedding=[0.1, 0.2, 0.3],
            is_active=False,
            valid_from_revision=None,
            valid_to_revision=None,
            updated_at="2025-01-01T00:00:00Z",
        )
        store.upsert_profile_keys([record])
        results = store.search_profile_keys(
            repo_id="repo-1",
            embedding=[0.1, 0.2, 0.3],
            limit=10,
            active_only=False,
        )
        self.assertEqual(len(results), 1)

    def test_reset_repo_context(self):
        store = InMemoryProfileVectorStore()
        store.bootstrap()
        from context_graph.backend.profile_vectors.store import ProfileKeyRecord
        record = ProfileKeyRecord(
            profile_key_id="pk-1",
            profile_id="prof-1",
            repo_id="repo-1",
            profile_kind="semantic_claim",
            relation_type="ABOUT",
            source_node_id="node-src",
            target_node_id="node-tgt",
            key_text="test key",
            key_origin="claim_text",
            embedding=[0.1, 0.2, 0.3],
            is_active=True,
            valid_from_revision=None,
            valid_to_revision=None,
            updated_at="2025-01-01T00:00:00Z",
        )
        store.upsert_profile_keys([record])
        count = store.reset_repo_context("repo-1")
        self.assertEqual(count, 1)

    def test_search_with_profile_kinds_filter(self):
        store = InMemoryProfileVectorStore()
        store.bootstrap()
        from context_graph.backend.profile_vectors.store import ProfileKeyRecord
        record = ProfileKeyRecord(
            profile_key_id="pk-1",
            profile_id="prof-1",
            repo_id="repo-1",
            profile_kind="semantic_claim",
            relation_type="ABOUT",
            source_node_id="node-src",
            target_node_id="node-tgt",
            key_text="test key",
            key_origin="claim_text",
            embedding=[1.0, 0.0, 0.0],
            is_active=True,
            valid_from_revision=None,
            valid_to_revision=None,
            updated_at="2025-01-01T00:00:00Z",
        )
        store.upsert_profile_keys([record])
        results = store.search_profile_keys(
            repo_id="repo-1",
            embedding=[1.0, 0.0, 0.0],
            limit=10,
            active_only=True,
            profile_kinds=("semantic_claim",),
        )
        self.assertEqual(len(results), 1)
        results_filtered = store.search_profile_keys(
            repo_id="repo-1",
            embedding=[1.0, 0.0, 0.0],
            limit=10,
            active_only=True,
            profile_kinds=("other_kind",),
        )
        self.assertEqual(len(results_filtered), 0)


class TestVectorHelpers(unittest.TestCase):
    def test_vector_literal(self):
        result = _vector_literal([0.1, 0.2, 0.3])
        self.assertIn("0.1", result)
        self.assertTrue(result.startswith("["))

    def test_cosine_similarity(self):
        sim = _cosine_similarity([1.0, 0.0], [1.0, 0.0])
        self.assertAlmostEqual(sim, 1.0)
        sim2 = _cosine_similarity([1.0, 0.0], [0.0, 1.0])
        self.assertAlmostEqual(sim2, 0.0)


if __name__ == "__main__":
    unittest.main()
