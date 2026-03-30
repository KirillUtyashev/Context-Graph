"""Common schema types shared across MCP tool modules."""

from __future__ import annotations

from typing import Literal

from typing_extensions import NotRequired, TypedDict


class ErrorResponse(TypedDict):
    error: str


ToolImplementationStatus = Literal["scaffold", "implemented", "unsupported"]


class ToolScaffoldResponse(TypedDict):
    status: ToolImplementationStatus
    message: str
    capability: NotRequired[str]
    todo: NotRequired[str]
