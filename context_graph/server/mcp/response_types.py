"""Response type definitions for Graphiti MCP Server."""

from typing import Any

from typing_extensions import TypedDict


class ErrorResponse(TypedDict):
    error: str


class SuccessResponse(TypedDict):
    message: str


class NodeResult(TypedDict):
    uuid: str
    name: str
    labels: list[str]
    created_at: str | None
    summary: str | None
    group_id: str
    attributes: dict[str, Any]


class NodeSearchResponse(TypedDict):
    message: str
    nodes: list[NodeResult]


class FactSearchResponse(TypedDict):
    message: str
    facts: list[dict[str, Any]]


class EpisodeSearchResponse(TypedDict):
    message: str
    episodes: list[dict[str, Any]]


class StatusResponse(TypedDict):
    status: str
    message: str


class ProfileResult(TypedDict):
    profile_id: str
    relation_type: str | None
    value_text: str
    source_node_id: str | None
    target_node_id: str | None
    is_active: bool
    score: float
    metadata: dict[str, Any]


class SearchMemoryResponse(TypedDict):
    message: str
    facts: list[dict[str, Any]]
    profiles: list[ProfileResult]
