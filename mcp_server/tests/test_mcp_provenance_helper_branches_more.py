from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from context_graph.backend import GraphRecordRef, RecordKind, TraversalHit
from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.query import code_graph as code_graph_queries

from src.provenance import search as search_module
from src.provenance import shared as shared_module
from src.tools import search_code as search_code_module


def make_snapshot(root_path: str) -> GraphSnapshot:
    repo_id = "demo"
    file_node = CanonicalNode(
        "file:demo:app.py",
        repo_id,
        "File",
        "app.py",
        qualified_name="demo.app.__file__",
        file_path="app.py",
    )
    missing_file = CanonicalNode(
        "file:demo:missing.py",
        repo_id,
        "File",
        "missing.py",
        qualified_name="demo.missing.__file__",
        file_path="missing.py",
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
    return GraphSnapshot(
        repo_id=repo_id,
        revision_id="rev-1",
        root_path=root_path,
        generated_at="2026-03-30T00:00:00Z",
        nodes=(file_node, main, helper, missing_file),
        edges=(
            CanonicalEdge("edge:defines-main", repo_id, "DEFINES", file_node.node_id, main.node_id),
            CanonicalEdge("edge:calls", repo_id, "CALLS", main.node_id, helper.node_id),
        ),
        metadata={},
    )


def _close_coro_and_return(value):
    def runner(coro):
        coro.close()
        return value

    return runner


def test_iter_file_matches_covers_duplicate_filter_missing_and_limit_branches(tmp_path) -> None:
    (tmp_path / "app.py").write_text(
        "def main():\n"
        "    return helper()\n\n"
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    snapshot = make_snapshot(str(tmp_path))

    assert (
        search_code_module._iter_file_matches(
            snapshot=snapshot,
            query="return",
            file_pattern="*.md",
            limit=5,
            regex=False,
        )
        == []
    )

    limited = search_code_module._iter_file_matches(
        snapshot=snapshot,
        query="return",
        file_pattern=None,
        limit=1,
        regex=False,
    )
    assert len(limited) == 1
    assert limited[0]["file_path"] == "app.py"

    assert (
        search_code_module._iter_file_matches(
            snapshot=snapshot,
            query="does-not-exist",
            file_pattern=None,
            limit=5,
            regex=False,
        )
        == []
    )


def test_shared_small_helpers_cover_mapping_wrapper_and_bool_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shared_module, "_run_async", _close_coro_and_return("wrapped"))

    assert (
        shared_module._relation_record_inactive_at(
            {"properties": {"is_active": False, "deactivated_at": "2026-03-30T00:00:00Z"}}
        )
        == "2026-03-30T00:00:00Z"
    )
    assert shared_module._search_records(
        driver=SimpleNamespace(),
        repo_id="demo",
        query="jwt",
        group_id="main",
        record_kinds=(RecordKind.NODE,),
        record_types=("claim",),
        limit=5,
    ) == "wrapped"
    assert shared_module._fetch_exact_node_record(
        driver=SimpleNamespace(),
        node_id="claim:1",
        repo_id="demo",
    ) == "wrapped"
    assert shared_module._normalize_sources(["", "claims"]) == ["claims"]
    with pytest.raises(ValueError, match="unsupported source 'unsupported'"):
        shared_module._normalize_sources(["unsupported"])
    assert shared_module._source_for_record_type("evidence_record") == "observations"
    assert shared_module._source_for_record_type("claim") == "claims"
    assert shared_module._source_for_record_type("mystery") == "unknown"
    assert shared_module._record_is_active({"properties": {"is_active": "false"}}) is False
    assert (
        shared_module._record_qualified_name(
            {"record_type": "code_node", "properties": {"title": "demo.app.main"}}
        )
        == "demo.app.main"
    )
    fallback = shared_module._fallback_record_from_profile_hit(
        {
            "profile_kind": "semantic_observation_profile",
            "profile_target_id": "observation:1",
            "value_text": "Observed a change.",
            "metadata": {"observed_at": "2026-03-30T00:00:00Z"},
        }
    )
    assert fallback["record_type"] == "observation"
    assert (
        shared_module._neighborhood_node_payload(
            {
                "record_id": "symbol:1",
                "record_type": "code_node",
                "properties": {
                    "title": "demo.app.main",
                    "qualified_name": "demo.app.main",
                },
            }
        )["qualified_name"]
        == "demo.app.main"
    )
    assert shared_module._coerce_bool(None, default=False) is False
    assert shared_module._coerce_bool(1, default=False) is True
    assert shared_module._coerce_bool("yes", default=False) is True
    assert shared_module._coerce_bool("no", default=True) is False
    assert shared_module._coerce_bool("maybe", default=True) is True
    assert shared_module._truncate("abcdef", limit=5) == "ab..."


@pytest.mark.asyncio
async def test_shared_async_helpers_cover_fetch_traversal_and_timeline_branches() -> None:
    empty_driver = SimpleNamespace(search=SimpleNamespace(fetch=AsyncMock(return_value=[])))
    with pytest.raises(ValueError, match="No indexed node found"):
        await shared_module._fetch_exact_node_record_async(
            driver=empty_driver,
            node_id="claim:1",
            repo_id="demo",
        )

    records = {
        "claim:1": {
            "record_id": "claim:1",
            "record_type": "claim",
            "repo_id": "demo",
            "group_id": "main",
            "properties": {"observed_at": "2026-03-30T00:00:00Z"},
        },
        "claim:2": {
            "record_id": "claim:2",
            "record_type": "claim",
            "repo_id": "demo",
            "group_id": "main",
            "properties": {"observed_at": "2026-03-31T00:00:00Z"},
        },
        "edge:1": {
            "record_id": "edge:1",
            "record_type": "claim_relation",
            "source_record_id": "claim:1",
            "target_record_id": "claim:2",
            "properties": {},
        },
    }

    async def fetch(refs):
        return [records[ref.record_id] for ref in refs if ref.record_id in records]

    traversal_hits = [
        TraversalHit(
            ref=GraphRecordRef(
                record_kind=RecordKind.NODE,
                record_id="claim:1",
                record_type="claim",
                repo_id="demo",
                group_id="main",
            ),
            distance=0,
        ),
        TraversalHit(
            ref=GraphRecordRef(
                record_kind=RecordKind.NODE,
                record_id="claim:2",
                record_type="claim",
                repo_id="demo",
                group_id="main",
            ),
            distance=1,
        ),
        TraversalHit(
            ref=GraphRecordRef(
                record_kind=RecordKind.NODE,
                record_id="claim:2",
                record_type="claim",
                repo_id="demo",
                group_id="main",
            ),
            distance=1,
        ),
        TraversalHit(
            ref=GraphRecordRef(
                record_kind=RecordKind.EDGE,
                record_id="edge:1",
                record_type="claim_relation",
                repo_id="demo",
                group_id="main",
            ),
            distance=1,
        ),
        TraversalHit(
            ref=GraphRecordRef(
                record_kind=RecordKind.EDGE,
                record_id="edge:1",
                record_type="claim_relation",
                repo_id="demo",
                group_id="main",
            ),
            distance=1,
        ),
    ]
    driver = SimpleNamespace(
        search=SimpleNamespace(fetch=fetch),
        traversal=SimpleNamespace(expand=AsyncMock(return_value=traversal_hits)),
    )
    center, nodes, edges = await shared_module._expand_backend_neighborhood_async(
        driver=driver,
        node_id="claim:1",
        repo_id="demo",
        hops=1,
        edge_types=None,
    )
    assert center["record_id"] == "claim:1"
    assert len(nodes) == 1
    assert len(edges) == 1

    events = shared_module._timeline_events_from_records(
        center_record=records["claim:1"],
        node_records=(records["claim:1"],),
        edge_records=(
            {"record_id": "edge:other", "record_type": "other", "properties": {}},
        ),
        limit=10,
    )
    assert len(events) == 1


def test_shared_context_helpers_cover_resolve_and_snippet_branches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    (tmp_path / "app.py").write_text(
        "def main():\n"
        "    return helper()\n\n"
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    snapshot = make_snapshot(str(tmp_path))
    node_map = snapshot.node_map()

    assert (
        shared_module._resolve_primary_node(snapshot, target="app.py", target_kind="file").node_id
        == "file:demo:app.py"
    )
    assert (
        shared_module._resolve_primary_node(snapshot, target="helper", target_kind="function").node_id
        == "symbol:demo:demo.app.helper"
    )

    monkeypatch.setattr(
        shared_module.code_graph_queries,
        "search_code_graph",
        lambda snapshot_arg, **kwargs: {"matches": [{"node_id": "symbol:demo:demo.app.helper"}]},
    )
    resolved = shared_module._resolve_primary_node(snapshot, target="assist", target_kind="function")
    assert resolved.qualified_name == "demo.app.helper"

    monkeypatch.setattr(
        shared_module.code_graph_queries,
        "search_code_graph",
        lambda snapshot_arg, **kwargs: {"matches": []},
    )
    with pytest.raises(code_graph_queries.CodeGraphQueryError, match="No indexed code context found"):
        shared_module._resolve_primary_node(snapshot, target="unknown target", target_kind="function")

    file_snippets = shared_module._context_snippets(
        snapshot,
        primary=node_map["file:demo:app.py"],
        related_nodes=[node_map["symbol:demo:demo.app.helper"]],
        include_neighbors=False,
    )
    assert len(file_snippets) == 2
    assert file_snippets[0]["file_path"] == "app.py"
    assert "def helper()" in file_snippets[1]["snippet"]

    anonymous_node = SimpleNamespace(qualified_name=None, file_path="app.py")
    symbol_snippets = shared_module._context_snippets(
        snapshot,
        primary=node_map["symbol:demo:demo.app.main"],
        related_nodes=[
            anonymous_node,
            node_map["symbol:demo:demo.app.helper"],
            node_map["symbol:demo:demo.app.helper"],
        ],
        include_neighbors=False,
    )
    assert [snippet["qualified_name"] for snippet in symbol_snippets] == [
        "demo.app.main",
        "demo.app.helper",
    ]


@pytest.mark.asyncio
async def test_shared_semantic_search_and_run_async_guard_branches() -> None:
    result = await shared_module._invoke_semantic_search_async(
        semantic_search=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("semantic failed")),
        query="jwt",
        repo_id="demo",
        limit=5,
        active_only=True,
        profile_kinds=("semantic_claim_profile",),
        llm_enabled=False,
        query_texts=("jwt",),
    )
    assert result == ()

    coro = asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError, match="event loop was already running"):
            shared_module._run_async(coro)
    finally:
        coro.close()


def test_search_sync_wrappers_and_channel_contributions_cover_remaining_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_module, "_run_async", _close_coro_and_return(("wrapped",)))
    hybrid = search_module.HybridContextSearchService(driver=SimpleNamespace())
    relation = search_module.RelationContextSearchService(driver=SimpleNamespace())

    assert hybrid.search(
        repo_id="demo",
        query="jwt",
        group_id="main",
        sources=["claims"],
        limit=5,
    ) == ("wrapped",)
    assert relation.search(
        repo_id="demo",
        group_id="main",
        query="jwt",
        limit=5,
        active_only=True,
    ) == ("wrapped",)

    aggregates: dict[tuple[str, str], object] = {}
    hybrid._apply_channel_contribution(
        aggregates=aggregates,
        identity=("observation", "observation:1"),
        record={
            "record_id": "evidence:1",
            "record_type": "evidence_record",
            "properties": {"observation_id": "observation:1", "quote_text": "quoted"},
        },
        channel=search_module.LEXICAL_CHANNEL,
        raw_score=0.4,
        rank=1,
    )
    hybrid._apply_channel_contribution(
        aggregates=aggregates,
        identity=("observation", "observation:1"),
        record={"record_id": "observation:1", "record_type": "observation", "properties": {}},
        channel=search_module.SEMANTIC_OBSERVATION_CHANNEL,
        raw_score=0.8,
        rank=1,
        matched_profile_key_text="key text",
        matched_profile_key_origin="generated",
        semantic_profile_kind="semantic_observation_profile",
    )
    assert aggregates[("observation", "observation:1")].record["record_type"] == "observation"


@pytest.mark.asyncio
async def test_search_internal_helpers_cover_surface_fetch_and_relation_guard_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hybrid = search_module.HybridContextSearchService(driver=SimpleNamespace())

    evidence_record = {
        "record_id": "evidence:1",
        "record_type": "evidence_record",
        "properties": {
            "observation_id": "observation:1",
            "quote_text": "quoted evidence",
        },
    }
    observation_record = {
        "record_id": "observation:1",
        "record_type": "observation",
        "properties": {"text": "observation"},
    }

    hybrid._fetch_records_async = AsyncMock(return_value={})
    surfaced = await hybrid._surface_lexical_records_async(
        repo_id="demo",
        group_id="main",
        records=((0.9, evidence_record), (0.8, observation_record)),
    )
    assert surfaced[0].record["record_type"] == "observation"

    hybrid._fetch_records_async = AsyncMock(return_value={"observation:1": observation_record})
    surfaced = await hybrid._surface_lexical_records_async(
        repo_id="demo",
        group_id="main",
        records=((0.8, observation_record), (0.9, evidence_record)),
    )
    assert surfaced[0].matched_lexical_summary == "quoted evidence"

    hybrid._fetch_records_async = AsyncMock(return_value={})
    await hybrid._semantic_profile_record_async(
        repo_id="demo",
        group_id="main",
        profile_hit={
            "profile_kind": "semantic_observation_profile",
            "profile_target_id": "observation:1",
            "value_text": "observed text",
            "metadata": {},
        },
    )
    assert hybrid._fetch_records_async.await_args.args[0][0].record_type == "observation"

    await hybrid._semantic_profile_record_async(
        repo_id="demo",
        group_id="main",
        profile_hit={
            "profile_kind": "custom_profile",
            "profile_target_kind": "File",
            "profile_target_id": "file:1",
            "value_text": "file text",
            "metadata": {},
        },
    )
    assert hybrid._fetch_records_async.await_args.args[0][0].record_type == "file"

    exploding_fetch_hybrid = search_module.HybridContextSearchService(
        driver=SimpleNamespace(search=SimpleNamespace(fetch=AsyncMock(side_effect=RuntimeError("fetch failed"))))
    )
    assert (
        await exploding_fetch_hybrid._fetch_records_async(
            [
                GraphRecordRef(
                    record_kind=RecordKind.NODE,
                    record_id="claim:1",
                    record_type="claim",
                    repo_id="demo",
                )
            ]
        )
        == {}
    )

    relation = search_module.RelationContextSearchService(driver=SimpleNamespace(), semantic_search=None)
    monkeypatch.setattr(
        search_module,
        "_search_records_async",
        AsyncMock(return_value=[(0.4, {"record_type": "claim_relation"})]),
    )
    assert (
        await relation.search_async(
            repo_id="demo",
            group_id="main",
            query="jwt",
            limit=5,
            active_only=True,
        )
        == ()
    )

    monkeypatch.setattr(search_module, "_search_records_async", AsyncMock(return_value=[]))
    assert (
        await relation.search_async(
            repo_id="demo",
            group_id="main",
            query="jwt",
            limit=5,
            active_only=True,
        )
        == ()
    )

    semantic_relation = search_module.RelationContextSearchService(
        driver=SimpleNamespace(),
        semantic_search=lambda **kwargs: [],
    )
    monkeypatch.setattr(search_module, "_search_records_async", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        search_module,
        "_invoke_semantic_search_async",
        AsyncMock(return_value=("not-a-mapping",)),
    )
    assert (
        await semantic_relation.search_async(
            repo_id="demo",
            group_id="main",
            query="jwt",
            limit=5,
            active_only=True,
        )
        == ()
    )

    monkeypatch.setattr(
        search_module,
        "_invoke_semantic_search_async",
        AsyncMock(return_value=({"profile_target_id": None},)),
    )
    assert (
        await semantic_relation.search_async(
            repo_id="demo",
            group_id="main",
            query="jwt",
            limit=5,
            active_only=True,
        )
        == ()
    )

    monkeypatch.setattr(search_module, "_normalize_sources", lambda sources: ["claims"])
    monkeypatch.setattr(search_module, "_record_types_for_sources", lambda sources: ())
    no_mapped_families = await search_module.search_context_async(
        driver=SimpleNamespace(),
        repo_id="demo",
        query="jwt",
        group_id="main",
        sources=["claims"],
        limit=5,
    )
    assert (
        no_mapped_families["message"]
        == "No indexed record families are mapped to the requested sources."
    )
