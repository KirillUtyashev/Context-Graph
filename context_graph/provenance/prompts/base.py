"""Shared prompt metadata for provenance extraction pipelines."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from context_graph.prompts.models import Message


@dataclass(frozen=True, slots=True)
class ProvenancePromptMetadata:
    """Stable prompt metadata used for tracing and token accounting."""

    name: str
    version: str

    @property
    def prompt_name(self) -> str:
        return f"{self.name}.{self.version}"


@dataclass(frozen=True, slots=True)
class ProvenancePromptRequest:
    """One future LLM request prepared by a provenance extraction service."""

    metadata: ProvenancePromptMetadata
    messages: tuple[Message, ...]
    response_model: type[BaseModel]

    @property
    def prompt_name(self) -> str:
        return self.metadata.prompt_name
