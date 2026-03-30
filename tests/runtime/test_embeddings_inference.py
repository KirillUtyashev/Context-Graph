from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from context_graph.cli import _make_profile_indexer
from context_graph.runtime import embeddings
from context_graph.backend import InMemoryMetadataStore


class EmbeddingInferenceTests(unittest.TestCase):
    def test_load_embedding_settings_infers_openai_from_llm_provider(self) -> None:
        args = SimpleNamespace(
            embed_provider=None,
            llm_provider="openai",
            embed_model=None,
            embed_dimensions=None,
            embed_base_url=None,
            embed_api_key=None,
            aws_region=None,
        )

        with patch.dict("os.environ", {"OPENAI_API_KEY": "secret"}, clear=True):
            settings = embeddings.load_embedding_settings(args)

        self.assertIsNotNone(settings)
        self.assertEqual(settings.provider, "openai")
        self.assertEqual(settings.api_key, "secret")

    def test_make_profile_indexer_uses_inferred_embedding_provider(self) -> None:
        args = SimpleNamespace(
            embed_provider=None,
            llm_provider="openai",
            embed_model=None,
            embed_dimensions=None,
            embed_base_url=None,
            embed_api_key=None,
            aws_region=None,
        )
        metadata = InMemoryMetadataStore()
        metadata.bootstrap()

        with patch.dict("os.environ", {"OPENAI_API_KEY": "secret"}, clear=True):
            indexer = _make_profile_indexer(args, metadata=metadata)

        self.assertIsNotNone(indexer)
        self.assertEqual(type(indexer).__name__, "ProfileVectorIndexer")


if __name__ == "__main__":
    unittest.main()
