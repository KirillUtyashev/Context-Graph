from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from context_graph.storage.backend.errors import (
    BackendConfigurationError,
    BackendFeatureNotImplementedError,
    UnsupportedCapabilityError,
)
from context_graph.storage.backend.factory import create_driver
import context_graph.storage.backend.schema as backend_schema
from context_graph.storage.backend.mutations import (
    EdgeRecord,
    MutationApplyResult,
    MutationPlan,
    NodeRecord,
    SearchDocumentRecord,
    merge_mutation_plans,
)
from context_graph.storage.backend.neo4j.config import Neo4jConfig
from context_graph.storage.backend.neptune.config import NeptuneConfig
from context_graph.storage.backend.types import (
    GraphRecordRef,
    RecordKind,
    SearchMode,
    SearchQuery,
    TraversalQuery,
)


def test_search_query_and_traversal_query_validate_inputs() -> None:
    lexical = SearchQuery(mode=SearchMode.LEXICAL, text="needle", limit=2)
    vector = SearchQuery(mode=SearchMode.VECTOR, embedding=(0.1, 0.2))
    hybrid = SearchQuery(mode=SearchMode.HYBRID, text="needle")

    assert lexical.limit == 2
    assert vector.embedding == (0.1, 0.2)
    assert hybrid.text == "needle"

    with pytest.raises(ValueError, match="lexical search requires text"):
        SearchQuery(mode=SearchMode.LEXICAL, text="")
    with pytest.raises(ValueError, match="vector search requires an embedding"):
        SearchQuery(mode=SearchMode.VECTOR)
    with pytest.raises(ValueError, match="hybrid search requires text, an embedding, or both"):
        SearchQuery(mode=SearchMode.HYBRID)
    with pytest.raises(ValueError, match="limit must be >= 1"):
        SearchQuery(mode=SearchMode.LEXICAL, text="needle", limit=0)

    origin = GraphRecordRef(RecordKind.NODE, "node-1", "code_node", repo_id="repo-1")
    traversal = TraversalQuery(origin_refs=(origin,), max_depth=2, direction="outbound", limit=4)
    assert traversal.max_depth == 2

    with pytest.raises(ValueError, match="origin_refs must not be empty"):
        TraversalQuery(origin_refs=(), max_depth=1)
    with pytest.raises(ValueError, match="max_depth must be >= 1"):
        TraversalQuery(origin_refs=(origin,), max_depth=0)
    with pytest.raises(ValueError, match="direction must be one of inbound, outbound, both"):
        TraversalQuery(origin_refs=(origin,), max_depth=1, direction="sideways")
    with pytest.raises(ValueError, match="limit must be >= 1"):
        TraversalQuery(origin_refs=(origin,), max_depth=1, limit=0)


def test_mutation_records_normalize_properties_and_merge_plans() -> None:
    node = NodeRecord(
        record_id="node-1",
        record_type="code_node",
        repo_id="repo-1",
        group_id="repo-1",
        labels=("CodeNode",),
        properties={"name": "main.py"},
    )
    edge = EdgeRecord(
        record_id="edge-1",
        record_type="calls",
        repo_id="repo-1",
        group_id="repo-1",
        relationship_type="CALLS",
        source_record_id="node-1",
        target_record_id="node-2",
        properties={"weight": 2},
    )
    document = SearchDocumentRecord(
        record_kind=RecordKind.NODE,
        record_id="node-1",
        record_type="code_node",
        repo_id="repo-1",
        group_id="repo-1",
        search_text="main.py main",
        properties={"kind": "File"},
    )

    assert node.normalized_properties()["record_id"] == "node-1"
    assert edge.normalized_properties()["record_type"] == "calls"
    assert document.normalized_properties()["record_kind"] == "node"

    first = MutationPlan(upsert_nodes=(node,), upsert_edges=(edge,), upsert_search_documents=(document,))
    second = MutationPlan(
        upsert_nodes=(
            NodeRecord(
                record_id=node.record_id,
                record_type=node.record_type,
                repo_id=node.repo_id,
                group_id=node.group_id,
                labels=node.labels,
                properties={"name": "updated.py"},
            ),
        ),
        upsert_edges=(edge,),
        upsert_search_documents=(document,),
    )
    merged = merge_mutation_plans(first, second)

    assert first.operation_count() == 3
    assert not first.is_empty()
    assert len(merged.upsert_nodes) == 1
    assert merged.upsert_nodes[0].properties["name"] == "updated.py"

    result = MutationApplyResult(nodes_upserted=2, edges_upserted=3, search_documents_upserted=4)
    assert result.operations_applied == 9


def test_create_driver_dispatches_by_config_type() -> None:
    neo4j_module = SimpleNamespace(Neo4jBackendDriver=lambda config: ("neo4j", config))
    neptune_module = SimpleNamespace(NeptuneBackendDriver=lambda config: ("neptune", config))

    with patch.dict(
        "sys.modules",
        {
            "context_graph.storage.backend.neo4j.driver": neo4j_module,
            "context_graph.storage.backend.neptune.driver": neptune_module,
        },
    ):
        neo4j_config = Neo4jConfig(uri="bolt://localhost", user="neo4j", password="secret")
        neptune_config = NeptuneConfig(host="example.test")

        assert create_driver(neo4j_config) == ("neo4j", neo4j_config)
        assert create_driver(neptune_config) == ("neptune", neptune_config)

    with pytest.raises(TypeError, match="unsupported backend config"):
        create_driver(object())  # type: ignore[arg-type]


def test_backend_error_types_preserve_messages() -> None:
    assert str(BackendConfigurationError("bad config")) == "bad config"
    assert str(UnsupportedCapabilityError("missing feature")) == "missing feature"
    assert str(BackendFeatureNotImplementedError("todo")) == "todo"


def test_backend_schema_constants_remain_stable() -> None:
    assert backend_schema.SEARCH_DOCUMENT_TEXT_PROPERTY == "search_text"
    assert backend_schema.SEARCH_DOCUMENT_ID_PROPERTY == "record_id"
    assert backend_schema.SEARCH_DOCUMENT_TYPE_PROPERTY == "record_type"
    assert backend_schema.SEARCH_DOCUMENT_REPO_ID_PROPERTY == "repo_id"
    assert backend_schema.SEARCH_DOCUMENT_GROUP_ID_PROPERTY == "group_id"
    assert backend_schema.GRAPH_RECORD_ID_PROPERTY == "record_id"
    assert backend_schema.GRAPH_RECORD_TYPE_PROPERTY == "record_type"
    assert backend_schema.GRAPH_REPO_ID_PROPERTY == "repo_id"
    assert backend_schema.GRAPH_GROUP_ID_PROPERTY == "group_id"
    assert backend_schema.NODE_SEARCH_DOCUMENT_LABEL == "NodeSearchDocument"
    assert backend_schema.EDGE_SEARCH_DOCUMENT_LABEL == "EdgeSearchDocument"
    assert backend_schema.NEO4J_NODE_SEARCH_INDEX == "backend_node_search_documents"
    assert backend_schema.NEO4J_EDGE_SEARCH_INDEX == "backend_edge_search_documents"
    assert backend_schema.NEO4J_GRAPH_NODE_RECORD_ID_INDEX == "backend_graph_node_record_id"
    assert backend_schema.NEO4J_GRAPH_EDGE_RECORD_ID_INDEX == "backend_graph_edge_record_id"
    assert backend_schema.NEPTUNE_NODE_AOSS_INDEX == "backend_node_documents"
    assert backend_schema.NEPTUNE_EDGE_AOSS_INDEX == "backend_edge_documents"
