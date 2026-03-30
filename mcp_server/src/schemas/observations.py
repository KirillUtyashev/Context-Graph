"""Schema types for observation-writing MCP tools."""

from __future__ import annotations

from .common import ToolScaffoldResponse


class RecordAgentObservationResponse(ToolScaffoldResponse):
    repo_id: str
    observation_id: str | None
    task_id: str | None
    claim_ids: list[str]
    accepted: bool
    persisted: bool
