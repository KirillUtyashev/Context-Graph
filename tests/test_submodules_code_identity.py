from __future__ import annotations

from context_graph.code_identity.canonicalizer import CodeIdentityCanonicalizer
from context_graph.code_identity.resolver import (
    SnapshotCodeResolver,
    _ancestor_folder_paths,
    _has_known_extension,
    _infer_hint_kind,
    _looks_like_dotted_identifier,
    _looks_like_file_basename,
    _normalize_hint_value,
)
from context_graph.models import CanonicalNode, GraphSnapshot


def _snapshot() -> GraphSnapshot:
    return GraphSnapshot(
        repo_id="repo-1",
        revision_id="rev-1",
        root_path="/tmp/repo",
        generated_at="2026-03-27T12:00:00+00:00",
        nodes=(
            CanonicalNode("project:repo-1", "repo-1", "Project", "repo"),
            CanonicalNode("folder:repo-1:src", "repo-1", "Folder", "src", qualified_name="repo-1.src", file_path="src"),
            CanonicalNode("file:repo-1:src/main.py:main.py", "repo-1", "File", "main.py", file_path="src/main.py"),
            CanonicalNode(
                "function:repo-1:pkg.module.run",
                "repo-1",
                "Function",
                "run",
                qualified_name="pkg.module.run",
                file_path="src/main.py",
            ),
            CanonicalNode(
                "class:repo-1:pkg.module.Runner",
                "repo-1",
                "Class",
                "Runner",
                qualified_name="pkg.module.Runner",
                file_path="src/main.py",
            ),
        ),
        edges=(),
    )


def test_canonicalizer_normalizes_paths_and_builds_stable_ids() -> None:
    canonicalizer = CodeIdentityCanonicalizer()

    assert canonicalizer.normalize_rel_path("./src\\pkg/") == "src/pkg"
    assert canonicalizer.normalize_rel_path(".") == ""
    assert canonicalizer.stable_node_id("repo-1", kind="Project", qualified_name=None, file_path=None, name="repo") == "project:repo-1"
    assert canonicalizer.stable_node_id(
        "repo-1",
        kind="Function",
        qualified_name="pkg.module.run",
        file_path="src/main.py",
        name="run",
    ) == "function:repo-1:pkg.module.run"
    assert canonicalizer.stable_node_id(
        "repo-1",
        kind="Function",
        qualified_name=None,
        file_path=None,
        name="run",
    ) == "function:repo-1:run"
    assert canonicalizer.stable_edge_id("repo-1", "CALLS", "a", "b") == "edge:repo-1:CALLS:a:b"


def test_snapshot_code_resolver_resolves_precise_and_inferred_hints() -> None:
    resolver = SnapshotCodeResolver(_snapshot())

    assert resolver.resolve_node_id("function:repo-1:pkg.module.run") is not None
    assert resolver.resolve_qualified_name("pkg.module.run").name == "run"
    assert resolver.resolve_qualified_name_suffix("module.run").name == "run"
    assert resolver.resolve_file_path("./src/main.py").name == "main.py"
    assert resolver.resolve_folder_path("src").name == "src"
    assert resolver.resolve_file_basename("main.py").file_path == "src/main.py"
    assert resolver.resolve_symbol_name("Runner").qualified_name == "pkg.module.Runner"
    assert resolver.resolve_about_hint(("pkg.module.run", "qualified_name")).qualified_name == "pkg.module.run"
    assert resolver.resolve_about_hint(("src/main.py", "file_path")).name == "main.py"
    assert resolver.resolve_about_hint(("main.py", "file_basename")).name == "main.py"
    assert resolver.resolve_about_hint(("module.run", "qualified_name_suffix")).name == "run"
    assert resolver.resolve_about_hint(("Runner", "symbol_name")).name == "Runner"
    assert resolver.resolve_about_hint("function:repo-1:pkg.module.run").name == "run"
    assert resolver.resolve_about_hint("") is None
    assert resolver.resolve_about_hint("no-match") is None
    assert resolver.resolve_about_record_id("Runner") == "class:repo-1:pkg.module.Runner@rev-1"
    assert resolver.resolve_about_record_id("unknown") is None
    assert resolver.resolve_about_record_ids(["Runner", "src/main.py"]) == (
        "class:repo-1:pkg.module.Runner@rev-1",
        "file:repo-1:src/main.py:main.py@rev-1",
    )
    assert resolver.resolve_about_hints(["Runner", "Runner", "unknown"]) == (
        resolver.resolve_symbol_name("Runner"),
    )


def test_snapshot_code_resolver_handles_ambiguity_and_changed_files() -> None:
    duplicate = CanonicalNode(
        "file:repo-1:pkg/main.py:main.py",
        "repo-1",
        "File",
        "main.py",
        file_path="pkg/main.py",
    )
    resolver = SnapshotCodeResolver(_snapshot(), extra_nodes=(duplicate,))

    assert resolver.resolve_file_basename("main.py") is None

    changed = resolver.resolve_changed_files(["src/main.py", "./src/ignored/../main.py", ""])
    assert [node.node_id for node in changed] == [
        "file:repo-1:src/main.py:main.py",
        "folder:repo-1:src",
    ]


def test_resolver_helper_functions_cover_edge_cases() -> None:
    canonicalizer = CodeIdentityCanonicalizer()

    assert _ancestor_folder_paths("", canonicalizer) == ()
    assert _ancestor_folder_paths("src/main.py", canonicalizer) == ("src",)
    assert _normalize_hint_value("  `pkg.module.run(),:`  ") == "pkg.module.run("
    assert _normalize_hint_value("value()") == "value("
    assert _normalize_hint_value("value,") == "value"
    assert _normalize_hint_value("   ") == ""
    assert _infer_hint_kind("function:repo_1:item") == "node_id"
    assert _infer_hint_kind("src/main.py") == "file_path"
    assert _infer_hint_kind("main.py") == "file_basename"
    assert _infer_hint_kind("pkg.module.run") == "qualified_name"
    assert _infer_hint_kind("Module.Run") == "qualified_name_suffix"
    assert _infer_hint_kind("Runner") == "symbol_name"
    assert _infer_hint_kind("???") is None
    assert _infer_hint_kind("") is None
    assert _looks_like_file_basename("main.py") is True
    assert _looks_like_file_basename("src/main.py") is False
    assert _has_known_extension("main.py") is True
    assert _has_known_extension("README") is False
    assert _looks_like_dotted_identifier("pkg.module") is True
    assert _looks_like_dotted_identifier("pkg.1module") is False
