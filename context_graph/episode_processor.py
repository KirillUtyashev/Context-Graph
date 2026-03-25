from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.utils.maintenance.edge_operations import extract_edges as _graphiti_extract_edges
from graphiti_core.utils.maintenance.node_operations import extract_nodes as _graphiti_extract_nodes

from .episodes import (
    EpisodeProcessingResult,
    FactInvalidation,
    MentionRecord,
    entity_node_kind,
    generic_entity_node_id,
    make_entity_node,
)
from .graph_read import GraphReadRepository
from .graph_sinks import BaseCypherGraphSink, CypherStatement
from .profile_vectors import ProfileVectorIndexer
from .profiles import (
    ProfileRecord,
    profile_deactivation_from_fact_invalidation,
    semantic_fact_profile_from_fact,
)


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
    """Process episodes using Graphiti's native entity/edge extraction.

    This replaces the legacy ``GraphitiAdapter`` by calling Graphiti's
    ``extract_nodes`` and ``extract_edges`` directly while keeping the
    plan-then-execute Cypher architecture.
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
        profile_indexer: ProfileVectorIndexer | None = None,
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

    def _extract_edges(
        self,
        episode: EpisodicNode,
        nodes: list[EntityNode],
        previous_episodes: list[EpisodicNode],
    ) -> list[EntityEdge]:
        """Extract fact edges from the episode using Graphiti's native LLM extraction."""
        if self.llm_client is None or len(nodes) < 2:
            return []
        clients = _build_minimal_clients(self.llm_client)
        return _run_async(
            _graphiti_extract_edges(
                clients,
                episode,
                nodes,
                previous_episodes,
                edge_type_map=self.edge_type_map,
                group_id=episode.group_id,
                edge_types=self.edge_types,
                custom_extraction_instructions=self.custom_extraction_instructions,
            )
        )

    # ------------------------------------------------------------------
    # Core processing pipeline
    # ------------------------------------------------------------------

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

        # --- LLM entity extraction via Graphiti's native pipeline ---
        previous_episodes = _texts_to_episodes(episode.group_id, previous_episode_texts) if previous_episode_texts else []
        llm_extracted_nodes = self._extract_nodes(episode, previous_episodes)

        llm_resolved_nodes: list[EntityNode] = []
        created_context_nodes: list[EntityNode] = []
        for llm_node in llm_extracted_nodes:
            name = llm_node.name
            resolved = self.read_repository.resolve_candidates(
                repo_id=episode.group_id,
                text=name,
                extra_nodes=extra_nodes,
                limit=5,
            )
            if not resolved:
                generic_node = _build_generic_entity_node(
                    repo_id=episode.group_id,
                    name=name,
                )
                created_context_nodes.append(generic_node)
                llm_resolved_nodes.append(generic_node)
                mention_sources[generic_node.uuid].update({"llm", "created"})
                matched_terms[generic_node.uuid].add(name)
                continue
            for node in resolved:
                llm_resolved_nodes.append(node)
                mention_sources[node.uuid].add("llm")
                matched_terms[node.uuid].add(name)

        candidate_nodes = list(_dedupe_nodes([*extra_nodes, *deterministic_nodes, *llm_resolved_nodes]))

        # --- LLM fact/edge extraction via Graphiti's native pipeline ---
        extracted_edges = self._extract_edges(episode, candidate_nodes, previous_episodes)

        # Build mention records from all candidate nodes (same as before)
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

        # Build fact invalidations from extracted edges
        invalidations = tuple(
            FactInvalidation(
                repo_id=edge.group_id,
                source_node_id=edge.source_node_uuid,
                target_node_id=edge.target_node_uuid,
                relation_type=edge.name,
                observed_at=str(episode.valid_at),
                replacement_fact_id=edge.uuid,
                replacement_valid_at=str(edge.valid_at) if edge.valid_at else None,
            )
            for edge in extracted_edges
        )
        facts = tuple(_merge_fact_metadata(fact) for fact in extracted_edges)

        node_lookup = {node.uuid: node for node in candidate_nodes}
        profiles = tuple(
            self._build_profile(fact, node_lookup=node_lookup)
            for fact in facts
        )
        profile_deactivations = tuple(
            profile_deactivation_from_fact_invalidation(invalidation)
            for invalidation in invalidations
        )

        statements = [
            *self.graph_sink.plan_upsert_episodes([episode]),
        ]
        if created_context_nodes:
            statements.extend(self.graph_sink.plan_upsert_context_nodes(_dedupe_nodes(created_context_nodes)))
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
        if invalidations:
            statements.extend(self.graph_sink.plan_invalidate_fact_edges(invalidations))
        if facts:
            statements.extend(self.graph_sink.plan_apply_fact_edges(facts))

        return EpisodeProcessingResult(
            episode=episode,
            mentions=mention_records,
            facts=facts,
            invalidations=invalidations,
            profiles=profiles,
            profile_deactivations=profile_deactivations,
            statements=tuple(statements),
            created_context_nodes=_dedupe_nodes(created_context_nodes),
        )

    def apply_batch(
        self,
        items: Sequence[EpisodeWorkItem],
        *,
        execute: bool = False,
    ) -> EpisodeBatchResult:
        history: dict[str, list[str]] = {}
        staged_context_nodes: dict[str, tuple[EntityNode, ...]] = {}
        results: list[EpisodeProcessingResult] = []
        statements: list[CypherStatement] = []

        for item in items:
            thread_key = _thread_key(item.episode)
            staged_nodes = staged_context_nodes.get(item.episode.group_id, ())
            result = self.plan_episode(
                item.episode,
                extra_nodes=(*item.extra_nodes, *staged_nodes),
                previous_episode_texts=history.get(thread_key, ()),
            )
            results.append(result)
            statements.extend(result.statements)
            history.setdefault(thread_key, []).append(item.episode.content)
            if result.created_context_nodes:
                staged_context_nodes[item.episode.group_id] = _dedupe_nodes(
                    [*staged_nodes, *result.created_context_nodes]
                )

        executed = self.graph_sink.execute(statements) if execute and statements else 0
        if execute and results:
            profile_deactivations = [
                deactivation
                for result in results
                for deactivation in result.profile_deactivations
            ]
            profiles = [
                profile
                for result in results
                for profile in result.profiles
            ]
            if profile_deactivations:
                self.read_repository.metadata_store.deactivate_profiles(profile_deactivations)
            if profiles:
                self.read_repository.metadata_store.upsert_profiles(profiles)
            if self.profile_indexer is not None and (profiles or profile_deactivations):
                self.profile_indexer.apply_profile_updates(
                    profiles=profiles,
                    deactivations=profile_deactivations,
                )
        return EpisodeBatchResult(
            results=tuple(results),
            statements=tuple(statements),
            statements_executed=executed,
        )

    def _build_profile(
        self,
        fact: EntityEdge,
        *,
        node_lookup: Mapping[str, EntityNode],
    ) -> ProfileRecord:
        return semantic_fact_profile_from_fact(
            fact,
            source=node_lookup.get(fact.source_node_uuid),
            target=node_lookup.get(fact.target_node_uuid),
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
    from graphiti_core.nodes import EpisodeType
    from graphiti_core.utils.datetime_utils import utc_now
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


def _merge_fact_metadata(fact: EntityEdge) -> EntityEdge:
    attributes = dict(fact.attributes)
    attributes.setdefault("episode_ids", list(fact.episodes))
    return EntityEdge(
        uuid=fact.uuid,
        group_id=fact.group_id,
        source_node_uuid=fact.source_node_uuid,
        target_node_uuid=fact.target_node_uuid,
        name=fact.name,
        fact=fact.fact,
        episodes=fact.episodes,
        created_at=fact.created_at,
        valid_at=fact.valid_at,
        invalid_at=fact.invalid_at,
        expired_at=fact.expired_at,
        attributes=attributes,
    )


def _build_generic_entity_node(*, repo_id: str, name: str) -> EntityNode:
    cleaned = " ".join(name.strip().split())
    aliases = list(
        dict.fromkeys(
            token
            for token in cleaned.replace("/", " ").replace(".", " ").split()
            if token and token.casefold() != cleaned.casefold()
        )
    )
    return make_entity_node(
        node_id=generic_entity_node_id(repo_id, cleaned),
        repo_id=repo_id,
        kind="Entity",
        name=cleaned,
        aliases=aliases,
        metadata={"origin": "episode_llm"},
    )


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
