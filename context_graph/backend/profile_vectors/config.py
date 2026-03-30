"""Configuration objects for profile-vector backend stores."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InMemoryProfileVectorConfig:
    """Create a process-local in-memory profile-vector store."""


@dataclass(frozen=True, slots=True)
class PgVectorProfileVectorConfig:
    """Create a PostgreSQL/pgvector-backed profile-vector store."""

    embedding_dim: int = 1024
