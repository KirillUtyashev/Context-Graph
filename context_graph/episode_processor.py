from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
from typing import Iterable, Mapping, Sequence

from .episodes import (
    ContextNodeRef,
    EpisodeProcessingResult,
    EpisodeRecord,
    FactRecord,
    MentionRecord,
    generic_entity_node_id,
)
from .graph_read import GraphReadRepository
from .graph_sinks import BaseCypherGraphSink, CypherStatement
from .graphiti_adapter import GraphitiAdapter
from .profile_vectors import ProfileVectorIndexer
from .profiles import (
    ProfileRecord,
    profile_deactivation_from_fact_invalidation,
    semantic_fact_profile_from_fact,
)


@dataclass(frozen=True, slots=True)
class EpisodeWorkItem:
    episode: EpisodeRecord
    extra_nodes: tuple[ContextNodeRef, ...] = ()


@dataclass(frozen=True, slots=True)
class EpisodeBatchResult:
    results: tuple[EpisodeProcessingResult, ...]
    statements: tuple[CypherStatement, ...]
    statements_executed: int


class EpisodeProcessor:
    def __init__(
        self,
        *,
        graph_sink: BaseCypherGraphSink,
        read_repository: GraphReadRepository,
        adapter: GraphitiAdapter | None = None,
        profile_indexer: ProfileVectorIndexer | None = None,
    ) -> None:
        self.graph_sink = graph_sink
        self.read_repository = read_repository
        self.adapter = adapter or GraphitiAdapter()
        self.profile_indexer = profile_indexer

    def plan_episode(
        self,
        episode: EpisodeRecord,
        *,
        extra_nodes: Sequence[ContextNodeRef] = (),
        previous_episode_texts: Sequence[str] = (),
    ) -> EpisodeProcessingResult:
        deterministic_nodes = self.read_repository.resolve_candidates(
            repo_id=episode.repo_id,
            text=episode.text,
            extra_nodes=extra_nodes,
        )
        mention_sources: dict[str, set[str]] = defaultdict(set)
        matched_terms: dict[str, set[str]] = defaultdict(set)

        for node in extra_nodes:
            mention_sources[node.node_id].add("context")
        for node in deterministic_nodes:
            mention_sources[node.node_id].add("deterministic")

        llm_entity_names = self.adapter.extract_entity_names(
            episode=episode,
            previous_episode_texts=previous_episode_texts,
        )
        llm_resolved_nodes: list[ContextNodeRef] = []
        created_context_nodes: list[ContextNodeRef] = []
        for name in llm_entity_names:
            resolved = self.read_repository.resolve_candidates(
                repo_id=episode.repo_id,
                text=name,
                extra_nodes=extra_nodes,
                limit=5,
            )
            if not resolved:
                generic_node = _build_generic_context_node(
                    repo_id=episode.repo_id,
                    name=name,
                )
                created_context_nodes.append(generic_node)
                llm_resolved_nodes.append(generic_node)
                mention_sources[generic_node.node_id].update({"llm", "created"})
                matched_terms[generic_node.node_id].add(name)
                continue
            for node in resolved:
                llm_resolved_nodes.append(node)
                mention_sources[node.node_id].add("llm")
                matched_terms[node.node_id].add(name)

        candidate_nodes = _dedupe_nodes([*extra_nodes, *deterministic_nodes, *llm_resolved_nodes])
        extraction = self.adapter.extract_facts(
            episode=episode,
            candidate_nodes=candidate_nodes,
            previous_episode_texts=previous_episode_texts,
        )
        mention_metadata = dict(extraction.mention_metadata)
        mention_records = tuple(
            MentionRecord(
                episode_node_id=episode.node_id,
                target_node_id=node.node_id,
                repo_id=episode.repo_id,
                metadata=_build_mention_metadata(
                    node=node,
                    base=mention_metadata.get(node.node_id),
                    sources=mention_sources.get(node.node_id),
                    matched_terms=matched_terms.get(node.node_id),
                ),
            )
            for node in _dedupe_nodes(extraction.mentions)
        )
        facts = tuple(
            _merge_fact_metadata(fact)
            for fact in extraction.facts
        )
        node_lookup = {node.node_id: node for node in candidate_nodes}
        profiles = tuple(
            self._build_profile(fact, node_lookup=node_lookup)
            for fact in facts
        )
        profile_deactivations = tuple(
            profile_deactivation_from_fact_invalidation(invalidation)
            for invalidation in extraction.invalidations
        )

        statements = [
            *self.graph_sink.plan_upsert_episodes([episode]),
        ]
        if created_context_nodes:
            statements.extend(self.graph_sink.plan_upsert_context_nodes(_dedupe_nodes(created_context_nodes)))
        if episode.parent_node_id:
            statements.extend(
                self.graph_sink.plan_attach_episode_parent(
                    repo_id=episode.repo_id,
                    episode_node_id=episode.node_id,
                    target_node_id=episode.parent_node_id,
                )
            )
        if mention_records:
            statements.extend(self.graph_sink.plan_attach_mentions(mention_records))
        if extraction.invalidations:
            statements.extend(self.graph_sink.plan_invalidate_fact_edges(extraction.invalidations))
        if facts:
            statements.extend(self.graph_sink.plan_apply_fact_edges(facts))

        return EpisodeProcessingResult(
            episode=episode,
            mentions=mention_records,
            facts=facts,
            invalidations=extraction.invalidations,
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
        staged_context_nodes: dict[str, tuple[ContextNodeRef, ...]] = {}
        results: list[EpisodeProcessingResult] = []
        statements: list[CypherStatement] = []

        for item in items:
            thread_key = _thread_key(item.episode)
            staged_nodes = staged_context_nodes.get(item.episode.repo_id, ())
            result = self.plan_episode(
                item.episode,
                extra_nodes=(*item.extra_nodes, *staged_nodes),
                previous_episode_texts=history.get(thread_key, ()),
            )
            results.append(result)
            statements.extend(result.statements)
            history.setdefault(thread_key, []).append(item.episode.text)
            if result.created_context_nodes:
                staged_context_nodes[item.episode.repo_id] = _dedupe_nodes(
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
        fact: FactRecord,
        *,
        node_lookup: Mapping[str, ContextNodeRef],
    ) -> ProfileRecord:
        return semantic_fact_profile_from_fact(
            fact,
            source=node_lookup.get(fact.source_node_id),
            target=node_lookup.get(fact.target_node_id),
        )


def _thread_key(episode: EpisodeRecord) -> str:
    if episode.parent_node_id:
        return f"parent:{episode.parent_node_id}"
    thread_id = episode.metadata.get("thread_id")
    if isinstance(thread_id, str) and thread_id:
        return f"thread:{thread_id}"
    return f"episode-source:{episode.source_type}:{episode.source_id}"


def _dedupe_nodes(nodes: Iterable[ContextNodeRef]) -> tuple[ContextNodeRef, ...]:
    deduped: dict[str, ContextNodeRef] = {}
    for node in nodes:
        deduped.setdefault(node.node_id, node)
    return tuple(deduped.values())


def _merge_fact_metadata(fact: FactRecord) -> FactRecord:
    metadata = dict(fact.metadata)
    metadata.setdefault("episode_ids", list(fact.episode_ids))
    return FactRecord(
        fact_id=fact.fact_id,
        repo_id=fact.repo_id,
        source_node_id=fact.source_node_id,
        target_node_id=fact.target_node_id,
        relation_type=fact.relation_type,
        fact=fact.fact,
        episode_ids=fact.episode_ids,
        created_at=fact.created_at,
        valid_at=fact.valid_at,
        invalid_at=fact.invalid_at,
        expired_at=fact.expired_at,
        metadata=metadata,
    )


def _build_generic_context_node(*, repo_id: str, name: str) -> ContextNodeRef:
    cleaned = " ".join(name.strip().split())
    aliases = tuple(
        dict.fromkeys(
            token
            for token in cleaned.replace("/", " ").replace(".", " ").split()
            if token and token.casefold() != cleaned.casefold()
        )
    )
    return ContextNodeRef(
        node_id=generic_entity_node_id(repo_id, cleaned),
        repo_id=repo_id,
        kind="Entity",
        name=cleaned,
        aliases=aliases,
        metadata={"origin": "episode_llm"},
    )


def _build_mention_metadata(
    *,
    node: ContextNodeRef,
    base: Mapping[str, object] | None,
    sources: set[str] | None,
    matched_terms: set[str] | None,
) -> Mapping[str, object]:
    metadata: dict[str, object] = {"target_kind": node.kind}
    if base:
        metadata.update(dict(base))
    if sources:
        metadata["match_sources"] = sorted(sources)
    if matched_terms:
        metadata["matched_terms"] = sorted(matched_terms)
    return metadata
