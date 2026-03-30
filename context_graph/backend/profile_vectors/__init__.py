"""Canonical profile-vector backend exports."""

from .config import InMemoryProfileVectorConfig, PgVectorProfileVectorConfig
from .factory import create_profile_vector_store, create_profile_vector_store_from_metadata
from .store import (
    AuroraProfileVectorStore,
    InMemoryProfileVectorStore,
    PgVectorProfileVectorStore,
    ProfileVectorStore,
)
from .types import ProfileKeyRecord, ProfileVectorHit

__all__ = [
    "AuroraProfileVectorStore",
    "InMemoryProfileVectorConfig",
    "InMemoryProfileVectorStore",
    "PgVectorProfileVectorConfig",
    "PgVectorProfileVectorStore",
    "ProfileKeyRecord",
    "ProfileVectorHit",
    "ProfileVectorStore",
    "create_profile_vector_store",
    "create_profile_vector_store_from_metadata",
]
