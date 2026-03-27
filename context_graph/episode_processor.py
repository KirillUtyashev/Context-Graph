from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

from .memory.models.nodes import EntityNode, EpisodeType, EpisodicNode
from .memory.extraction.node_operations import extract_nodes as _graphiti_extract_nodes
from .memory.datetime_utils import utc_now

from .episodes import (
    EpisodeProcessingResult,
    MentionRecord,
    entity_node_kind,
)
from .graph_read import GraphReadRepository
from .legacy_graph_backend import BaseCypherGraphSink, CypherStatement


@dataclass(frozen=True, slots=True)
class EpisodeWorkItem:
    episode: EpisodicNode
    extra_nodes: tuple[EntityNode, ...] = ()


@dataclass(frozen=True, slots=True)
class EpisodeBatchResult:
    results: tuple[EpisodeProcessingResult, ...]
    statements: tuple[CypherStatement, ...]
    statements_executed: int


class EpisodeProcessor:
    """Process episodes into stored episode nodes plus mention links only.

    The canonical ingestion pipeline no longer turns text into generic
    ``ContextNode`` records or ``RELATES_TO`` facts. This processor therefore
    resolves mentions against existing graph entities and optionally uses LLM
    extraction only as a hinting step for mention resolution.
    """

    def __init__(
        self,
        *,
        graph_sink: BaseCypherGraphSink,
        read_repository: GraphReadRepository,
        llm_client: Any | None = None,
        entity_types: dict[str, Any] | None = None,
        edge_types: dict[str, Any] | None = None,
        edge_type_map: dict[tuple[str, str], list[str]] | None = None,
        custom_extraction_instructions: str | None = None,
        profile_indexer: Any | None = None,
    ) -> None:
        self.graph_sink = graph_sink
        self.read_repository = read_repository
        self.llm_client = llm_client
        self.entity_types = entity_types
        self.edge_types = edge_types or {}
        self.edge_type_map = edge_type_map or {}
        self.custom_extraction_instructions = custom_extraction_instructions
        self.profile_indexer = profile_indexer

    # ------------------------------------------------------------------
    # LLM extraction helpers (sync wrappers around Graphiti's async API)
    # ------------------------------------------------------------------

    def _extract_nodes(
        self,
        episode: EpisodicNode,
        previous_episodes: list[EpisodicNode],
    ) -> list[EntityNode]:
        """Extract entity names from the episode using Graphiti's native LLM extraction."""
        if self.llm_client is None:
            return []
        clients = _build_minimal_clients(self.llm_client)
        return _run_async(
            _graphiti_extract_nodes(
                clients,
                episode,
                previous_episodes,
                entity_types=self.entity_types,
                custom_extraction_instructions=self.custom_extraction_instructions,
            )
        )

    def plan_episode(
        self,
        episode: EpisodicNode,
        *,
        extra_nodes: Sequence[EntityNode] = (),
        previous_episode_texts: Sequence[str] = (),
    ) -> EpisodeProcessingResult:
        deterministic_nodes = self.read_repository.resolve_candidates(
            repo_id=episode.group_id,
            text=episode.content,
            extra_nodes=extra_nodes,
        )
        mention_sources: dict[str, set[str]] = defaultdict(set)
        matched_terms: dict[str, set[str]] = defaultdict(set)

        for node in extra_nodes:
            mention_sources[node.uuid].add("context")
        for node in deterministic_nodes:
            mention_sources[node.uuid].add("deterministic")

        previous_episodes = (
            _texts_to_episodes(episode.group_id, previous_episode_texts)
            if previous_episode_texts
            else []
        )
        llm_extracted_nodes = self._extract_nodes(episode, previous_episodes)

        llm_resolved_nodes: list[EntityNode] = []
        for llm_node in llm_extracted_nodes:
            name = llm_node.name
            resolved = self.read_repository.resolve_candidates(
                repo_id=episode.group_id,
                text=name,
                extra_nodes=extra_nodes,
                limit=5,
            )
            for node in resolved:
                llm_resolved_nodes.append(node)
                mention_sources[node.uuid].add("llm")
                matched_terms[node.uuid].add(name)

        candidate_nodes = list(_dedupe_nodes([*extra_nodes, *deterministic_nodes, *llm_resolved_nodes]))
        mention_records = tuple(
            MentionRecord(
                episode_node_id=episode.uuid,
                target_node_id=node.uuid,
                repo_id=episode.group_id,
                metadata=_build_mention_metadata(
                    node=node,
                    sources=mention_sources.get(node.uuid),
                    matched_terms=matched_terms.get(node.uuid),
                ),
            )
            for node in _dedupe_nodes(candidate_nodes)
        )

        statements = [
            *self.graph_sink.plan_upsert_episodes([episode]),
        ]
        if episode.parent_node_id:
            statements.extend(
                self.graph_sink.plan_attach_episode_parent(
                    repo_id=episode.group_id,
                    episode_node_id=episode.uuid,
                    target_node_id=episode.parent_node_id,
                )
            )
        if mention_records:
            statements.extend(self.graph_sink.plan_attach_mentions(mention_records))

        return EpisodeProcessingResult(
            episode=episode,
            mentions=mention_records,
            facts=(),
            invalidations=(),
            profiles=(),
            profile_deactivations=(),
            statements=tuple(statements),
            created_context_nodes=(),
        )

    def apply_batch(
        self,
        items: Sequence[EpisodeWorkItem],
        *,
        execute: bool = False,
    ) -> EpisodeBatchResult:
        history: dict[str, list[str]] = {}
        results: list[EpisodeProcessingResult] = []
        statements: list[CypherStatement] = []

        for item in items:
            thread_key = _thread_key(item.episode)
            result = self.plan_episode(
                item.episode,
                extra_nodes=item.extra_nodes,
                previous_episode_texts=history.get(thread_key, ()),
            )
            results.append(result)
            statements.extend(result.statements)
            history.setdefault(thread_key, []).append(item.episode.content)

        executed = self.graph_sink.execute(statements) if execute and statements else 0
        return EpisodeBatchResult(
            results=tuple(results),
            statements=tuple(statements),
            statements_executed=executed,
        )


# ======================================================================
# Private helpers
# ======================================================================


def _build_minimal_clients(llm_client: Any) -> Any:
    """Build a minimal GraphitiClients-like object for extraction-only calls.

    Graphiti's ``extract_nodes`` and ``extract_edges`` only access
    ``clients.llm_client``, so we use a lightweight namespace instead of
    requiring a full ``GraphitiClients`` (which needs driver, embedder, etc.).
    """
    class _MinimalClients:
        __slots__ = ("llm_client",)
        def __init__(self, llm_client: Any) -> None:
            self.llm_client = llm_client
    return _MinimalClients(llm_client)


def _run_async(coro: Any) -> Any:
    """Run an async coroutine from sync code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _texts_to_episodes(group_id: str, texts: Sequence[str]) -> list[EpisodicNode]:
    """Convert plain text strings to minimal EpisodicNode objects for Graphiti's API."""
    return [
        EpisodicNode(
            name="previous_context",
            group_id=group_id,
            content=text,
            source=EpisodeType.text,
            source_description="previous_context",
            valid_at=utc_now(),
        )
        for text in texts
    ]


def _thread_key(episode: EpisodicNode) -> str:
    if episode.parent_node_id:
        return f"parent:{episode.parent_node_id}"
    thread_id = episode.episode_metadata.get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        return f"thread:{thread_id}"
    return f"episode-source:{episode.source.value}:{episode.source_description}"


def _dedupe_nodes(nodes: Iterable[EntityNode]) -> tuple[EntityNode, ...]:
    deduped: dict[str, EntityNode] = {}
    for node in nodes:
        deduped.setdefault(node.uuid, node)
    return tuple(deduped.values())


def _build_mention_metadata(
    *,
    node: EntityNode,
    sources: set[str] | None,
    matched_terms: set[str] | None,
) -> Mapping[str, object]:
    metadata: dict[str, object] = {"target_kind": entity_node_kind(node)}
    if sources:
        metadata["match_sources"] = sorted(sources)
    if matched_terms:
        metadata["matched_terms"] = sorted(matched_terms)
    return metadata
