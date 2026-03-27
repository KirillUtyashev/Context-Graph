"""Shared record and hit types for profile-vector stores."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProfileKeyRecord:
    """One searchable key phrase associated with a profile."""

    profile_key_id: str
    profile_id: str
    repo_id: str
    profile_kind: str
    relation_type: str | None
    source_node_id: str | None
    target_node_id: str | None
    key_text: str
    embedding: tuple[float, ...]
    is_active: bool
    valid_from_revision: str | None
    valid_to_revision: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class ProfileVectorHit:
    """One scored vector hit for a profile key."""

    profile_id: str
    profile_key_id: str
    repo_id: str
    profile_kind: str
    key_text: str
    score: float
