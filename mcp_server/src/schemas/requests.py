"""Request schema types for MCP tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ObservationWriteRequest:
    """One scoped agent observation write request."""

    repo_id: str
    group_id: str
    observation_text: str
    observation_kind: str
    title: str | None = None
    source_kind: str = "agent"
    source_id: str | None = None
    context_kind: str | None = None
    context_id: str | None = None
    observed_at: str | None = None
    task_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
