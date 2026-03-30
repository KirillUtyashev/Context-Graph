from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.query import code_graph as code_graph_queries


def make_code_graph_snapshot(root_path: str) -> GraphSnapshot:
    repo_id = "demo"
    project = CanonicalNode(
        "project:demo",
        repo_id,
        "Project",
        "demo",
        qualified_name="demo",
    )
    app_file = CanonicalNode(
        "file:demo:app",
        repo_id,
        "File",
        "app.py",
        qualified_name="demo.app.__file__",
        file_path="app.py",
    )
    caller_file = CanonicalNode(
        "file:demo:caller",
        repo_id,
        "File",
        "caller.py",
        qualified_name="demo.caller.__file__",
        file_path="caller.py",
    )
    main = CanonicalNode(
        "symbol:demo:demo.app.main",
        repo_id,
        "Function",
        "main",
        qualified_name="demo.app.main",
        file_path="app.py",
        start_line=1,
        end_line=2,
    )
    helper = CanonicalNode(
        "symbol:demo:demo.app.helper",
        repo_id,
        "Function",
        "helper",
        qualified_name="demo.app.helper",
        file_path="app.py",
        start_line=4,
        end_line=5,
    )
    helper_extra = CanonicalNode(
        "symbol:demo:demo.app.helper_extra",
        repo_id,
        "Function",
        "helper_extra",
        qualified_name="demo.app.helper_extra",
        file_path="app.py",
        start_line=7,
        end_line=8,
    )
    caller = CanonicalNode(
        "symbol:demo:demo.caller.run",
        repo_id,
        "Function",
        "run",
        qualified_name="demo.caller.run",
        file_path="caller.py",
        start_line=1,
        end_line=2,
    )
    edges = (
        CanonicalEdge("edge:project-app", repo_id, "CONTAINS_FILE", project.node_id, app_file.node_id),
        CanonicalEdge("edge:project-caller", repo_id, "CONTAINS_FILE", project.node_id, caller_file.node_id),
        CanonicalEdge("edge:defines-main", repo_id, "DEFINES", app_file.node_id, main.node_id),
        CanonicalEdge("edge:defines-helper", repo_id, "DEFINES", app_file.node_id, helper.node_id),
        CanonicalEdge("edge:defines-helper-extra", repo_id, "DEFINES", app_file.node_id, helper_extra.node_id),
        CanonicalEdge("edge:defines-caller", repo_id, "DEFINES", caller_file.node_id, caller.node_id),
        CanonicalEdge("edge:calls-main-helper", repo_id, "CALLS", main.node_id, helper.node_id),
        CanonicalEdge("edge:calls-main-helper-extra", repo_id, "CALLS", main.node_id, helper_extra.node_id),
        CanonicalEdge("edge:calls-caller-helper", repo_id, "CALLS", caller.node_id, helper.node_id),
        CanonicalEdge("edge:calls-broken", repo_id, "CALLS", main.node_id, "symbol:demo:missing"),
    )
    return GraphSnapshot(
        repo_id=repo_id,
        revision_id="rev-123",
        root_path=root_path,
        generated_at="2026-03-27T00:00:00+00:00",
        nodes=(project, app_file, caller_file, main, helper, helper_extra, caller),
        edges=edges,
        metadata={},
    )


class _MetadataStore:
    def __init__(self, snapshot: GraphSnapshot | None) -> None:
        self.snapshot = snapshot
        self.calls: list[str] = []

    def load_latest_snapshot(self, repo_id: str) -> GraphSnapshot | None:
        self.calls.append(repo_id)
        return self.snapshot


class CodeGraphQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        (root / "app.py").write_text(
            "def main():\n"
            "    return helper()\n\n"
            "def helper():\n"
            "    return 1\n\n"
            "def helper_extra():\n"
            "    return 2\n",
            encoding="utf-8",
        )
        (root / "caller.py").write_text(
            "def run():\n"
            "    return helper()\n",
            encoding="utf-8",
        )
        self.snapshot = make_code_graph_snapshot(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_load_latest_snapshot_returns_snapshot_or_error(self) -> None:
        store = _MetadataStore(self.snapshot)
        loaded = code_graph_queries.load_latest_snapshot(store, "demo")
        self.assertIs(loaded, self.snapshot)
        self.assertEqual(store.calls, ["demo"])

        with self.assertRaises(code_graph_queries.CodeGraphQueryError):
            code_graph_queries.load_latest_snapshot(_MetadataStore(None), "demo")

    def test_get_code_graph_schema_is_deterministic_and_skips_missing_edge_nodes(self) -> None:
        response = code_graph_queries.get_code_graph_schema(self.snapshot)

        self.assertEqual(response["repo_id"], "demo")
        self.assertEqual(response["revision_id"], "rev-123")
        self.assertEqual(response["node_type_counts"][0], {"node_kind": "File", "count": 2})
        self.assertEqual(response["edge_type_counts"][0], {"edge_type": "CALLS", "count": 4})
        self.assertIn(
            {
                "source_kind": "Function",
                "edge_type": "CALLS",
                "target_kind": "Function",
                "count": 3,
            },
            response["relationship_patterns"],
        )

    def test_search_code_graph_filters_ranks_and_validates_inputs(self) -> None:
        response = code_graph_queries.search_code_graph(
            self.snapshot,
            query="helper",
            node_kinds=["Function"],
            file_pattern="app.py",
            limit=10,
            offset=0,
        )
        self.assertEqual(response["total"], 2)
        self.assertEqual(
            [match["qualified_name"] for match in response["matches"]],
            ["demo.app.helper", "demo.app.helper_extra"],
        )

        paged = code_graph_queries.search_code_graph(
            self.snapshot,
            node_kinds=["Function"],
            limit=1,
            offset=1,
        )
        self.assertEqual(paged["total"], 4)
        self.assertEqual(len(paged["matches"]), 1)

        exact = code_graph_queries.search_code_graph(
            self.snapshot,
            query="demo.app.main",
            limit=5,
        )
        self.assertEqual(exact["matches"][0]["qualified_name"], "demo.app.main")

        with self.assertRaises(code_graph_queries.CodeGraphQueryError):
            code_graph_queries.search_code_graph(self.snapshot, limit=0)
        with self.assertRaises(code_graph_queries.CodeGraphQueryError):
            code_graph_queries.search_code_graph(self.snapshot, offset=-1)

    def test_get_code_snippet_reads_exact_symbol_and_optional_neighbors(self) -> None:
        response = code_graph_queries.get_code_snippet(
            self.snapshot,
            qualified_name="demo.app.helper",
            include_neighbors=False,
        )
        self.assertEqual(response["node"]["qualified_name"], "demo.app.helper")
        self.assertEqual(response["file_path"], "app.py")
        self.assertEqual(response["snippet"], "def helper():\n    return 1")

        neighboring = code_graph_queries.get_code_snippet(
            self.snapshot,
            qualified_name="demo.app.helper",
            include_neighbors=True,
        )
        self.assertEqual(neighboring["start_line"], 1)
        self.assertIn("def main():", neighboring["snippet"])
        self.assertIn("def helper_extra():", neighboring["snippet"])

    def test_get_code_snippet_errors_for_missing_symbol_and_missing_file(self) -> None:
        with self.assertRaises(code_graph_queries.CodeGraphQueryError):
            code_graph_queries.get_code_snippet(
                self.snapshot,
                qualified_name="demo.app.missing",
            )

        no_path_snapshot = GraphSnapshot(
            repo_id="demo",
            revision_id="rev-123",
            root_path=self.temp_dir.name,
            generated_at="2026-03-27T00:00:00+00:00",
            nodes=(
                CanonicalNode(
                    "symbol:demo:demo.app.nopath",
                    "demo",
                    "Function",
                    "nopath",
                    qualified_name="demo.app.nopath",
                ),
            ),
            edges=(),
            metadata={},
        )
        with self.assertRaises(code_graph_queries.CodeGraphQueryError):
            code_graph_queries.get_code_snippet(
                no_path_snapshot,
                qualified_name="demo.app.nopath",
            )

        (Path(self.temp_dir.name) / "app.py").unlink()
        with self.assertRaises(code_graph_queries.CodeGraphQueryError):
            code_graph_queries.get_code_snippet(
                self.snapshot,
                qualified_name="demo.app.helper",
            )

    def test_trace_call_path_supports_directions_and_validates_inputs(self) -> None:
        outbound = code_graph_queries.trace_call_path(
            self.snapshot,
            qualified_name="demo.app.main",
            direction="outbound",
            depth=1,
        )
        self.assertEqual(
            [(step["target_qualified_name"], step["depth"]) for step in outbound["steps"]],
            [("demo.app.helper", 1), ("demo.app.helper_extra", 1)],
        )

        inbound = code_graph_queries.trace_call_path(
            self.snapshot,
            qualified_name="demo.app.helper",
            direction="inbound",
            depth=1,
        )
        self.assertEqual(
            [step["source_qualified_name"] for step in inbound["steps"]],
            ["demo.app.helper", "demo.app.helper"],
        )

        both = code_graph_queries.trace_call_path(
            self.snapshot,
            qualified_name="demo.app.helper",
            direction="both",
            depth=1,
        )
        self.assertGreaterEqual(len(both["steps"]), 2)

        with self.assertRaises(code_graph_queries.CodeGraphQueryError):
            code_graph_queries.trace_call_path(self.snapshot, qualified_name="demo.app.main", direction="sideways")
        with self.assertRaises(code_graph_queries.CodeGraphQueryError):
            code_graph_queries.trace_call_path(self.snapshot, qualified_name="demo.app.main", depth=0)

    def test_helper_functions_cover_small_branches(self) -> None:
        with self.assertRaises(code_graph_queries.CodeGraphQueryError):
            code_graph_queries._require_exact_node(self.snapshot, "demo.app.unknown")

        main = next(node for node in self.snapshot.nodes if node.qualified_name == "demo.app.main")
        helper = next(node for node in self.snapshot.nodes if node.qualified_name == "demo.app.helper")
        self.assertEqual(code_graph_queries._search_rank(main, ""), 4)
        self.assertEqual(code_graph_queries._search_rank(main, "demo.app.main"), 0)
        self.assertEqual(code_graph_queries._search_rank(helper, "helper"), 1)
        self.assertEqual(code_graph_queries._search_rank(helper, "app.helper"), 2)
        self.assertEqual(code_graph_queries._search_rank(helper, "elp"), 3)
        self.assertIsNone(code_graph_queries._search_rank(helper, "missing"))

        self.assertIsNone(code_graph_queries._normalize_repo_file_path(self.snapshot.root_path, None))
        resolved = code_graph_queries._normalize_repo_file_path(self.snapshot.root_path, "app.py")
        self.assertTrue(str(resolved).endswith("app.py"))

        sort_key = code_graph_queries._edge_sort_key(
            next(edge for edge in self.snapshot.edges if edge.edge_id == "edge:calls-main-helper"),
            {
                "symbol:demo:demo.app.main": "demo.app.main",
                "symbol:demo:demo.app.helper": "demo.app.helper",
            },
            traversal_direction="outbound",
        )
        self.assertEqual(sort_key[0], "CALLS")
        self.assertEqual(sort_key[-1], "edge:calls-main-helper")


if __name__ == "__main__":
    unittest.main()
