from __future__ import annotations

import unittest

from context_graph.diffing import diff_snapshots
from context_graph.graph_sinks import NeptuneGraphSink, Neo4jGraphSink
from context_graph.metadata import InMemoryMetadataStore, SQLiteMetadataStore
from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.snapshot_builder import (
    build_snapshot_from_extract_summary,
    build_snapshot_from_graph_export,
)
from context_graph.temporal import TemporalGraphWriter


def make_snapshot(revision_id: str, *, include_extra_call: bool = False) -> GraphSnapshot:
    node_repo = CanonicalNode("project:demo", "demo", "Project", "demo", qualified_name="demo")
    node_file = CanonicalNode(
        "file:demo:demo.app.__file__",
        "demo",
        "File",
        "app.py",
        qualified_name="demo.app.__file__",
        file_path="app.py",
    )
    node_func = CanonicalNode(
        "symbol:demo:demo.app.main",
        "demo",
        "Function",
        "main",
        qualified_name="demo.app.main",
        file_path="app.py",
        start_line=1,
        end_line=5,
        properties={"version": revision_id},
    )
    node_helper = CanonicalNode(
        "symbol:demo:demo.app.helper",
        "demo",
        "Function",
        "helper",
        qualified_name="demo.app.helper",
        file_path="app.py",
        start_line=7,
        end_line=8,
    )
    edges = [
        CanonicalEdge(
            "edge:demo:CONTAINS_FILE:project:demo:file:demo:demo.app.__file__",
            "demo",
            "CONTAINS_FILE",
            node_repo.node_id,
            node_file.node_id,
        ),
        CanonicalEdge(
            "edge:demo:DEFINES:file:demo:demo.app.__file__:symbol:demo:demo.app.main",
            "demo",
            "DEFINES",
            node_file.node_id,
            node_func.node_id,
        ),
    ]
    if include_extra_call:
        edges.append(
            CanonicalEdge(
                "edge:demo:CALLS:symbol:demo:demo.app.main:symbol:demo:demo.app.helper",
                "demo",
                "CALLS",
                node_func.node_id,
                node_helper.node_id,
                properties={"callee_name": "helper"},
            )
        )
        nodes = (node_repo, node_file, node_func, node_helper)
    else:
        nodes = (node_repo, node_file, node_func)
    return GraphSnapshot(
        repo_id="demo",
        revision_id=revision_id,
        root_path="/tmp/demo",
        generated_at="2026-03-20T00:00:00+00:00",
        nodes=nodes,
        edges=tuple(edges),
        metadata={},
    )


class SnapshotBuilderTests(unittest.TestCase):
    def test_build_snapshot_from_extract_summary(self) -> None:
        summary = {
            "repo_path": "/tmp/repo",
            "discovered_files": 1,
            "files": [
                {
                    "rel_path": "app.py",
                    "language": "Python",
                    "supported": True,
                    "has_error": False,
                    "error": "",
                    "size": 120,
                    "is_test_file": False,
                    "defs": [
                        {
                            "label": "Class",
                            "name": "Greeter",
                            "qualified_name": "demo.app.Greeter",
                            "parent_class": None,
                            "start_line": 1,
                            "end_line": 10,
                            "signature": None,
                            "complexity": 0,
                            "lines": 0,
                            "is_test": False,
                            "is_entry_point": False,
                        },
                        {
                            "label": "Module",
                            "name": "app.py",
                            "qualified_name": "demo.app",
                            "parent_class": None,
                            "start_line": 1,
                            "end_line": 10,
                            "signature": None,
                            "complexity": 0,
                            "lines": 0,
                            "is_test": False,
                            "is_entry_point": False,
                        },
                        {
                            "label": "Function",
                            "name": "main",
                            "qualified_name": "demo.app.main",
                            "parent_class": None,
                            "start_line": 2,
                            "end_line": 4,
                            "signature": "()",
                            "complexity": 1,
                            "lines": 2,
                            "is_test": False,
                            "is_entry_point": True,
                        },
                        {
                            "label": "Method",
                            "name": "hello",
                            "qualified_name": "demo.app.Greeter.hello",
                            "parent_class": "demo.app.Greeter",
                            "start_line": 5,
                            "end_line": 6,
                            "signature": "(self)",
                            "complexity": 1,
                            "lines": 1,
                            "is_test": False,
                            "is_entry_point": False,
                        },
                    ],
                    "calls": [{"callee_name": "main", "enclosing_func_qn": "demo.app.main"}],
                    "imports": [],
                }
            ],
            "totals": {"processed": 1, "supported": 1},
        }

        snapshot = build_snapshot_from_extract_summary(summary, repo_id="demo", revision_id="abc123")
        self.assertEqual(snapshot.repo_id, "demo")
        self.assertEqual(snapshot.revision_id, "abc123")
        self.assertGreaterEqual(len(snapshot.nodes), 5)
        edge_types = {edge.edge_type for edge in snapshot.edges}
        self.assertIn("CONTAINS_FILE", edge_types)
        self.assertIn("DEFINES", edge_types)
        self.assertIn("DEFINES_METHOD", edge_types)

    def test_build_snapshot_keeps_hidden_folder_ancestors(self) -> None:
        summary = {
            "repo_path": "/tmp/repo",
            "discovered_files": 1,
            "files": [
                {
                    "rel_path": ".github/workflows/deploy.yml",
                    "language": "YAML",
                    "supported": False,
                    "has_error": True,
                    "error": "no tree-sitter grammar",
                    "module_qn": None,
                    "size": 20,
                    "is_test_file": False,
                    "defs": [],
                    "calls": [],
                    "imports": [],
                }
            ],
            "totals": {"processed": 1, "supported": 0},
        }

        snapshot = build_snapshot_from_extract_summary(summary, repo_id="demo", revision_id="abc123")
        folder_names = {node.name for node in snapshot.nodes if node.kind == "Folder"}
        edge_types = {edge.edge_type for edge in snapshot.edges}

        self.assertIn(".github", folder_names)
        self.assertIn("workflows", folder_names)
        self.assertIn("CONTAINS_FOLDER", edge_types)
        self.assertIn("CONTAINS_FILE", edge_types)

    def test_build_snapshot_from_graph_export_preserves_generic_edge_types(self) -> None:
        export_payload = {
            "project": "demo",
            "root_path": "/tmp/repo",
            "node_count": 4,
            "edge_count": 3,
            "nodes": [
                {
                    "temp_id": 1,
                    "label": "Project",
                    "name": "demo",
                    "qualified_name": "demo",
                    "file_path": None,
                    "start_line": 0,
                    "end_line": 0,
                    "properties_json": "{}",
                },
                {
                    "temp_id": 2,
                    "label": "File",
                    "name": "app.py",
                    "qualified_name": "demo.app.__file__",
                    "file_path": "app.py",
                    "start_line": 0,
                    "end_line": 0,
                    "properties_json": "{\"language\":\"Python\"}",
                },
                {
                    "temp_id": 3,
                    "label": "Function",
                    "name": "main",
                    "qualified_name": "demo.app.main",
                    "file_path": "app.py",
                    "start_line": 1,
                    "end_line": 4,
                    "properties_json": "{\"signature\":\"()\"}",
                },
                {
                    "temp_id": 4,
                    "label": "File",
                    "name": "test_app.py",
                    "qualified_name": "demo.test_app.__file__",
                    "file_path": "test_app.py",
                    "start_line": 0,
                    "end_line": 0,
                    "properties_json": "{}",
                },
            ],
            "edges": [
                {
                    "temp_id": 11,
                    "source_temp_id": 1,
                    "target_temp_id": 2,
                    "type": "CONTAINS_FILE",
                    "properties_json": "{}",
                },
                {
                    "temp_id": 12,
                    "source_temp_id": 2,
                    "target_temp_id": 3,
                    "type": "DEFINES",
                    "properties_json": "{}",
                },
                {
                    "temp_id": 13,
                    "source_temp_id": 4,
                    "target_temp_id": 3,
                    "type": "TESTS",
                    "properties_json": "{\"framework\":\"pytest\"}",
                },
            ],
        }

        snapshot = build_snapshot_from_graph_export(export_payload, repo_id="demo", revision_id="r1")

        self.assertEqual(snapshot.repo_id, "demo")
        self.assertEqual(snapshot.metadata["source"], "codebase-memory-mcp-graph-export")
        edge_types = {edge.edge_type for edge in snapshot.edges}
        self.assertIn("TESTS", edge_types)
        test_edge = next(edge for edge in snapshot.edges if edge.edge_type == "TESTS")
        self.assertEqual(test_edge.properties["framework"], "pytest")


class DiffAndWriterTests(unittest.TestCase):
    def test_diff_detects_changed_nodes_and_added_edges(self) -> None:
        before = make_snapshot("r1")
        after = make_snapshot("r2", include_extra_call=True)

        diff = diff_snapshots(before, after)
        self.assertIn("symbol:demo:demo.app.main", diff.changed_nodes)
        self.assertEqual(len(diff.added_edges), 1)
        self.assertEqual(len(diff.added_nodes), 1)

    def test_temporal_writer_records_and_updates_snapshot(self) -> None:
        metadata = InMemoryMetadataStore()
        writer = TemporalGraphWriter(metadata_store=metadata, graph_sink=Neo4jGraphSink())

        first = writer.apply_snapshot(make_snapshot("r1"), execute=False)
        second = writer.apply_snapshot(make_snapshot("r2", include_extra_call=True), execute=False)

        self.assertEqual(first.statements_executed, 0)
        self.assertFalse(first.diff.is_empty())
        self.assertEqual(second.diff.previous_revision_id, "r1")
        self.assertEqual(metadata.load_latest_snapshot("demo").revision_id, "r2")


class SinkPlanningTests(unittest.TestCase):
    def test_neo4j_and_neptune_generate_versioning_statements(self) -> None:
        before = make_snapshot("r1")
        after = make_snapshot("r2", include_extra_call=True)
        diff = diff_snapshots(before, after)

        neo4j_statements = Neo4jGraphSink().plan_apply_diff(after, diff)
        neptune_statements = NeptuneGraphSink().plan_apply_diff(after, diff)

        neo4j_queries = "\n".join(statement.query for statement in neo4j_statements)
        neptune_queries = "\n".join(statement.query for statement in neptune_statements)

        self.assertIn("MERGE (project:CodeNode:Project", neo4j_queries)
        self.assertIn("CREATE (v:CodeNodeVersion", neo4j_queries)
        self.assertIn("CREATE (s)-[r:CALLS", neo4j_queries)
        self.assertIn("MERGE (project:CodeNode:Project", neptune_queries)
        self.assertIn("CREATE (v:CodeNodeVersion", neptune_queries)
        self.assertIn("CREATE (s)-[r:CALLS", neptune_queries)


class MetadataStoreTests(unittest.TestCase):
    def test_sqlite_metadata_store_round_trips_snapshot(self) -> None:
        store = SQLiteMetadataStore(":memory:")
        snapshot = make_snapshot("r1", include_extra_call=True)

        store.save_latest_snapshot(snapshot)
        loaded = store.load_latest_snapshot("demo")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.revision_id, "r1")
        self.assertEqual(len(loaded.nodes), len(snapshot.nodes))
        self.assertEqual(len(loaded.edges), len(snapshot.edges))


if __name__ == "__main__":
    unittest.main()
