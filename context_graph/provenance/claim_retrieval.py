"""Helpers for claim retrieval, ranking, and future semantic search."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Any, Sequence

from context_graph.query.parser import QueryParseResult, QueryParser

from .canonicalization import canonicalize_claim_text

if TYPE_CHECKING:
    from context_graph.runtime.embeddings import EmbeddingProvider
    from context_graph.models.profiles import ProfileRecord
    from context_graph.backend import MetadataStore
    from context_graph.backend import ProfileVectorStore
    from .models import ClaimRecord


_WORD_RE = re.compile(r"[a-z0-9_]+")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_FILE_PATH_RE = re.compile(
    r"\b(?:[\w.-]+/)*[\w.-]+\.(?:py|js|ts|tsx|jsx|java|go|rs|c|cc|cpp|h|hpp|cs|sh|json|yaml|yml|toml)\b",
    re.IGNORECASE,
)
_DOTTED_NAME_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+\b")
_UPPER_IDENTIFIER_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
_SNAKE_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_]+_[A-Za-z0-9_]+\b")
_NUMERIC_LITERAL_RE = re.compile(r"\b\d+\b")
_STOP_TOKENS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "was",
        "were",
        "is",
        "are",
        "be",
        "being",
        "been",
        "this",
        "that",
        "these",
        "those",
        "from",
        "into",
        "because",
        "due",
        "not",
        "issue",
        "change",
        "changed",
        "changes",
        "updated",
        "update",
        "left",
        "value",
        "new",
    }
)


@dataclass(frozen=True, slots=True)
class ClaimRetrievalFeatures:
    """Deterministic retrieval features derived from claim-support text."""

    lexical_tokens: tuple[str, ...]
    code_anchors: tuple[str, ...]
    numeric_anchors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClaimSemanticSearchHit:
    """One semantic-claim vector result hydrated for provenance use."""

    profile_id: str
    claim_id: str
    claim_text: str
    claim_kind: str | None
    claim_subkind: str | None
    score: float
    key_text: str
    key_origin: str


@dataclass(frozen=True, slots=True)
class ClaimSemanticSearchResult:
    """Hydrated semantic-claim search results."""

    parse_result: QueryParseResult
    hits: tuple[ClaimSemanticSearchHit, ...]
    profiles: tuple["ProfileRecord", ...]


def build_claim_retrieval_features(
    *,
    claim_text: str,
    support_texts: Sequence[str] = (),
    fallback_texts: Sequence[str] = (),
    about_record_ids: Sequence[str] = (),
) -> ClaimRetrievalFeatures:
    texts = tuple(
        text
        for text in (
            claim_text,
            *support_texts,
            *fallback_texts,
        )
        if isinstance(text, str) and text.strip()
    )
    lexical_tokens = tuple(
        _dedupe_preserve_order(
            token
            for text in texts
            for token in _claim_lexical_tokens(text)
        )
    )
    raw_code_anchors = tuple(
        _dedupe_preserve_order(
            anchor
            for text in texts
            for anchor in _claim_code_anchors(text)
        )
    )
    grounded_terms = grounded_code_terms(about_record_ids)
    code_anchors = tuple(
        _dedupe_preserve_order(
            anchor.casefold()
            for anchor in (*raw_code_anchors, *grounded_terms)
            if anchor and anchor.strip()
        )
    )
    numeric_anchors = tuple(
        _dedupe_preserve_order(
            match.group(0)
            for text in texts
            for match in _NUMERIC_LITERAL_RE.finditer(text)
        )
    )
    return ClaimRetrievalFeatures(
        lexical_tokens=lexical_tokens,
        code_anchors=code_anchors,
        numeric_anchors=numeric_anchors,
    )


def claim_profile_keys(
    *,
    claim_text: str,
    claim_kind: str,
    claim_subkind: str | None,
    support_texts: Sequence[str] = (),
    about_record_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    normalized_claim_text = canonicalize_claim_text(claim_text)
    claim_kind_label = claim_kind.replace("_", " ").strip().lower()
    claim_subkind_label = (claim_subkind or "").replace("_", " ").strip().lower()
    raw_code_anchors = tuple(
        _dedupe_preserve_order(
            anchor
            for text in (claim_text, *support_texts)
            if isinstance(text, str) and text.strip()
            for anchor in _claim_code_anchors(text)
        )
    )
    grounded_terms = grounded_code_terms(about_record_ids)
    return tuple(
        _dedupe_preserve_order(
            value
            for value in (
                normalized_claim_text,
                claim_text.strip(),
                claim_kind_label,
                claim_subkind_label,
                *raw_code_anchors,
                *grounded_terms,
            )
            if isinstance(value, str) and value.strip()
        )
    )


def grounded_code_terms(about_record_ids: Sequence[str]) -> tuple[str, ...]:
    terms: list[str] = []
    for record_id in about_record_ids:
        if not record_id:
            continue
        core = str(record_id).split("@", 1)[0]
        tail = core.rsplit(":", 1)[-1]
        segments = [segment for segment in tail.split(".") if segment]
        if tail:
            terms.append(tail)
        if segments:
            terminal = segments[-1]
            if terminal == "__file__" and len(segments) > 1:
                terms.append(segments[-2])
            elif terminal != "__file__":
                terms.append(terminal)
        if len(segments) > 1 and segments[-1] == "__file__":
            terms.append(".".join(segments[-2:]))
    return tuple(
        _dedupe_preserve_order(
            value
            for value in terms
            if isinstance(value, str) and value.strip()
        )
    )


class ClaimSemanticRetrievalService:
    """Semantic-claim vector retrieval boundary for future planner integration."""

    def __init__(
        self,
        *,
        metadata_store: "MetadataStore",
        vector_store: "ProfileVectorStore",
        embedding_provider: "EmbeddingProvider",
        query_parser: QueryParser,
    ) -> None:
        self.metadata_store = metadata_store
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider
        self.query_parser = query_parser

    def build_profile(
        self,
        claim: "ClaimRecord",
        *,
        about_record_ids: Sequence[str] = (),
        support_texts: Sequence[str] = (),
    ) -> "ProfileRecord":
        from context_graph.models.profiles import semantic_claim_profile_from_claim

        return semantic_claim_profile_from_claim(
            claim,
            about_record_ids=about_record_ids,
            support_texts=support_texts,
        )

    def search(
        self,
        *,
        repo_id: str,
        query: str,
        limit: int = 10,
        active_only: bool = True,
        llm_enabled: bool = True,
    ) -> ClaimSemanticSearchResult:
        from context_graph.models.profiles import SEMANTIC_CLAIM_PROFILE_KIND

        parse_result = self.query_parser.parse(query, llm_enabled=llm_enabled)
        keywords = list(parse_result.high_level_keywords) or [query]
        embeddings = self.embedding_provider.embed_texts(keywords)
        merged_hits: dict[str, Any] = {}
        for embedding in embeddings:
            hits = self.vector_store.search_profile_keys(
                repo_id=repo_id,
                embedding=embedding,
                limit=limit,
                active_only=active_only,
                profile_kinds=(SEMANTIC_CLAIM_PROFILE_KIND,),
            )
            for hit in hits:
                current = merged_hits.get(hit.profile_id)
                if current is None or hit.score > current.score:
                    merged_hits[hit.profile_id] = hit
        ordered_hits = tuple(
            sorted(
                merged_hits.values(),
                key=lambda hit: (-hit.score, hit.profile_id, hit.profile_key_id),
            )[:limit]
        )
        profile_map = {
            profile.profile_id: profile
            for profile in self.metadata_store.load_profiles(repo_id, active_only=active_only)
            if profile.profile_kind == SEMANTIC_CLAIM_PROFILE_KIND
        }
        profiles = tuple(
            profile_map[hit.profile_id]
            for hit in ordered_hits
            if hit.profile_id in profile_map
        )
        hydrated_hits = tuple(
            ClaimSemanticSearchHit(
                profile_id=hit.profile_id,
                claim_id=_string_or_none(profile.metadata.get("claim_id")) or profile.profile_target_id,
                claim_text=_string_or_none(profile.metadata.get("claim_text")) or profile.value_text,
                claim_kind=_string_or_none(profile.metadata.get("claim_kind")),
                claim_subkind=_string_or_none(profile.metadata.get("claim_subkind")),
                score=hit.score,
                key_text=hit.key_text,
                key_origin=hit.key_origin,
            )
            for hit, profile in zip(ordered_hits, profiles)
        )
        return ClaimSemanticSearchResult(
            parse_result=parse_result,
            hits=hydrated_hits,
            profiles=profiles,
        )


def semantic_claim_profile_key_records(
    claim: "ClaimRecord",
    *,
    about_record_ids: Sequence[str] = (),
    support_texts: Sequence[str] = (),
    embedding_provider: EmbeddingProvider,
) -> tuple[Any, ...]:
    """Build concrete vector-store records for one semantic claim profile."""

    from context_graph.profile_vectors import build_profile_key_records
    from context_graph.models.profiles import semantic_claim_profile_from_claim

    profile = semantic_claim_profile_from_claim(
        claim,
        about_record_ids=about_record_ids,
        support_texts=support_texts,
    )
    return build_profile_key_records((profile,), embedding_provider)


def semantic_claim_profile_key_ids(
    claim: "ClaimRecord",
    *,
    about_record_ids: Sequence[str] = (),
    support_texts: Sequence[str] = (),
) -> tuple[str, ...]:
    """Expose stable key ids for tests and future profile indexing."""

    from context_graph.profile_vectors import profile_key_id
    from context_graph.models.profiles import semantic_claim_profile_from_claim

    profile = semantic_claim_profile_from_claim(
        claim,
        about_record_ids=about_record_ids,
        support_texts=support_texts,
    )
    return tuple(
        profile_key_id(profile.profile_id, key.text, key.origin)
        for key in profile.keys
    )


def _claim_lexical_tokens(text: str) -> tuple[str, ...]:
    normalized = canonicalize_claim_text(text)
    return tuple(
        _dedupe_preserve_order(
            token
            for token in _WORD_RE.findall(normalized)
            if len(token) > 2 and token not in _STOP_TOKENS
        )
    )


def _claim_code_anchors(text: str) -> tuple[str, ...]:
    anchors: list[str] = []
    for pattern in (_BACKTICK_RE, _FILE_PATH_RE, _DOTTED_NAME_RE, _UPPER_IDENTIFIER_RE, _SNAKE_IDENTIFIER_RE):
        for match in pattern.finditer(text):
            value = match.group(1) if pattern is _BACKTICK_RE else match.group(0)
            normalized = " ".join(value.strip().split())
            if not normalized:
                continue
            anchors.append(normalized)
            if _FILE_PATH_RE.fullmatch(normalized):
                anchors.append(normalized.rsplit("/", 1)[-1])
    return tuple(_dedupe_preserve_order(anchors))


def _dedupe_preserve_order(values: Sequence[str]) -> tuple[str, ...]:
    deduped: dict[str, str] = {}
    for value in values:
        normalized = " ".join(str(value).strip().split())
        if not normalized:
            continue
        deduped.setdefault(normalized.casefold(), normalized)
    return tuple(deduped.values())


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
