from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Sequence

from .episodes import (
    EntityNode,
    entity_node_kind,
    entity_node_qualified_name,
    entity_node_searchable_terms,
    make_entity_node,
)
from .graph_sinks import BaseCypherGraphSink
from .metadata import MetadataStore
from .models import CanonicalNode


_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./:-]*")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_ISSUE_RE = re.compile(r"#(\d+)")
_COMMIT_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")


@dataclass(frozen=True, slots=True)
class GraphReadRepository:
    metadata_store: MetadataStore
    graph_sink: BaseCypherGraphSink | None = None

    def latest_code_nodes(self, repo_id: str) -> tuple[EntityNode, ...]:
        snapshot = self.metadata_store.load_latest_snapshot(repo_id)
        if snapshot is None:
            return ()
        return tuple(
            self._from_canonical_node(node)
            for node in snapshot.nodes
            if node.kind != "Project"
        )

    def resolve_candidates(
        self,
        *,
        repo_id: str,
        text: str,
        extra_nodes: Sequence[EntityNode] = (),
        limit: int = 25,
    ) -> tuple[EntityNode, ...]:
        scored: dict[str, tuple[int, EntityNode]] = {}
        candidate_text = text or ""
        lowered_text = candidate_text.lower()
        search_terms = _extract_search_terms(candidate_text)

        for node in (*extra_nodes, *self.latest_code_nodes(repo_id), *self.graph_context_nodes(repo_id, search_terms, limit=limit)):
            score = self._score_node(node, lowered_text, search_terms)
            if score <= 0:
                continue
            current = scored.get(node.uuid)
            if current is None or score > current[0]:
                scored[node.uuid] = (score, node)

        ordered = sorted(
            scored.values(),
            key=lambda item: (
                -item[0],
                entity_node_kind(item[1]),
                entity_node_qualified_name(item[1]) or item[1].name,
            ),
        )
        return tuple(node for _, node in ordered[:limit])

    def graph_context_nodes(
        self,
        repo_id: str,
        search_terms: Sequence[str],
        *,
        limit: int = 25,
    ) -> tuple[EntityNode, ...]:
        if self.graph_sink is None:
            return ()
        return self.graph_sink.search_context_nodes(
            repo_id=repo_id,
            search_terms=search_terms,
            limit=limit,
        )

    def _from_canonical_node(self, node: CanonicalNode) -> EntityNode:
        aliases: list[str] = []
        if node.file_path:
            aliases.append(node.file_path.rsplit("/", 1)[-1])
        if node.qualified_name:
            aliases.extend(part for part in node.qualified_name.split(".") if part)
        return make_entity_node(
            node_id=node.node_id,
            repo_id=node.repo_id,
            kind=node.kind,
            name=node.name,
            qualified_name=node.qualified_name,
            file_path=node.file_path,
            aliases=tuple(dict.fromkeys(alias for alias in aliases if alias)),
            metadata=node.properties,
        )

    def _score_node(
        self,
        node: EntityNode,
        lowered_text: str,
        search_terms: Iterable[str],
    ) -> int:
        terms = [term.lower() for term in entity_node_searchable_terms(node)]
        score = 0
        for term in terms:
            if not term:
                continue
            if term == node.name.lower():
                if f"`{term}`" in lowered_text:
                    score += 8
            if term in lowered_text:
                score += 4
            for search_term in search_terms:
                if term == search_term:
                    score += 10
                elif term.endswith(f".{search_term}") or term.endswith(f"/{search_term}"):
                    score += 6
        return score


def _extract_search_terms(text: str) -> tuple[str, ...]:
    values: list[str] = []
    for pattern in (_BACKTICK_RE, _COMMIT_RE, _IDENTIFIER_RE):
        for match in pattern.findall(text):
            if isinstance(match, tuple):
                values.extend(item for item in match if item)
            else:
                values.append(match)
    values.extend(f"#{number}" for number in _ISSUE_RE.findall(text))
    return tuple(dict.fromkeys(value.lower() for value in values if value))
