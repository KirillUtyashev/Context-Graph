from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Sequence

from .episodes import ContextNodeRef
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

    def latest_code_nodes(self, repo_id: str) -> tuple[ContextNodeRef, ...]:
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
        extra_nodes: Sequence[ContextNodeRef] = (),
        limit: int = 25,
    ) -> tuple[ContextNodeRef, ...]:
        scored: dict[str, tuple[int, ContextNodeRef]] = {}
        candidate_text = text or ""
        lowered_text = candidate_text.lower()
        search_terms = _extract_search_terms(candidate_text)

        for node in (*extra_nodes, *self.latest_code_nodes(repo_id), *self.graph_context_nodes(repo_id, search_terms, limit=limit)):
            score = self._score_node(node, lowered_text, search_terms)
            if score <= 0:
                continue
            current = scored.get(node.node_id)
            if current is None or score > current[0]:
                scored[node.node_id] = (score, node)

        ordered = sorted(
            scored.values(),
            key=lambda item: (
                -item[0],
                item[1].kind,
                item[1].qualified_name or item[1].name,
            ),
        )
        return tuple(node for _, node in ordered[:limit])

    def graph_context_nodes(
        self,
        repo_id: str,
        search_terms: Sequence[str],
        *,
        limit: int = 25,
    ) -> tuple[ContextNodeRef, ...]:
        if self.graph_sink is None:
            return ()
        return self.graph_sink.search_context_nodes(
            repo_id=repo_id,
            search_terms=search_terms,
            limit=limit,
        )

    def _from_canonical_node(self, node: CanonicalNode) -> ContextNodeRef:
        aliases: list[str] = []
        if node.file_path:
            aliases.append(node.file_path.rsplit("/", 1)[-1])
        if node.qualified_name:
            aliases.extend(part for part in node.qualified_name.split(".") if part)
        return ContextNodeRef(
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
        node: ContextNodeRef,
        lowered_text: str,
        search_terms: Iterable[str],
    ) -> int:
        terms = [term.lower() for term in node.searchable_terms()]
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
