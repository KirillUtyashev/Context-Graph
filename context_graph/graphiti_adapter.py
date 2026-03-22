from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence

from .episodes import (
    ContextNodeRef,
    EpisodeExtractionResult,
    EpisodeRecord,
    FactInvalidation,
    FactRecord,
    fact_edge_id,
)

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover - exercised implicitly by unit tests without pydantic
    BaseModel = None
    Field = None


@dataclass(slots=True)
class PromptMessage:
    role: str
    content: str

    def model_dump(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


if BaseModel is not None:

    class ExtractedEntity(BaseModel):
        name: str = Field(..., description="Name of the extracted entity")
        entity_type_id: int = Field(
            default=0,
            description="Classified entity type id. Defaults to the generic Entity type.",
        )


    class ExtractedEntities(BaseModel):
        extracted_entities: list[ExtractedEntity]


    class Edge(BaseModel):
        source_entity_name: str = Field(..., description="Name of the source entity")
        target_entity_name: str = Field(..., description="Name of the target entity")
        relation_type: str = Field(..., description="SCREAMING_SNAKE_CASE relation type")
        fact: str = Field(..., description="Natural language fact paraphrase")
        valid_at: str | None = Field(None, description="When the fact became true")
        invalid_at: str | None = Field(None, description="When the fact stopped being true")


    class ExtractedEdges(BaseModel):
        edges: list[Edge]

else:

    class ExtractedEntities:
        @classmethod
        def model_json_schema(cls) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "extracted_entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "entity_type_id": {"type": "integer"},
                            },
                            "required": ["name", "entity_type_id"],
                        },
                    }
                },
                "required": ["extracted_entities"],
            }


    class ExtractedEdges:
        @classmethod
        def model_json_schema(cls) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "edges": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_entity_name": {"type": "string"},
                                "target_entity_name": {"type": "string"},
                                "relation_type": {"type": "string"},
                                "fact": {"type": "string"},
                                "valid_at": {"type": ["string", "null"]},
                                "invalid_at": {"type": ["string", "null"]},
                            },
                            "required": [
                                "source_entity_name",
                                "target_entity_name",
                                "relation_type",
                                "fact",
                            ],
                        },
                    }
                },
                "required": ["edges"],
            }


@dataclass(slots=True)
class GraphitiAdapter:
    llm_client: Any | None = None
    edge_type_map: Mapping[tuple[str, str], list[str]] | None = None
    edge_types: Mapping[str, type[Any]] | None = None
    custom_extraction_instructions: str | None = None

    def extract_entity_names(
        self,
        *,
        episode: EpisodeRecord,
        previous_episode_texts: Sequence[str] = (),
    ) -> tuple[str, ...]:
        if self.llm_client is None:
            return ()

        context = {
            "episode_content": episode.text,
            "episode_timestamp": episode.updated_at or episode.created_at,
            "previous_episodes": list(previous_episode_texts),
            "custom_extraction_instructions": self.custom_extraction_instructions or "",
            "entity_types": [
                {
                    "entity_type_id": 0,
                    "entity_type_name": "Entity",
                    "entity_type_description": "Default entity classification.",
                }
            ],
            "source_description": episode.source_type,
        }

        response = _run_async(
            self.llm_client.generate_response(
                _entity_prompt_for_episode(episode, context),
                response_model=ExtractedEntities,
                max_tokens=16384,
                group_id=episode.repo_id,
                prompt_name="context_graph.graphiti.extract_nodes",
            )
        )
        extracted_entities = _extract_entity_rows(response)
        names: list[str] = []
        seen: set[str] = set()
        for entity in extracted_entities:
            name = str(entity.get("name", "")).strip()
            normalized = name.casefold()
            if not name or normalized in seen:
                continue
            seen.add(normalized)
            names.append(name)
        return tuple(names)

    def extract_facts(
        self,
        *,
        episode: EpisodeRecord,
        candidate_nodes: Sequence[ContextNodeRef],
        previous_episode_texts: Sequence[str] = (),
    ) -> EpisodeExtractionResult:
        mentions = tuple(self._dedupe_nodes(candidate_nodes))
        if self.llm_client is None or len(mentions) < 2:
            return EpisodeExtractionResult(
                mentions=mentions,
                mention_metadata={},
                facts=(),
                invalidations=(),
            )

        extracted = self._extract_facts_with_graphiti(
            episode=episode,
            candidate_nodes=mentions,
            previous_episode_texts=previous_episode_texts,
        )
        facts = tuple(extracted)
        invalidations = tuple(
            FactInvalidation(
                repo_id=fact.repo_id,
                source_node_id=fact.source_node_id,
                target_node_id=fact.target_node_id,
                relation_type=fact.relation_type,
                observed_at=episode.updated_at or episode.created_at,
                replacement_fact_id=fact.fact_id,
                replacement_valid_at=fact.valid_at,
            )
            for fact in facts
        )
        return EpisodeExtractionResult(
            mentions=mentions,
            mention_metadata={},
            facts=facts,
            invalidations=invalidations,
        )

    def _extract_facts_with_graphiti(
        self,
        *,
        episode: EpisodeRecord,
        candidate_nodes: Sequence[ContextNodeRef],
        previous_episode_texts: Sequence[str],
    ) -> list[FactRecord]:
        edge_type_map = self.edge_type_map or {}
        edge_types = self.edge_types or {}

        edge_type_signatures_map: dict[str, list[tuple[str, str]]] = {}
        for signature, edge_type_names in edge_type_map.items():
            for edge_type_name in edge_type_names:
                edge_type_signatures_map.setdefault(edge_type_name, []).append(signature)

        context = {
            "episode_content": episode.text,
            "nodes": [
                {"name": node.name, "entity_types": [node.kind]}
                for node in candidate_nodes
            ],
            "previous_episodes": list(previous_episode_texts),
            "reference_time": episode.updated_at or episode.created_at,
            "edge_types": [
                {
                    "fact_type_name": type_name,
                    "fact_type_signatures": edge_type_signatures_map.get(
                        type_name,
                        [("Entity", "Entity")],
                    ),
                    "fact_type_description": getattr(type_model, "__doc__", ""),
                }
                for type_name, type_model in edge_types.items()
            ],
            "custom_extraction_instructions": self.custom_extraction_instructions or "",
        }

        response = _run_async(
            self.llm_client.generate_response(
                _edge_prompt(context),
                response_model=ExtractedEdges,
                max_tokens=16384,
                group_id=episode.repo_id,
                prompt_name="context_graph.graphiti.extract_edges",
            )
        )
        extracted_edges = _extract_edge_rows(response)
        by_name: dict[str, ContextNodeRef] = {}
        for node in candidate_nodes:
            by_name.setdefault(node.name, node)

        facts: list[FactRecord] = []
        for edge in extracted_edges:
            fact_text = str(edge.get("fact", "")).strip()
            if not fact_text:
                continue
            source = by_name.get(str(edge.get("source_entity_name", "")))
            target = by_name.get(str(edge.get("target_entity_name", "")))
            if source is None or target is None or source.node_id == target.node_id:
                continue
            facts.append(
                FactRecord(
                    fact_id=fact_edge_id(
                        repo_id=episode.repo_id,
                        source_node_id=source.node_id,
                        relation_type=str(edge.get("relation_type", "")),
                        target_node_id=target.node_id,
                        fact=fact_text,
                    ),
                    repo_id=episode.repo_id,
                    source_node_id=source.node_id,
                    target_node_id=target.node_id,
                    relation_type=str(edge.get("relation_type", "")),
                    fact=fact_text,
                    episode_ids=(episode.episode_id,),
                    created_at=episode.updated_at or episode.created_at,
                    valid_at=_string_or_none(edge.get("valid_at")),
                    invalid_at=_string_or_none(edge.get("invalid_at")),
                    metadata={
                        "source_kind": source.kind,
                        "target_kind": target.kind,
                    },
                )
            )
        return facts

    def _dedupe_nodes(self, nodes: Sequence[ContextNodeRef]) -> list[ContextNodeRef]:
        deduped: dict[str, ContextNodeRef] = {}
        for node in nodes:
            deduped.setdefault(node.node_id, node)
        return list(deduped.values())


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _entity_prompt_for_episode(episode: EpisodeRecord, context: dict[str, Any]) -> list[PromptMessage]:
    source_type = episode.source_type.lower()
    if source_type in {"user_message", "agent_message"}:
        return _entity_message_prompt(context)
    return _entity_text_prompt(context)


def _entity_message_prompt(context: dict[str, Any]) -> list[PromptMessage]:
    return [
        PromptMessage(
            role="system",
            content=(
                "You are an AI assistant that extracts entity nodes from conversational messages. "
                "Extract distinct entities explicitly or implicitly mentioned in the current message."
            ),
        ),
        PromptMessage(
            role="user",
            content=(
                "<ENTITY TYPES>\n"
                f"{_prompt_json(context['entity_types'])}\n"
                "</ENTITY TYPES>\n\n"
                "<PREVIOUS MESSAGES>\n"
                f"{_prompt_json(list(context['previous_episodes']))}\n"
                "</PREVIOUS MESSAGES>\n\n"
                "<CURRENT MESSAGE>\n"
                f"{context['episode_content']}\n"
                "</CURRENT MESSAGE>\n\n"
                "Extract distinct entities from the CURRENT MESSAGE only. "
                "Resolve obvious pronouns to named entities when possible. "
                "Do not extract relationships, actions, or dates as entities.\n\n"
                f"{context['custom_extraction_instructions']}"
            ),
        ),
    ]


def _entity_text_prompt(context: dict[str, Any]) -> list[PromptMessage]:
    return [
        PromptMessage(
            role="system",
            content=(
                "You are an AI assistant that extracts entity nodes from text. "
                "Extract significant entities, concepts, or actors mentioned in the text."
            ),
        ),
        PromptMessage(
            role="user",
            content=(
                "<ENTITY TYPES>\n"
                f"{_prompt_json(context['entity_types'])}\n"
                "</ENTITY TYPES>\n\n"
                "<TEXT>\n"
                f"{context['episode_content']}\n"
                "</TEXT>\n\n"
                "Extract entities explicitly or implicitly mentioned in the TEXT. "
                "Do not extract relationships, actions, or temporal information as entities. "
                "Use explicit names where possible.\n\n"
                f"{context['custom_extraction_instructions']}"
            ),
        ),
    ]


def _edge_prompt(context: dict[str, Any]) -> list[PromptMessage]:
    edge_types_section = ""
    if context.get("edge_types"):
        edge_types_section = (
            "<FACT_TYPES>\n"
            f"{_prompt_json(context['edge_types'])}\n"
            "</FACT_TYPES>\n\n"
        )

    return [
        PromptMessage(
            role="system",
            content=(
                "You are an expert fact extractor that extracts fact triples from text. "
                "Use the current message as the primary source and attach temporal information when stated."
            ),
        ),
        PromptMessage(
            role="user",
            content=(
                "<PREVIOUS_MESSAGES>\n"
                f"{_prompt_json(list(context['previous_episodes']))}\n"
                "</PREVIOUS_MESSAGES>\n\n"
                "<CURRENT_MESSAGE>\n"
                f"{context['episode_content']}\n"
                "</CURRENT_MESSAGE>\n\n"
                "<ENTITIES>\n"
                f"{_prompt_json(context['nodes'])}\n"
                "</ENTITIES>\n\n"
                "<REFERENCE_TIME>\n"
                f"{context['reference_time']}\n"
                "</REFERENCE_TIME>\n\n"
                f"{edge_types_section}"
                "Extract factual relationships only between distinct entities from the ENTITIES list. "
                "Use source_entity_name and target_entity_name exactly as they appear in the list. "
                "Do not emit duplicate facts.\n\n"
                f"{context['custom_extraction_instructions']}"
            ),
        ),
    ]


def _extract_entity_rows(response: Any) -> list[dict[str, Any]]:
    if hasattr(response, "model_dump"):
        response = response.model_dump()
    if isinstance(response, dict):
        raw_entities = response.get("extracted_entities", [])
        if isinstance(raw_entities, list):
            return [dict(item) for item in raw_entities if isinstance(item, dict)]
    return []


def _extract_edge_rows(response: Any) -> list[dict[str, Any]]:
    if hasattr(response, "model_dump"):
        response = response.model_dump()
    if isinstance(response, dict):
        raw_edges = response.get("edges", [])
        if isinstance(raw_edges, list):
            return [dict(item) for item in raw_edges if isinstance(item, dict)]
    return []


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    string_value = str(value).strip()
    return string_value or None


def _prompt_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)
