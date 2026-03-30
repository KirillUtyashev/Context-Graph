from __future__ import annotations

import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from context_graph.backend.maintenance import ScopeResetRequest
from context_graph.backend.mutations import EdgeRecord, MutationPlan, NodeRecord, SearchDocumentRecord
from context_graph.backend.types import GraphRecordRef, RecordKind, SearchMode, SearchQuery, TraversalQuery


class RecordingDriver:
    def __init__(self, responses=None, *, aoss_client=None):
        self.responses = list(responses or [])
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.aoss_client = aoss_client

    async def execute_query(self, query: str, **kwargs):
        self.calls.append((query, kwargs))
        if self.responses:
            return self.responses.pop(0)
        return []


class FakeAossIndices:
    def __init__(self, exists_map=None):
        self.exists_map = dict(exists_map or {})
        self.exists_calls: list[str] = []
        self.deleted: list[str] = []
        self.created: list[tuple[str, dict[str, object]]] = []

    def exists(self, *, index: str) -> bool:
        self.exists_calls.append(index)
        return self.exists_map.get(index, False)

    def delete(self, *, index: str) -> None:
        self.deleted.append(index)

    def create(self, *, index: str, body: dict[str, object]) -> None:
        self.created.append((index, body))


class FakeAossClient:
    def __init__(self, exists_map=None):
        self.indices = FakeAossIndices(exists_map)
        self.index_calls: list[dict[str, object]] = []
        self.search_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []

    def index(self, **kwargs) -> None:
        self.index_calls.append(kwargs)

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return {"hits": {"hits": []}}

    def delete_by_query(self, **kwargs):
        self.delete_calls.append(kwargs)
        return {"deleted": 1}


@pytest.mark.asyncio
async def test_neo4j_driver_build_indices_reraises_unexpected_schema_errors() -> None:
    from neo4j.exceptions import ClientError
    from context_graph.backend.neo4j.config import Neo4jConfig
    from context_graph.backend.neo4j.driver import Neo4jBackendDriver

    driver = Neo4jBackendDriver(Neo4jConfig(uri="bolt://localhost:7687", user="neo4j", password="pw"))

    async def fail(_query, **_kwargs):
        raise ClientError("DifferentSchemaError")

    driver.execute_query = fail
    with pytest.raises(ClientError):
        await driver.build_indices()


@pytest.mark.asyncio
async def test_neo4j_transaction_handles_awaitable_cursor_data() -> None:
    from context_graph.backend.neo4j.driver import _Neo4jTransaction

    async def fake_data():
        return [{"ok": True}]

    async def run(_query, _kwargs):
        return SimpleNamespace(data=lambda: fake_data())

    tx = SimpleNamespace(run=run)
    result = await _Neo4jTransaction(tx).run("RETURN 1")
    assert result == [{"ok": True}]


@pytest.mark.asyncio
async def test_neo4j_maintenance_helpers_cover_scope_and_empty_counts() -> None:
    from context_graph.backend.neo4j import maintenance as neo_maintenance

    driver = RecordingDriver(
        [
            [{"count": 2}],
            [{"count": 3}],
            [{"count": 4}],
            [],
            [],
            [],
        ]
    )
    ops = neo_maintenance.Neo4jMaintenanceOperations(driver)
    result = await ops.reset_scope(
        ScopeResetRequest(
            repo_id="repo-1",
            node_record_types=("node-type",),
            edge_record_types=("edge-type",),
            search_document_record_types=("search-type",),
            delete_search_documents=True,
        )
    )
    assert result.nodes_deleted == 2
    assert result.edges_deleted == 3
    assert result.search_documents_deleted == 4
    assert neo_maintenance._count_from_records([]) == 0
    assert neo_maintenance._type_filter(alias="n", record_types=()) == ""
    assert "IN $record_types" in neo_maintenance._type_filter(alias="n", record_types=("x",))


@pytest.mark.asyncio
async def test_neo4j_mutation_search_and_traversal_helpers_cover_remaining_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from context_graph.backend.neo4j import mutations as neo_mutations
    from context_graph.backend.neo4j import search as neo_search
    from context_graph.backend.neo4j import traversal as neo_traversal

    driver = RecordingDriver()
    ops = neo_mutations.Neo4jMutationOperations(driver)
    monkeypatch.setattr(neo_mutations, "_group_nodes", lambda records: {("Label",): []})
    monkeypatch.setattr(neo_mutations, "_group_edges", lambda records: {"REL": []})
    monkeypatch.setattr(neo_mutations, "_group_search_documents", lambda records: {RecordKind.NODE: []})
    assert await ops.apply(MutationPlan()) == neo_mutations.MutationApplyResult(0, 0, 0)
    assert neo_mutations._sanitize_relationship_type("1 rel") == "R_1_REL"
    with pytest.raises(ValueError):
        neo_mutations._sanitize_relationship_type("!!!")
    assert neo_mutations._sanitize_node_label("1 label") == "N_1_label"
    with pytest.raises(ValueError):
        neo_mutations._sanitize_node_label("!!!")

    search_driver = RecordingDriver(
        [
            [{"record_id": "n1", "record_type": "node"}],
            [{"record_id": "e1", "record_type": "edge"}],
        ]
    )
    search_ops = neo_search.Neo4jSearchOperations(search_driver)
    refs = [
        GraphRecordRef(RecordKind.NODE, "n1", "node"),
        GraphRecordRef(RecordKind.EDGE, "e1", "edge"),
    ]
    assert await search_ops.fetch(refs) == [
        {"record_id": "n1", "record_type": "node"},
        {"record_id": "e1", "record_type": "edge"},
    ]
    assert await search_ops.fetch([]) == []
    where_clauses: list[str] = []
    params: dict[str, object] = {}
    neo_search._append_list_filter(where_clauses, params, field="repo_id", param_name="repo_ids", values=None)
    neo_search._append_extra_filters(where_clauses, params, {"status": ["open", "closed"]})
    assert where_clauses == ["doc.status IN $filter_status"]
    assert params["filter_status"] == ["open", "closed"]

    assert neo_traversal._path_pattern(2, "outbound").endswith("->(neighbor:BackendRecord)")
    assert neo_traversal._path_pattern(2, "both").endswith("]-(neighbor:BackendRecord)")
    assert neo_traversal._coerce_hits([{"record_id": "", "record_kind": "node"}]) == []
    assert neo_traversal._dedupe_text(["a", "a", " ", None]) == ("a",)


def test_neptune_driver_helpers_cover_reload_db_path_and_aoss_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("context_graph.backend.neptune.driver")
    importlib.reload(module)

    config = module.NeptuneConfig(host="neptune-db://example.cluster", port=8182)
    driver = module.NeptuneBackendDriver.__new__(module.NeptuneBackendDriver)
    monkeypatch.setattr(module, "NeptuneGraph", lambda endpoint, port: ("db", endpoint, port))
    monkeypatch.setattr(module, "NeptuneAnalyticsGraph", object())
    assert module.NeptuneBackendDriver._create_neptune_client(driver, config) == (
        "db",
        "example.cluster",
        8182,
    )

    monkeypatch.setattr(module, "boto3", None)
    monkeypatch.setattr(module, "OpenSearch", None)
    monkeypatch.setattr(module, "Urllib3AWSV4SignerAuth", None)
    monkeypatch.setattr(module, "Urllib3HttpConnection", None)
    with pytest.raises(module.BackendConfigurationError, match="AOSS dependencies are not installed"):
        module.NeptuneBackendDriver._create_aoss_client(
            driver,
            module.NeptuneConfig(host="neptune-graph://graph-id", aoss_host="search.example.com"),
        )


@pytest.mark.asyncio
async def test_neptune_driver_build_indices_and_empty_aoss_query() -> None:
    from context_graph.backend.neptune import driver as neptune_driver

    driver = neptune_driver.NeptuneBackendDriver.__new__(neptune_driver.NeptuneBackendDriver)
    driver.aoss_client = FakeAossClient(
        {
            neptune_driver.NEPTUNE_NODE_AOSS_INDEX: True,
            neptune_driver.NEPTUNE_EDGE_AOSS_INDEX: False,
        }
    )
    driver.client = SimpleNamespace(client=SimpleNamespace(close=lambda: None))
    await neptune_driver.NeptuneBackendDriver.build_indices(driver, delete_existing=True)
    assert neptune_driver.NEPTUNE_NODE_AOSS_INDEX in driver.aoss_client.indices.deleted
    assert any(index == neptune_driver.NEPTUNE_EDGE_AOSS_INDEX for index, _ in driver.aoss_client.indices.created)
    driver.aoss_client = None
    assert await neptune_driver.NeptuneBackendDriver._run_aoss_query(
        driver,
        index_name="index",
        query=SearchQuery(mode=SearchMode.LEXICAL, text="hello"),
    ) == {}


@pytest.mark.asyncio
async def test_neptune_maintenance_mutation_search_and_traversal_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from context_graph.backend.neptune import maintenance as neptune_maintenance
    from context_graph.backend.neptune import mutations as neptune_mutations
    from context_graph.backend.neptune import search as neptune_search
    from context_graph.backend.neptune import traversal as neptune_traversal

    driver = RecordingDriver([[{"count": 1}], [{"count": 2}]])
    driver.aoss_client = None
    maintenance_ops = neptune_maintenance.NeptuneMaintenanceOperations(driver)
    repo_result = await maintenance_ops.reset_repo("repo-1")
    assert repo_result.total_deleted == 3
    assert neptune_maintenance._count_from_records([]) == 0
    assert neptune_maintenance._type_filter(()) == ""

    fake_not_found = type("NotFoundError", (Exception,), {})
    with pytest.MonkeyPatch.context() as local_patch:
        local_patch.setitem(sys.modules, "opensearchpy.exceptions", SimpleNamespace(NotFoundError=fake_not_found))
        aoss_driver = RecordingDriver()
        aoss_driver.aoss_client = FakeAossClient()
        aoss_driver.aoss_client.delete_by_query = MagicMock(side_effect=[{"deleted": 2}, fake_not_found("missing")])
        ops = neptune_maintenance.NeptuneMaintenanceOperations(aoss_driver)
        assert ops._delete_search_documents(repo_id="repo-1", record_types=("type",)) == 2

    mutation_driver = RecordingDriver(aoss_client=None)
    mutation_ops = neptune_mutations.NeptuneMutationOperations(mutation_driver)
    monkeypatch.setattr(neptune_mutations, "_group_nodes", lambda records: {("Label",): []})
    monkeypatch.setattr(neptune_mutations, "_group_edges", lambda records: {"REL": []})
    assert await mutation_ops.apply(MutationPlan()) == neptune_mutations.MutationApplyResult(0, 0, 0)
    assert neptune_mutations._sanitize_relationship_type("1 rel") == "R_1_REL"
    with pytest.raises(ValueError):
        neptune_mutations._sanitize_relationship_type("!!!")
    assert neptune_mutations._sanitize_node_label("1 label") == "N_1_label"
    with pytest.raises(ValueError):
        neptune_mutations._sanitize_node_label("!!!")

    search_driver = RecordingDriver(
        [
            [{"record_id": "n1", "record_type": "node"}],
            [{"record_id": "e1", "record_type": "edge"}],
        ],
        aoss_client=FakeAossClient(),
    )
    search_ops = neptune_search.NeptuneSearchOperations(search_driver)
    refs = [
        GraphRecordRef(RecordKind.NODE, "n1", "node"),
        GraphRecordRef(RecordKind.EDGE, "e1", "edge"),
    ]
    assert await search_ops.fetch(refs) == [
        {"record_id": "n1", "record_type": "node"},
        {"record_id": "e1", "record_type": "edge"},
    ]
    assert await search_ops.fetch([]) == []
    with pytest.raises(neptune_search.UnsupportedCapabilityError):
        await neptune_search.NeptuneSearchOperations(RecordingDriver(aoss_client=None)).search(
            SearchQuery(mode=SearchMode.LEXICAL, text="hello")
        )
    query_body = neptune_search.build_aoss_query(
        SearchQuery(
            mode=SearchMode.LEXICAL,
            text="hello",
            filters={"status": ["open"], "owner": "alice"},
        )
    )
    assert {"terms": {"status": ["open"]}} in query_body["query"]["bool"]["filter"]
    assert {"term": {"owner": "alice"}} in query_body["query"]["bool"]["filter"]

    assert neptune_traversal._path_pattern(2, "inbound").startswith("(origin)<-")
    assert neptune_traversal._path_pattern(2, "both").endswith("]-(neighbor:BackendRecord)")
    assert neptune_traversal._coerce_hits([{"record_id": "", "record_kind": "node"}]) == []
    assert neptune_traversal._dedupe_text(["a", "a", " ", None]) == ("a",)
