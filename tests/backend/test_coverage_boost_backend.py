"""Tests to boost coverage for Postgres/PgVector backend stores."""

import unittest
from unittest.mock import MagicMock
from context_graph.backend.profile_vectors.store import PgVectorProfileVectorStore
from context_graph.backend.profile_vectors.types import ProfileKeyRecord
from context_graph.models.profiles import ProfileDeactivation

class TestPgVectorStore(unittest.TestCase):
    def setUp(self):
        self.mock_conn = MagicMock()
        self.mock_cursor = MagicMock()
        self.mock_conn.cursor.return_value = self.mock_cursor
        self.store = PgVectorProfileVectorStore(self.mock_conn, embedding_dim=1024)

    def test_from_metadata_store_invalid(self):
        with self.assertRaises(RuntimeError):
            # Pass a mock that is not PostgresMetadataStore
            PgVectorProfileVectorStore.from_metadata_store(MagicMock())

    def test_from_metadata_store_valid(self):
        from context_graph.backend.metadata import PostgresMetadataStore
        meta = MagicMock(spec=PostgresMetadataStore)
        meta.connection = MagicMock()
        store = PgVectorProfileVectorStore.from_metadata_store(meta)
        self.assertIsNotNone(store)

    def test_bootstrap_rebuilds(self):
        # mock _current_vector_dimension returning 512, which triggers drop
        self.mock_cursor.fetchone.side_effect = [
            ("cg_profile_keys",), # to_regclass
            (512,) # atttypmod
        ]
        self.store.bootstrap()
        self.assertTrue(self.store.needs_resync)
        self.store.mark_resynced()
        self.assertFalse(self.store.needs_resync)

    def test_bootstrap_exception(self):
        self.mock_cursor.execute.side_effect = Exception("DB Error")
        with self.assertRaises(Exception):
            self.store.bootstrap()
        self.mock_conn.rollback.assert_called_once()

    def test_upsert_profile_keys(self):
        rec = ProfileKeyRecord(
            profile_key_id="pk-1",
            profile_id="prof-1",
            repo_id="repo-1",
            profile_kind="semantic_claim",
            relation_type="ABOUT",
            source_node_id="src",
            target_node_id="tgt",
            key_text="key",
            key_origin="test",
            embedding=[0.0] * 1024,
            is_active=True,
            valid_from_revision=None,
            valid_to_revision=None,
            updated_at="2025-01-01T00:00:00Z",
        )
        self.store.upsert_profile_keys([]) # no-op
        
        self.mock_cursor.fetchone.side_effect = [("cg_profile_keys",), (1024,)]
        self.store.upsert_profile_keys([rec])
        self.mock_cursor.executemany.assert_called_once()

    def test_upsert_exception(self):
        rec = ProfileKeyRecord(
            profile_key_id="pk-1",
            profile_id="prof-1",
            repo_id="repo-1",
            profile_kind="semantic_claim",
            relation_type="ABOUT",
            source_node_id="src",
            target_node_id="tgt",
            key_text="key",
            key_origin="test",
            embedding=[0.0] * 1024,
            is_active=True,
            valid_from_revision=None,
            valid_to_revision=None,
            updated_at="2025-01-01T00:00:00Z",
        )
        self.mock_cursor.fetchone.side_effect = [("cg_profile_keys",), (1024,)]
        self.mock_cursor.executemany.side_effect = Exception("error")
        with self.assertRaises(Exception):
            self.store.upsert_profile_keys([rec])
        self.mock_conn.rollback.assert_called_once()

    def test_deactivate_profile_keys(self):
        self.store.deactivate_profile_keys([]) # no-op
        deac = ProfileDeactivation(
            repo_id="repo-1",
            profile_kind="semantic_claim",
            relation_type="ABOUT",
            source_node_id="src",
            target_node_id="tgt",
            replacement_profile_id=None,
            valid_to_revision="rev-2",
            observed_at="2025-01-01T00:00:00Z",
        )
        self.mock_cursor.fetchone.side_effect = [("cg_profile_keys",), (1024,)]
        self.store.deactivate_profile_keys([deac])
        self.mock_cursor.executemany.assert_called_once()
        
    def test_deactivate_exception(self):
        deac = ProfileDeactivation(
            repo_id="repo-1",
            profile_kind="semantic_claim",
            relation_type="ABOUT",
            source_node_id="src",
            target_node_id="tgt",
            replacement_profile_id=None,
            valid_to_revision="rev-2",
            observed_at="2025-01-01T00:00:00Z",
        )
        self.mock_cursor.fetchone.side_effect = [("cg_profile_keys",), (1024,)]
        self.mock_cursor.executemany.side_effect = Exception("foo")
        with self.assertRaises(Exception):
            self.store.deactivate_profile_keys([deac])
        self.mock_conn.rollback.assert_called_once()

    def test_search_profile_keys(self):
        self.mock_cursor.fetchone.side_effect = [("cg_profile_keys",), (1024,)]
        self.mock_cursor.fetchall.return_value = [
            ("prof-1", "pk-1", "repo-1", "semantic_claim", "key", "test", 0.9)
        ]
        res = self.store.search_profile_keys(
            repo_id="repo-1",
            embedding=[0.0] * 1024,
            limit=10,
        )
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].profile_id, "prof-1")

    def test_search_exception(self):
        self.mock_cursor.fetchone.side_effect = [("cg_profile_keys",), (1024,)]
        self.mock_cursor.fetchall.side_effect = Exception("err")
        with self.assertRaises(Exception):
            self.store.search_profile_keys(repo_id="repo-1", embedding=[0.0] * 1024, limit=10)

    def test_reset_repo_context(self):
        self.mock_cursor.fetchone.side_effect = [("cg_profile_keys",), (1024,), (5,)] # 5 is the count
        res = self.store.reset_repo_context("repo-1")
        self.assertEqual(res, 5)

    def test_reset_exception(self):
        self.mock_cursor.fetchone.side_effect = [("cg_profile_keys",), (1024,)]
        self.mock_cursor.execute.side_effect = [None, Exception("err")] # second execute fails
        with self.assertRaises(Exception):
            self.store.reset_repo_context("repo-1")

if __name__ == "__main__":
    unittest.main()
