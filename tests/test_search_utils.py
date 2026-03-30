from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from context_graph.profile_vectors import build_profile_key_records, profile_key_id
from context_graph.profiles import ProfileRecord
from context_graph.storage.backend.errors import (
    BackendFeatureNotImplementedError,
    UnsupportedCapabilityError,
)
from context_graph.storage.backend.neo4j.search import (
    Neo4jSearchOperations,
    _build_lexical_search_query,
    _records_to_hits as neo4j_records_to_hits,
)
from context_graph.storage.backend.neptune.search import (
    NeptuneSearchOperations,
    _aoss_response_to_hits,
    build_aoss_query,
)
from context_graph.storage.backend.search import ensure_lexical_mode, validate_extra_filters
from context_graph.storage.backend.types import (
    GraphRecordRef,
    RecordKind,
    SearchMode,
    SearchQuery,
    SearchHit,
)


class FakeEmbeddingProvider:
    def embed_texts(self, texts):
        return [[float(len(text)), float(index + 1)] for index, text in enumerate(texts)]


class FakeNeo4jDriver:
    def __init__(self, results: list[list[dict]] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, dict]] = []

    async def execute_query(self, query: str, **kwargs):
        self.calls.append((query, kwargs))
        if self.results:
            return self.results.pop(0)
        return []


class FakeNeptuneDriver:
    def __init__(
        self,
        *,
        aoss_client: object | None = object(),
        aoss_responses: list[dict] | None = None,
        query_results: list[list[dict]] | None = None,
    ) -> None:
        self.aoss_client = aoss_client
        self.aoss_responses = list(aoss_responses or [])
        self.query_results = list(query_results or [])
        self.aoss_calls: list[tuple[str, SearchQuery]] = []
        self.query_calls: list[tuple[str, dict]] = []

    def _run_aoss_query(self, *, index_name: str, query: SearchQuery):
        self.aoss_calls.append((index_name, query))
        if self.aoss_responses:
            return self.aoss_responses.pop(0)
        return {"hits": {"hits": []}}

    async def execute_query(self, query: str, **kwargs):
        self.query_calls.append((query, kwargs))
        if self.query_results:
            return self.query_results.pop(0)
        return []


def make_profile(*, profile_id: str = "profile:1", keys: tuple[str, ...] = ("helper", "rollback")) -> ProfileRecord:
    return ProfileRecord(
        profile_id=profile_id,
        repo_id="demo",
        profile_kind="semantic_fact_profile",
        profile_target_kind="FactEdge",
        profile_target_id="fact:1",
        relation_type="JUSTIFIES",
        source_node_id="node:source",
        source_node_kind="Function",
        target_node_id="node:target",
        target_node_kind="Function",
        target_revision_id=None,
        value_text="main uses helper for rollback behavior",
        keys=keys,
        metadata={"source": "unit-test"},
        is_active=True,
        valid_from_revision=None,
        valid_to_revision=None,
        updated_at="2026-03-27T00:00:00+00:00",
    )


def test_search_validation_helpers_accept_safe_filters_and_reject_invalid_ones() -> None:
    validate_extra_filters({"repo_id": "demo", "active": True, "score_band": "high"})

    with pytest.raises(ValueError, match="invalid filter key"):
        validate_extra_filters({"repo-id": "demo"})

    ensure_lexical_mode(SearchQuery(mode=SearchMode.LEXICAL, text="helper"))
    with pytest.raises(ValueError, match="only lexical mode"):
        ensure_lexical_mode(SearchQuery(mode=SearchMode.VECTOR, embedding=(0.1, 0.2)))


def test_neo4j_lexical_query_builder_includes_filters_and_limits() -> None:
    query = SearchQuery(
        mode=SearchMode.LEXICAL,
        text="rollback helper",
        record_kinds=(RecordKind.NODE,),
        record_types=("research_artifact",),
        repo_ids=("demo",),
        group_ids=("demo",),
        limit=5,
        filters={"active": True, "kind": ["Function", "File"]},
    )

    cypher, params = _build_lexical_search_query(
        index_name="backend_node_search_documents",
        label="NodeSearchDocument",
        search_query=query,
    )

    assert 'db.index.fulltext.queryNodes("backend_node_search_documents"' in cypher
    assert "doc.record_type IN $record_types" in cypher
    assert "doc.repo_id IN $repo_ids" in cypher
    assert "doc.group_id IN $group_ids" in cypher
    assert "doc.active = $filter_active" in cypher
    assert "doc.kind IN $filter_kind" in cypher
    assert params["search_text"] == "rollback helper"
    assert params["search_limit"] == 5
    assert params["record_types"] == ["research_artifact"]
    assert params["repo_ids"] == ["demo"]
    assert params["group_ids"] == ["demo"]
    assert params["filter_active"] is True
    assert params["filter_kind"] == ["Function", "File"]


def test_neo4j_records_to_hits_preserve_record_refs_and_scores() -> None:
    hits = neo4j_records_to_hits(
        kind=RecordKind.NODE,
        records=[
            {
                "record_id": "node:1",
                "record_type": "research_artifact",
                "repo_id": "demo",
                "group_id": "group-1",
                "score": 4.5,
            }
        ],
    )

    assert hits == [
        SearchHit(
            ref=GraphRecordRef(
                record_kind=RecordKind.NODE,
                record_id="node:1",
                record_type="research_artifact",
                repo_id="demo",
                group_id="group-1",
            ),
            score=4.5,
            lexical_score=4.5,
            metadata={"repo_id": "demo", "group_id": "group-1", "provider": "neo4j"},
        )
    ]


def test_neo4j_search_and_fetch_cover_current_backend_contract() -> None:
    async def run_test() -> None:
        driver = FakeNeo4jDriver(
            results=[
                [
                    {
                        "record_id": "node:1",
                        "record_type": "research_artifact",
                        "repo_id": "demo",
                        "group_id": "group-1",
                        "score": 3.0,
                    }
                ],
                [
                    {
                        "record_kind": "node",
                        "record_id": "node:1",
                        "record_type": "research_artifact",
                        "repo_id": "demo",
                        "group_id": "group-1",
                        "labels": ["BackendRecord", "ResearchArtifact"],
                        "properties": {"title": "Rollback plan"},
                    }
                ],
            ]
        )
        ops = Neo4jSearchOperations(driver)

        hits = await ops.search(
            SearchQuery(
                mode=SearchMode.LEXICAL,
                text="rollback",
                record_kinds=(RecordKind.NODE,),
                limit=3,
            )
        )
        fetched = await ops.fetch([hits[0].ref])

        assert len(hits) == 1
        assert hits[0].ref.record_id == "node:1"
        assert fetched[0]["properties"]["title"] == "Rollback plan"
        assert any("db.index.fulltext.queryNodes" in query for query, _ in driver.calls)

    asyncio.run(run_test())


def test_neo4j_search_rejects_non_lexical_modes() -> None:
    async def run_test() -> None:
        ops = Neo4jSearchOperations(FakeNeo4jDriver())

        with pytest.raises(BackendFeatureNotImplementedError, match="vector search is not implemented"):
            await ops.search(SearchQuery(mode=SearchMode.VECTOR, embedding=(0.1,)))

    asyncio.run(run_test())


def test_neptune_aoss_query_builder_captures_terms_filters_and_limit() -> None:
    query = SearchQuery(
        mode=SearchMode.LEXICAL,
        text="helper rollback",
        record_types=("research_artifact",),
        repo_ids=("demo",),
        group_ids=("group-1",),
        limit=7,
        filters={"active": True, "kind": ["Function", "File"]},
    )

    body = build_aoss_query(query)

    assert body["size"] == 7
    assert body["query"]["bool"]["must"][0]["multi_match"]["query"] == "helper rollback"
    assert {"terms": {"record_type": ["research_artifact"]}} in body["query"]["bool"]["filter"]
    assert {"terms": {"repo_id": ["demo"]}} in body["query"]["bool"]["filter"]
    assert {"terms": {"group_id": ["group-1"]}} in body["query"]["bool"]["filter"]
    assert {"term": {"active": True}} in body["query"]["bool"]["filter"]
    assert {"terms": {"kind": ["Function", "File"]}} in body["query"]["bool"]["filter"]


def test_neptune_aoss_hits_are_translated_into_ranked_search_hits() -> None:
    hits = _aoss_response_to_hits(
        kind=RecordKind.EDGE,
        response={
            "hits": {
                "hits": [
                    {
                        "_index": "backend_edge_documents",
                        "_score": 8.2,
                        "_source": {
                            "record_id": "edge:1",
                            "record_type": "github_pull_request",
                            "repo_id": "demo",
                            "group_id": "group-1",
                        },
                    }
                ]
            }
        },
    )

    assert len(hits) == 1
    assert hits[0].ref.record_kind is RecordKind.EDGE
    assert hits[0].ref.record_id == "edge:1"
    assert hits[0].score == pytest.approx(8.2)
    assert hits[0].metadata == {"provider": "neptune", "index": "backend_edge_documents"}


def test_neptune_search_and_fetch_use_current_driver_hooks() -> None:
    async def run_test() -> None:
        driver = FakeNeptuneDriver(
            aoss_responses=[
                {
                    "hits": {
                        "hits": [
                            {
                                "_index": "backend_node_documents",
                                "_score": 5.0,
                                "_source": {
                                    "record_id": "node:1",
                                    "record_type": "research_artifact",
                                    "repo_id": "demo",
                                    "group_id": "group-1",
                                },
                            }
                        ]
                    }
                }
            ],
            query_results=[
                [
                    {
                        "record_kind": "node",
                        "record_id": "node:1",
                        "record_type": "research_artifact",
                        "repo_id": "demo",
                        "group_id": "group-1",
                        "labels": ["ResearchArtifact"],
                        "properties": {"title": "Rollback note"},
                    }
                ]
            ],
        )
        ops = NeptuneSearchOperations(driver)

        hits = await ops.search(SearchQuery(mode=SearchMode.LEXICAL, text="rollback", limit=4))
        fetched = await ops.fetch([hits[0].ref])

        assert len(hits) == 1
        assert driver.aoss_calls[0][0] == "backend_node_documents"
        assert fetched[0]["properties"]["title"] == "Rollback note"

    asyncio.run(run_test())


def test_neptune_search_requires_aoss_and_lexical_mode() -> None:
    async def run_test() -> None:
        no_aoss_ops = NeptuneSearchOperations(FakeNeptuneDriver(aoss_client=None))
        with pytest.raises(UnsupportedCapabilityError, match="requires an AOSS host"):
            await no_aoss_ops.search(SearchQuery(mode=SearchMode.LEXICAL, text="rollback"))

        ops = NeptuneSearchOperations(FakeNeptuneDriver())
        with pytest.raises(BackendFeatureNotImplementedError, match="hybrid search is not implemented"):
            await ops.search(SearchQuery(mode=SearchMode.HYBRID, text="rollback"))

    asyncio.run(run_test())


def test_profile_key_helpers_build_stable_ids_and_embeddings() -> None:
    profile = make_profile(keys=("helper", "helper", "rollback path"))
    records = build_profile_key_records((profile,), FakeEmbeddingProvider())

    assert len(records) == 3
    assert records[0].profile_key_id == profile_key_id(profile.profile_id, "helper")
    assert records[0].embedding == (6.0, 1.0)
    assert records[-1].key_text == "rollback path"
