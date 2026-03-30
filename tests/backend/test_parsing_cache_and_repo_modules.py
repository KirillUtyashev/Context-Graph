from __future__ import annotations

from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from context_graph.backend import BackendCapabilities, BackendProvider, GraphRecordRef, RecordKind
from context_graph.backend.driver import BackendDriver, BackendSession
from context_graph.backend.search import ensure_lexical_mode
from context_graph.backend.types import SearchMode, SearchQuery, TraversalQuery
from context_graph.code_identity.canonicalizer import CodeIdentityCanonicalizer
from context_graph.ingestion.git.github_client import GitHubRepoRef
from context_graph.ingestion.repo_cache import RepoCacheManager
from context_graph.ingestion import snapshot_parsing
from context_graph.models.entity_nodes import (
    EntityNode,
    entity_node_aliases,
    entity_node_file_path,
    entity_node_kind,
    entity_node_qualified_name,
    entity_node_searchable_terms,
    make_entity_node,
)
from context_graph.runtime.llm.cache import LLMCache
from context_graph.backend import maintenance as maintenance_module


class FakeGitMetadata:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.repo_roots: dict[str, str] = {}
        self.bad_repo_roots: set[str] = set()
        self.revision_map: dict[str, str] = {}
        self.fetch_fail_paths: set[str] = set()
        self.head_sha = "head-sha"

    def resolve_repo_root(self, repo_path: str) -> str:
        if repo_path in self.bad_repo_roots:
            raise RuntimeError("not a repo")
        return self.repo_roots.get(repo_path, repo_path)

    def clone_repo(
        self,
        clone_url: str,
        clone_path: str,
        *,
        branch: str | None,
        auth_token: str | None,
    ) -> None:
        self.calls.append(("clone_repo", clone_url, clone_path, branch, auth_token))
        Path(clone_path).mkdir(parents=True, exist_ok=True)
        self.repo_roots[clone_path] = str(Path(clone_path).resolve())

    def fetch_remote(self, repo_path: str, *, auth_token: str | None) -> None:
        self.calls.append(("fetch_remote", repo_path, auth_token))
        if repo_path in self.fetch_fail_paths:
            raise RuntimeError("fetch failed")

    def remove_repo(self, repo_path: str) -> None:
        self.calls.append(("remove_repo", repo_path))
        shutil.rmtree(repo_path, ignore_errors=True)

    def checkout_branch(self, repo_path: str, branch: str) -> None:
        self.calls.append(("checkout_branch", repo_path, branch))

    def hard_reset(self, repo_path: str, target: str) -> None:
        self.calls.append(("hard_reset", repo_path, target))

    def get_head_sha(self, repo_path: str) -> str:
        self.calls.append(("get_head_sha", repo_path))
        return self.head_sha

    def resolve_revision(self, repo_path: str, candidate: str) -> str:
        self.calls.append(("resolve_revision", repo_path, candidate))
        if candidate not in self.revision_map:
            raise RuntimeError("unknown ref")
        return self.revision_map[candidate]


class FakeSession(BackendSession):
    provider = BackendProvider.NEO4J

    def __init__(self) -> None:
        self.closed = False
        self.exit_args = None
        self.runs: list[tuple[str, dict[str, object]]] = []
        self.write_calls: list[tuple[tuple, dict]] = []

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exit_args = (exc_type, exc, tb)

    async def run(self, query: str, **kwargs):
        self.runs.append((query, kwargs))
        return {"query": query, "kwargs": kwargs}

    async def close(self) -> None:
        self.closed = True

    async def execute_write(self, func, *args, **kwargs):
        self.write_calls.append((args, kwargs))
        return func(*args, **kwargs)


class FakeDriver(BackendDriver):
    provider = BackendProvider.NEO4J
    capabilities = BackendCapabilities(
        supports_transactions=False,
        supports_lexical_search=True,
        supports_vector_search=False,
        supports_hybrid_search=False,
        supports_graph_traversal=True,
        uses_external_text_index=False,
    )

    def __init__(self) -> None:
        self._session = FakeSession()

    @property
    def search(self):
        return SimpleNamespace()

    @property
    def mutations(self):
        return SimpleNamespace()

    @property
    def maintenance(self):
        return SimpleNamespace()

    @property
    def traversal(self):
        return SimpleNamespace()

    async def execute_query(self, query: str, **kwargs):
        return {"query": query, "kwargs": kwargs}

    def session(self) -> BackendSession:
        return self._session

    async def close(self) -> None:
        self._session.closed = True

    async def build_indices(self, delete_existing: bool = False) -> None:
        return None

    async def health_check(self) -> None:
        return None


def test_snapshot_parsing_helpers_and_export_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(snapshot_parsing, "utc_now_iso", lambda: "2026-03-30T00:00:00Z")

    assert snapshot_parsing._slugify("My Repo/Test") == "my-repo-test"
    assert snapshot_parsing._slugify("!!!") == "repo"
    assert snapshot_parsing._normalized_rel_path(".\\src\\pkg/") == "src/pkg"
    assert snapshot_parsing._stable_node_id(
        "demo",
        kind="Project",
        qualified_name=None,
        file_path=None,
        name="demo",
    ) == "project:demo"
    assert snapshot_parsing._stable_node_id(
        "demo",
        kind="Function",
        qualified_name=None,
        file_path="./src/main.py",
        name="main",
    ) == "function:demo:src/main.py:main"
    assert snapshot_parsing._coerce_properties(None) == {}
    assert snapshot_parsing._coerce_properties("{}") == {}
    assert snapshot_parsing._coerce_properties('{"ok": true}') == {"ok": True}
    assert snapshot_parsing._coerce_properties("[1, 2]") == {"value": [1, 2]}
    assert snapshot_parsing._coerce_properties("{oops") == {"raw_properties_json": "{oops"}

    export_payload = {
        "root_path": "C:\\repo",
        "project": "My Demo Repo",
        "node_count": 3,
        "edge_count": 3,
        "nodes": [
            {
                "temp_id": 1,
                "label": "Project",
                "name": "My Demo Repo",
                "qualified_name": None,
                "file_path": "",
                "properties_json": '{"language": "python"}',
            },
            {
                "temp_id": 2,
                "label": "File",
                "name": "main.py",
                "qualified_name": "demo.main.__file__",
                "file_path": "./src\\main.py/",
                "properties_json": "{}",
            },
            {
                "temp_id": 3,
                "label": "Function",
                "name": "main",
                "qualified_name": "demo.main",
                "file_path": "./src\\main.py/",
                "start_line": 10,
                "end_line": 12,
                "properties_json": "[1, 2]",
            },
        ],
        "edges": [
            {
                "source_temp_id": 1,
                "target_temp_id": 2,
                "type": "CONTAINS_FILE",
                "properties_json": '{"kind": "structural"}',
            },
            {
                "source_temp_id": 2,
                "target_temp_id": 3,
                "type": "DEFINES",
                "properties_json": "{oops",
            },
            {
                "source_temp_id": 2,
                "target_temp_id": 99,
                "type": "BROKEN",
                "properties_json": "{}",
            },
        ],
    }

    snapshot = snapshot_parsing.build_snapshot_from_graph_export(
        export_payload,
        revision_id="rev-1",
    )

    assert snapshot.repo_id == "my-demo-repo"
    assert snapshot.generated_at == "2026-03-30T00:00:00Z"
    assert snapshot.root_path == "C:\\repo"
    assert len(snapshot.nodes) == 3
    assert len(snapshot.edges) == 2
    assert snapshot.metadata["project_name"] == "My Demo Repo"
    project_node = next(node for node in snapshot.nodes if node.kind == "Project")
    assert project_node.properties["root_path"] == "C:\\repo"
    assert project_node.properties["language"] == "python"
    function_node = next(node for node in snapshot.nodes if node.kind == "Function")
    assert function_node.file_path == "src/main.py"
    assert function_node.properties == {"value": [1, 2]}
    defines_edge = next(edge for edge in snapshot.edges if edge.edge_type == "DEFINES")
    assert defines_edge.properties == {"raw_properties_json": "{oops"}


def test_entity_node_helpers_cover_labels_and_search_terms() -> None:
    node = make_entity_node(
        node_id="symbol:1",
        repo_id="demo",
        kind="Function",
        name="main",
        qualified_name="pkg.main",
        file_path="src/app.py",
        aliases=("run", " main ", ""),
        metadata={"owner": "tests"},
    )
    assert node.labels == ("Function",)
    assert entity_node_kind(node) == "Function"
    assert entity_node_qualified_name(node) == "pkg.main"
    assert entity_node_file_path(node) == "src/app.py"
    assert entity_node_aliases(node) == ("run", " main ")
    assert entity_node_searchable_terms(node) == (
        " main ",
        "app.py",
        "main",
        "pkg.main",
        "run",
        "src/app.py",
    )

    entity_only = make_entity_node(
        node_id="entity:1",
        repo_id="demo",
        kind="Entity",
        name="plain",
        metadata={"qualified_name": " ", "file_path": "  ", "aliases": "ignored"},
    )
    assert entity_only.labels == ()
    assert entity_node_kind(entity_only) == "Entity"
    assert entity_node_qualified_name(entity_only) is None
    assert entity_node_file_path(entity_only) is None
    assert entity_node_aliases(entity_only) == ()
    assert entity_node_qualified_name(EntityNode(uuid="3", name="none", group_id="demo")) is None

    manual = EntityNode(uuid="2", name="manual", group_id="demo", labels=("Entity", "Class"))
    assert entity_node_kind(manual) == "Class"


def test_code_identity_canonicalizer_and_maintenance_totals() -> None:
    canonicalizer = CodeIdentityCanonicalizer()
    assert canonicalizer.normalize_rel_path("./src\\pkg/") == "src/pkg"
    assert canonicalizer.normalize_rel_path(".") == ""
    assert canonicalizer.stable_node_id(
        "demo",
        kind="Project",
        qualified_name=None,
        file_path=None,
        name="demo",
    ) == "project:demo"
    assert canonicalizer.stable_node_id(
        "demo",
        kind="Function",
        qualified_name="pkg.main",
        file_path=None,
        name="main",
    ) == "function:demo:pkg.main"
    assert canonicalizer.stable_node_id(
        "demo",
        kind="File",
        qualified_name=None,
        file_path="./src/app.py",
        name="app.py",
    ) == "file:demo:src/app.py:app.py"
    assert canonicalizer.stable_node_id(
        "demo",
        kind="Module",
        qualified_name=None,
        file_path=None,
        name="pkg",
    ) == "module:demo:pkg"
    assert canonicalizer.stable_edge_id("demo", "CALLS", "a", "b") == "edge:demo:CALLS:a:b"

    assert maintenance_module.ScopeResetResult("demo", 1, 2, 3).total_deleted == 6
    assert maintenance_module.RepoResetResult("demo", 4, 5, 6).total_deleted == 15


def test_llm_cache_round_trips_and_handles_bad_values(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cache = LLMCache(str(tmp_path / "cache"))
    cache.set("ok", {"value": 1})
    assert cache.get("ok") == {"value": 1}
    assert cache.get("missing") is None

    cache._conn.execute("INSERT OR REPLACE INTO cache (key, value) VALUES (?, ?)", ("broken", "{oops"))
    cache._conn.commit()
    with caplog.at_level("WARNING"):
        assert cache.get("broken") is None
        cache.set("bad", {"value": object()})
    assert "Corrupted cache entry" in caplog.text
    assert "Non-JSON-serializable cache value" in caplog.text
    cache.close()

    suppressing_cache = LLMCache(str(tmp_path / "cache2"))
    suppressing_cache._conn = SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    suppressing_cache.__del__()


@pytest.mark.asyncio
async def test_backend_query_types_and_default_transaction_wrapper() -> None:
    with pytest.raises(ValueError):
        SearchQuery(mode=SearchMode.LEXICAL, text="jwt", limit=0)
    with pytest.raises(ValueError):
        SearchQuery(mode=SearchMode.LEXICAL, text=None)
    with pytest.raises(ValueError):
        SearchQuery(mode=SearchMode.VECTOR, embedding=None)
    with pytest.raises(ValueError):
        SearchQuery(mode=SearchMode.HYBRID, text=None, embedding=None)
    with pytest.raises(ValueError):
        TraversalQuery(origin_refs=(), max_depth=1)
    with pytest.raises(ValueError):
        TraversalQuery(
            origin_refs=(GraphRecordRef(RecordKind.NODE, "1", "claim"),),
            max_depth=0,
        )
    with pytest.raises(ValueError):
        TraversalQuery(
            origin_refs=(GraphRecordRef(RecordKind.NODE, "1", "claim"),),
            max_depth=1,
            limit=0,
        )
    with pytest.raises(ValueError):
        TraversalQuery(
            origin_refs=(GraphRecordRef(RecordKind.NODE, "1", "claim"),),
            max_depth=1,
            direction="sideways",
        )

    ensure_lexical_mode(SearchQuery(mode=SearchMode.LEXICAL, text="jwt"))
    with pytest.raises(ValueError):
        ensure_lexical_mode(SearchQuery(mode=SearchMode.VECTOR, embedding=(1.0,)))

    session = FakeSession()
    async with session as entered:
        assert entered is session
        assert await entered.execute_write(lambda value: value + 1, 2) == 3

    driver = FakeDriver()
    async with driver.transaction() as tx:
        result = await tx.run("MATCH", value=1)
    assert result == {"query": "MATCH", "kwargs": {"value": 1}}
    assert driver._session.runs == [("MATCH", {"value": 1})]
    assert driver._session.closed is True


def test_repo_cache_manager_covers_local_repos_and_ref_resolution(
    tmp_path: Path,
) -> None:
    metadata = FakeGitMetadata()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    nested = repo_root / "nested"
    nested.mkdir()
    metadata.repo_roots[str(nested)] = str(repo_root)

    manager = RepoCacheManager(base_dir=str(tmp_path / "cache"), git_metadata=metadata)
    managed = manager.manage_local_repo(str(nested), repo_id="demo", request_id="req-1")

    assert managed.repo_path == str(repo_root)
    assert Path(managed.work_root).exists()

    manager.prepare_default_branch(str(repo_root), "main")
    assert ("checkout_branch", str(repo_root), "main") in metadata.calls
    assert ("hard_reset", str(repo_root), "origin/main") in metadata.calls

    metadata.revision_map["origin/main"] = "abc123"
    assert manager.resolve_ref_sha(str(repo_root), "main") == "abc123"
    assert metadata.calls[-2:] == [
        ("resolve_revision", str(repo_root), "main"),
        ("resolve_revision", str(repo_root), "origin/main"),
    ]
    with pytest.raises(RuntimeError, match="unable to resolve git ref 'missing'"):
        manager.resolve_ref_sha(str(repo_root), "missing")

    manager.cleanup_request_workspace("demo", "req-1")
    assert not Path(managed.work_root).exists()


def test_repo_cache_manager_handles_clone_paths_and_existing_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = FakeGitMetadata()
    monkeypatch.setattr("context_graph.ingestion.repo_cache.utc_now_iso", lambda: "2026-03-30T00:00:00Z")

    manager = RepoCacheManager(base_dir=str(tmp_path / "cache"), git_metadata=metadata)
    repo_ref = GitHubRepoRef(
        owner="acme",
        name="demo",
        canonical_repo="acme/demo",
        clone_url="https://github.com/acme/demo.git",
        html_url="https://github.com/acme/demo",
        default_branch="main",
        source="github_url",
    )

    managed = manager.ensure_managed_repo(
        repo_ref,
        repo_id="demo",
        request_id="req-1",
        auth_token="secret",
    )

    clone_path = tmp_path / "cache" / "repos" / "acme" / "demo"
    assert managed.repo_path == str(clone_path)
    assert managed.source_state is not None
    assert managed.source_state.default_branch == "main"
    assert managed.source_state.last_fetch_at == "2026-03-30T00:00:00Z"
    assert managed.source_state.last_fetched_head_sha == "head-sha"
    assert ("clone_repo", repo_ref.clone_url, str(clone_path), "main", "secret") in metadata.calls
    assert ("checkout_branch", str(clone_path), "main") in metadata.calls
    assert ("hard_reset", str(clone_path), "origin/main") in metadata.calls

    invalid_clone = tmp_path / "invalid-clone"
    invalid_clone.mkdir()
    metadata.bad_repo_roots.add(str(invalid_clone))
    manager._ensure_clone(repo_ref, invalid_clone, auth_token=None)
    assert ("remove_repo", str(invalid_clone)) in metadata.calls

    stale_clone = tmp_path / "stale-clone"
    stale_clone.mkdir()
    metadata.repo_roots[str(stale_clone)] = str(stale_clone.resolve())
    metadata.fetch_fail_paths.add(str(stale_clone))
    manager._ensure_clone(repo_ref, stale_clone, auth_token="secret")
    assert ("fetch_remote", str(stale_clone), "secret") in metadata.calls
    assert ("remove_repo", str(stale_clone)) in metadata.calls

    existing_clone = tmp_path / "existing-clone"
    existing_clone.mkdir()
    metadata.repo_roots[str(existing_clone)] = str(existing_clone.resolve())
    existing_state = SimpleNamespace(clone_path=str(existing_clone), default_branch="develop")
    no_branch_ref = GitHubRepoRef(
        owner="acme",
        name="demo",
        canonical_repo="acme/demo",
        clone_url="https://github.com/acme/demo.git",
        html_url="https://github.com/acme/demo",
        default_branch=None,
        source="github_repo",
    )

    managed_existing = manager.ensure_managed_repo(
        no_branch_ref,
        repo_id="demo",
        request_id="req-2",
        auth_token=None,
        existing_state=existing_state,
    )
    assert managed_existing.source_state is not None
    assert managed_existing.source_state.default_branch == "develop"
    assert manager._looks_like_git_repo(existing_clone) is True
    metadata.bad_repo_roots.add(str(existing_clone))
    assert manager._looks_like_git_repo(existing_clone) is False
