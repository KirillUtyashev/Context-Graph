"""
No-op cross-encoder for deployments without an OpenAI key.

Returns passages in their original order with equal scores (1.0).
Semantic + graph search still work normally; only result re-ranking is skipped.
"""

from context_graph.runtime.rerank.client import CrossEncoderClient


class NoOpCrossEncoder(CrossEncoderClient):
    """Identity cross-encoder that returns passages in original order."""

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        return [(p, 1.0) for p in passages]
