"""Configuration objects for the Neo4j backend implementation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Neo4jConfig:
    """Connection settings for a Neo4j backend."""

    uri: str
    user: str | None
    password: str | None
    database: str = "neo4j"
