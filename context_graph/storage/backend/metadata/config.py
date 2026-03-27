"""Configuration objects for backend metadata stores."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InMemoryMetadataConfig:
    """Create a process-local in-memory metadata store."""


@dataclass(frozen=True, slots=True)
class SQLiteMetadataConfig:
    """Create a SQLite-backed metadata store."""

    path: str


@dataclass(frozen=True, slots=True)
class PostgresMetadataConfig:
    """Create a PostgreSQL-backed metadata store from a DSN."""

    dsn: str
