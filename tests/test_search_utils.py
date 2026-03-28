from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from context_graph.memory.search.search_filters import SearchFilters
from context_graph.memory.search import search_utils as su
from context_graph.storage.graph.driver import GraphProvider


@dataclass
class FakeNode:
    uuid: str
    group_id: str = "g1"
    name: str = "node"
    name_embedding: list[float] | None = None


@dataclass
class FakeEdge:
    uuid: str
    source_node_uuid: str = "s1"
    target_node_uuid: str = "t1"
    group_id: str = "g1"
    episodes: list[str] | None = None
    fact_embedding: list[float] | None = None

    def __post_init__(self) -> None:
        if self.episodes is None:
            self.episodes = []

    def model_dump(self) -> dict[str, object]:
        return {
            "uuid": self.uuid,
            "source_node_uuid": self.source_node_uuid,
            "target_node_uuid": self.target_node_uuid,
            "group_id": self.group_id,
            "fact_embedding": self.fact_embedding,
        }


class FakeDriver:
    def __init__(
        self,
        provider: GraphProvider,
        *,
        execute_results: list[tuple[object, object, object]] | None = None,
        search_interface=None,
        graph_operations_interface=None,
        aoss_result: dict | None = None,
    ) -> None:
        self.provider = provider
        self.search_interface = search_interface
        self.graph_operations_interface = graph_operations_interface
        self.fulltext_syntax = ""
        self._execute_results = list(execute_results or [])
        self.calls: list[tuple[str, dict]] = []
        self.aoss_calls: list[tuple[str, str, int | None]] = []
        self.aoss_result = aoss_result or {"hits": {"total": {"value": 0}, "hits": []}}

    async def execute_query(self, cypher_query_: str, **kwargs):
        self.calls.append((cypher_query_, kwargs))
        if self._execute_results:
            return self._execute_results.pop(0)
        return [], [], None

    def run_aoss_query(self, index_name: str, query: str, limit: int | None = None):
        self.aoss_calls.append((index_name, query, limit))
        return self.aoss_result

    def build_fulltext_query(
        self, query: str, group_ids: list[str] | None = None, max_query_length: int = 128
    ) -> str:
        return f"ft::{query}::{group_ids}::{max_query_length}"


def run(coro):
    return asyncio.run(coro)


def test_calculate_cosine_similarity_handles_zero_and_nonzero_vectors() -> None:
    assert su.calculate_cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert su.calculate_cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0


def test_fulltext_query_covers_provider_specific_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(su, "validate_group_ids", lambda group_ids: None)
    monkeypatch.setattr(su, "lucene_sanitize", lambda query: f"sanitized:{query}")

    kuzu_driver = FakeDriver(GraphProvider.KUZU)
    assert su.fulltext_query("short query", ["g1"], kuzu_driver) == "short query"
    assert su.fulltext_query(" ".join(["x"] * 129), ["g1"], kuzu_driver) == ""

    falkor_driver = FakeDriver(GraphProvider.FALKORDB)
    assert su.fulltext_query("hello", ["g1"], falkor_driver) == "ft::hello::['g1']::128"

    neo4j_driver = FakeDriver(GraphProvider.NEO4J)
    neo4j_driver.fulltext_syntax = "+"
    assert su.fulltext_query("hello world", ["g1", "g2"], neo4j_driver) == (
        '+group_id:"g1" OR +group_id:"g2" AND (sanitized:hello world)'
    )

    monkeypatch.setattr(su, "lucene_sanitize", lambda query: " ".join(["x"] * 200))
    assert su.fulltext_query("too long", ["g1"], neo4j_driver) == ""


def test_rrf_combines_rankings_and_applies_min_score() -> None:
    uuids, scores = su.rrf([["a", "b"], ["b", "a"], ["c"]], rank_const=1, min_score=1.0)
    assert set(uuids) == {"a", "b", "c"}
    assert scores[0] == pytest.approx(1.5)
    assert len(scores) == 3


def test_hybrid_node_search_deduplicates_and_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    node_a = FakeNode("a")
    node_b = FakeNode("b")
    node_c = FakeNode("c")

    async def fake_gather(*aws):
        return [
            [node_a, node_b],
            [node_b, node_c],
            [node_c],
        ]

    monkeypatch.setattr(su, "semaphore_gather", fake_gather)
    monkeypatch.setattr(su, "node_fulltext_search", lambda *args, **kwargs: None)
    monkeypatch.setattr(su, "node_similarity_search", lambda *args, **kwargs: None)

    result = run(
        su.hybrid_node_search(
            queries=["alpha", "beta"],
            embeddings=[[0.1, 0.2]],
            driver=FakeDriver(GraphProvider.NEO4J),
            search_filter=SearchFilters(),
            group_ids=["g1"],
            limit=2,
        )
    )

    assert [node.uuid for node in result] == ["b", "c", "a"]


def test_get_relevant_nodes_empty_and_kuzu_without_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run(
        su.get_relevant_nodes(
            FakeDriver(GraphProvider.NEO4J),
            [],
            SearchFilters(),
        )
    ) == []

    monkeypatch.setattr(su, "fulltext_query", lambda *args, **kwargs: "ft")
    assert run(
        su.get_relevant_nodes(
            FakeDriver(GraphProvider.KUZU),
            [FakeNode("a", name_embedding=None)],
            SearchFilters(),
        )
    ) == []


def test_get_relevant_nodes_maps_matches_back_to_input_order(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = FakeDriver(
        GraphProvider.NEO4J,
        execute_results=[
            (
                [
                    {"search_node_uuid": "n1", "matches": [{"uuid": "m1"}, {"uuid": "m2"}]},
                    {"search_node_uuid": "n2", "matches": [{"uuid": "m3"}]},
                ],
                [],
                None,
            )
        ],
    )
    monkeypatch.setattr(su, "fulltext_query", lambda *args, **kwargs: "ft")
    monkeypatch.setattr(su, "node_search_filter_query_constructor", lambda *args, **kwargs: ([], {}))
    monkeypatch.setattr(
        su,
        "get_entity_node_from_record",
        lambda record, provider: FakeNode(record["uuid"]),
    )
    monkeypatch.setattr(su, "get_vector_cosine_func_query", lambda *args, **kwargs: "score_fn")
    monkeypatch.setattr(su, "get_nodes_query", lambda *args, **kwargs: "NODE_QUERY ")

    nodes = [
        FakeNode("n1", name="one", name_embedding=[1.0, 0.0]),
        FakeNode("n2", name="two", name_embedding=[0.0, 1.0]),
    ]
    result = run(su.get_relevant_nodes(driver, nodes, SearchFilters(), limit=2))

    assert [[match.uuid for match in matches] for matches in result] == [["m1", "m2"], ["m3"]]
    assert driver.calls


def test_get_mentioned_nodes_uses_graph_ops_then_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    episodes = [SimpleNamespace(uuid="e1"), SimpleNamespace(uuid="e2")]
    graph_ops = SimpleNamespace(
        get_mentioned_nodes=lambda driver, episodes: (_ for _ in ()).throw(NotImplementedError())
    )
    driver = FakeDriver(
        GraphProvider.NEO4J,
        graph_operations_interface=graph_ops,
        execute_results=[([{"uuid": "n1"}, {"uuid": "n2"}], [], None)],
    )
    monkeypatch.setattr(
        su,
        "get_entity_node_from_record",
        lambda record, provider: FakeNode(record["uuid"]),
    )
    monkeypatch.setattr(su, "get_entity_node_return_query", lambda provider: " RETURN_NODE")

    result = run(su.get_mentioned_nodes(driver, episodes))

    assert [node.uuid for node in result] == ["n1", "n2"]
    assert driver.calls[0][1]["uuids"] == ["e1", "e2"]


def test_edge_fulltext_search_returns_empty_for_blank_or_empty_aoss(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = FakeDriver(GraphProvider.NEPTUNE)
    monkeypatch.setattr(su, "fulltext_query", lambda *args, **kwargs: "")
    assert run(su.edge_fulltext_search(driver, "query", SearchFilters(), ["g1"])) == []

    monkeypatch.setattr(su, "fulltext_query", lambda *args, **kwargs: "ft")
    assert run(su.edge_fulltext_search(driver, "query", SearchFilters(), ["g1"])) == []


def test_edge_fulltext_search_neptune_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = FakeDriver(
        GraphProvider.NEPTUNE,
        aoss_result={
            "hits": {
                "total": {"value": 2},
                "hits": [
                    {"_source": {"uuid": "e1"}, "_score": 4.0},
                    {"_source": {"uuid": "e2"}, "_score": 2.5},
                ],
            }
        },
        execute_results=[([{"uuid": "edge-1"}, {"uuid": "edge-2"}], [], None)],
    )
    monkeypatch.setattr(su, "fulltext_query", lambda *args, **kwargs: "ft")
    monkeypatch.setattr(su, "edge_search_filter_query_constructor", lambda *args, **kwargs: (["e.flag = true"], {"flag": True}))
    monkeypatch.setattr(
        su,
        "get_entity_edge_from_record",
        lambda record, provider: FakeEdge(record["uuid"]),
    )

    result = run(su.edge_fulltext_search(driver, "query", SearchFilters(), ["g1"], limit=2))

    assert [edge.uuid for edge in result] == ["edge-1", "edge-2"]
    assert driver.aoss_calls == [("edge_name_and_fact", "query", None)]
    assert driver.calls[0][1]["ids"] == [{"id": "e1", "score": 4.0}, {"id": "e2", "score": 2.5}]


def test_edge_similarity_search_neptune_filters_by_cosine_score(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = FakeDriver(
        GraphProvider.NEPTUNE,
        execute_results=[
            (
                [
                    {"id": 1, "embedding": "1.0,0.0"},
                    {"id": 2, "embedding": "0.0,1.0"},
                    {"id": 3, "embedding": None},
                ],
                [],
                None,
            ),
            ([{"uuid": "edge-1"}], [], None),
        ],
    )
    monkeypatch.setattr(su, "edge_search_filter_query_constructor", lambda *args, **kwargs: ([], {}))
    monkeypatch.setattr(
        su,
        "get_entity_edge_from_record",
        lambda record, provider: FakeEdge(record["uuid"]),
    )

    result = run(
        su.edge_similarity_search(
            driver,
            search_vector=[1.0, 0.0],
            source_node_uuid="source",
            target_node_uuid="target",
            search_filter=SearchFilters(),
            group_ids=None,
            min_score=0.5,
        )
    )

    assert [edge.uuid for edge in result] == ["edge-1"]
    assert driver.calls[1][1]["ids"] == [{"id": 1, "score": pytest.approx(1.0)}]
    assert "source_uuid" not in driver.calls[0][1]
    assert "target_uuid" not in driver.calls[0][1]


def test_edge_bfs_search_early_return_and_kuzu_multi_query(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run(
        su.edge_bfs_search(
            FakeDriver(GraphProvider.NEO4J),
            [],
            2,
            SearchFilters(),
        )
    ) == []

    driver = FakeDriver(
        GraphProvider.KUZU,
        execute_results=[
            ([{"uuid": "edge-1"}], [], None),
            ([{"uuid": "edge-2"}], [], None),
        ],
    )
    monkeypatch.setattr(su, "edge_search_filter_query_constructor", lambda *args, **kwargs: ([], {}))
    monkeypatch.setattr(
        su,
        "get_entity_edge_from_record",
        lambda record, provider: FakeEdge(record["uuid"]),
    )
    monkeypatch.setattr(su, "get_entity_edge_return_query", lambda provider: " EDGE_RETURN")

    result = run(
        su.edge_bfs_search(
            driver,
            ["n1"],
            2,
            SearchFilters(),
            group_ids=["g1"],
            limit=5,
        )
    )

    assert [edge.uuid for edge in result] == ["edge-1", "edge-2"]
    assert len(driver.calls) == 2


def test_node_fulltext_search_blank_and_neptune_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = FakeDriver(GraphProvider.NEPTUNE)
    monkeypatch.setattr(su, "fulltext_query", lambda *args, **kwargs: "")
    assert run(su.node_fulltext_search(driver, "query", SearchFilters(), ["g1"])) == []

    driver = FakeDriver(
        GraphProvider.NEPTUNE,
        aoss_result={
            "hits": {
                "total": {"value": 1},
                "hits": [{"_source": {"uuid": "n1"}, "_score": 8.0}],
            }
        },
        execute_results=[([{"uuid": "node-1"}], [], None)],
    )
    monkeypatch.setattr(su, "fulltext_query", lambda *args, **kwargs: "ft")
    monkeypatch.setattr(su, "node_search_filter_query_constructor", lambda *args, **kwargs: (["n.kind = 'x'"], {"kind": "x"}))
    monkeypatch.setattr(
        su,
        "get_entity_node_from_record",
        lambda record, provider: FakeNode(record["uuid"]),
    )
    monkeypatch.setattr(su, "get_entity_node_return_query", lambda provider: " NODE_RETURN")

    result = run(su.node_fulltext_search(driver, "query", SearchFilters(), ["g1"], limit=3))

    assert [node.uuid for node in result] == ["node-1"]
    assert driver.calls[0][1]["ids"] == [{"id": "n1", "score": 8.0}]


def test_node_similarity_search_neptune_returns_only_above_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = FakeDriver(
        GraphProvider.NEPTUNE,
        execute_results=[
            (
                [
                    {"id": 1, "embedding": "1.0,0.0"},
                    {"id": 2, "embedding": "0.0,1.0"},
                ],
                [],
                None,
            ),
            ([{"uuid": "node-1"}], [], None),
        ],
    )
    monkeypatch.setattr(su, "node_search_filter_query_constructor", lambda *args, **kwargs: ([], {}))
    monkeypatch.setattr(
        su,
        "get_entity_node_from_record",
        lambda record, provider: FakeNode(record["uuid"]),
    )
    monkeypatch.setattr(su, "get_entity_node_return_query", lambda provider: " NODE_RETURN")

    result = run(
        su.node_similarity_search(
            driver,
            search_vector=[1.0, 0.0],
            search_filter=SearchFilters(),
            min_score=0.5,
        )
    )

    assert [node.uuid for node in result] == ["node-1"]
    assert driver.calls[1][1]["ids"] == [{"id": 1, "score": pytest.approx(1.0)}]


def test_node_bfs_search_covers_early_return_and_kuzu_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run(
        su.node_bfs_search(
            FakeDriver(GraphProvider.NEO4J),
            None,
            SearchFilters(),
            bfs_max_depth=1,
        )
    ) == []
    assert run(
        su.node_bfs_search(
            FakeDriver(GraphProvider.NEO4J),
            ["n1"],
            SearchFilters(),
            bfs_max_depth=0,
        )
    ) == []

    driver = FakeDriver(
        GraphProvider.KUZU,
        execute_results=[
            ([{"uuid": "n1"}], [], None),
            ([{"uuid": "n2"}], [], None),
            ([{"uuid": "n3"}], [], None),
        ],
    )
    monkeypatch.setattr(su, "node_search_filter_query_constructor", lambda *args, **kwargs: ([], {}))
    monkeypatch.setattr(
        su,
        "get_entity_node_from_record",
        lambda record, provider: FakeNode(record["uuid"]),
    )
    monkeypatch.setattr(su, "get_entity_node_return_query", lambda provider: " NODE_RETURN")

    result = run(
        su.node_bfs_search(
            driver,
            ["origin-1"],
            SearchFilters(),
            bfs_max_depth=2,
            group_ids=["g1"],
            limit=5,
        )
    )

    assert [node.uuid for node in result] == ["n1", "n2", "n3"]
    assert len(driver.calls) == 3
