from __future__ import annotations

import importlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from context_graph.code_identity.resolver import (
    SnapshotCodeResolver,
    _ancestor_folder_paths,
    _infer_hint_kind,
    _looks_like_file_basename,
    _looks_like_file_path,
    _normalize_hint_value,
)
from context_graph.models.graph import CanonicalNode, GraphSnapshot
from context_graph.models.profiles import (
    PROFILE_KEY_ORIGIN_RAW_TEXT,
    ProfileKeyDefinition,
    _dedupe_profile_keys,
)
from context_graph.provenance.grounding import resolve_about_record_id


def _make_snapshot(*, metadata: dict[str, object] | None = None) -> GraphSnapshot:
    return GraphSnapshot(
        repo_id="demo",
        revision_id="r1",
        root_path="/tmp/demo",
        generated_at="2026-03-30T12:00:00Z",
        nodes=(
            CanonicalNode(
                node_id="folder:demo:src",
                repo_id="demo",
                kind="Folder",
                name="src",
                qualified_name="demo.src",
                file_path="src",
            ),
            CanonicalNode(
                node_id="file:demo:src/app.py",
                repo_id="demo",
                kind="File",
                name="app.py",
                qualified_name="demo.src.app.__file__",
                file_path="src/app.py",
            ),
            CanonicalNode(
                node_id="function:demo:src.app.Handler.run",
                repo_id="demo",
                kind="Function",
                name="run",
                qualified_name="demo.src.app.Handler.run",
                file_path="src/app.py",
            ),
        ),
        edges=(),
        metadata=metadata or {},
    )


def test_tracing_forces_import_error_and_noop_when_otel_is_disabled() -> None:
    from context_graph.runtime import tracing

    with patch.object(tracing, "OTEL_AVAILABLE", False):
        with pytest.raises(ImportError):
            tracing.OpenTelemetryTracer(MagicMock())

        tracer = tracing.create_tracer(MagicMock())
        assert isinstance(tracer, tracing.NoOpTracer)


def test_structural_planner_normalization_and_profile_key_dedup_skip_invalid_values() -> None:
    from context_graph.ingestion.git.structural_planner import _normalized_text

    assert _normalized_text(None) == ""

    deduped = _dedupe_profile_keys(
        (
            ProfileKeyDefinition(text="   ", origin=PROFILE_KEY_ORIGIN_RAW_TEXT),
            ProfileKeyDefinition(text="valid", origin="   "),
            ProfileKeyDefinition(text="valid", origin=PROFILE_KEY_ORIGIN_RAW_TEXT),
        )
    )

    assert deduped == (
        ProfileKeyDefinition(text="valid", origin=PROFILE_KEY_ORIGIN_RAW_TEXT),
    )


def test_neptune_driver_reload_with_fake_dependencies_and_successful_aoss_client(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("context_graph.backend.neptune.driver")

    fake_boto3 = types.SimpleNamespace(
        Session=lambda: SimpleNamespace(
            get_credentials=lambda: "creds",
            region_name="ca-central-1",
        )
    )
    fake_graphs = types.SimpleNamespace(
        NeptuneAnalyticsGraph=object(),
        NeptuneGraph=lambda endpoint, port: ("db", endpoint, port),
    )
    captured: dict[str, object] = {}

    class FakeOpenSearch:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    def fake_auth(credentials, region_name, service_name):
        return ("auth", credentials, region_name, service_name)

    fake_opensearch = types.SimpleNamespace(
        OpenSearch=FakeOpenSearch,
        Urllib3AWSV4SignerAuth=fake_auth,
        Urllib3HttpConnection="http-connection",
    )

    with patch.dict(
        sys.modules,
        {
            "boto3": fake_boto3,
            "langchain_aws.graphs": fake_graphs,
            "opensearchpy": fake_opensearch,
        },
    ):
        importlib.reload(module)
        driver = module.NeptuneBackendDriver.__new__(module.NeptuneBackendDriver)
        client = module.NeptuneBackendDriver._create_aoss_client(
            driver,
            module.NeptuneConfig(host="neptune-db://demo", aoss_host="search.example.com"),
        )

        assert isinstance(client, FakeOpenSearch)
        assert captured["hosts"] == [{"host": "search.example.com", "port": 443}]
        assert captured["http_auth"] == ("auth", "creds", "ca-central-1", "aoss")

    importlib.reload(module)


def test_code_identity_helper_branches_and_grounding_resolution() -> None:
    snapshot = _make_snapshot()
    resolver = SnapshotCodeResolver(snapshot)

    assert resolver.resolve_about_hint("   ") is None
    assert resolver.resolve_about_record_id("missing.py") is None
    assert resolver.resolve_about_record_id(("src/app.py", "file_path")) == "file:demo:src/app.py@r1"

    changed = resolver.resolve_changed_files(("", "src/app.py"))
    assert tuple(node.node_id for node in changed) == (
        "file:demo:src/app.py",
        "folder:demo:src",
    )

    folder_candidates = resolver._folder_path_candidates(snapshot.nodes[2])
    assert "src/app.py" in folder_candidates

    assert _ancestor_folder_paths("", resolver.canonicalizer) == ()
    assert _normalize_hint_value("`src/app.py`,") == "src/app.py"
    assert _normalize_hint_value("   ''   ") == ""
    assert _infer_hint_kind("") is None
    assert _infer_hint_kind("file:demo:123") == "node_id"
    assert _infer_hint_kind("src/app.py") == "file_path"
    assert _infer_hint_kind("Handler.run") == "qualified_name_suffix"
    assert _looks_like_file_path("src/app.py") is True
    assert _looks_like_file_basename("src/app.py") is False

    assert resolve_about_record_id(None, "src/app.py") is None
    assert resolve_about_record_id(snapshot, "missing.py") is None
    assert resolve_about_record_id(snapshot, "src/app.py") == "file:demo:src/app.py@r1"

    active_snapshot = _make_snapshot(
        metadata={
            "deterministic_mutation_state": {
                "active_node_versions": {
                    "file:demo:src/app.py": "file:demo:src/app.py@active",
                }
            }
        }
    )
    assert resolve_about_record_id(active_snapshot, "src/app.py") == "file:demo:src/app.py@active"
