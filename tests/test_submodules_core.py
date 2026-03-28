from __future__ import annotations

import io
import importlib.util
import runpy
import sys
import tempfile
import types
from pathlib import Path
from contextlib import redirect_stdout
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import context_graph
from context_graph import embeddings, llm, query_parser
from context_graph.models import (
    CanonicalEdge,
    CanonicalNode,
    GraphSnapshot,
    SnapshotApplyResult,
    SnapshotDiff,
    canonical_json,
)
from context_graph.runtime import tracing
import context_graph.ingestion as ingestion_pkg
import context_graph.runtime.llm as runtime_llm_pkg
import context_graph.runtime.rerank as rerank_pkg
from context_graph.runtime.llm.cache import LLMCache
from context_graph.runtime.llm.config import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE, LLMConfig, ModelSize
from context_graph.runtime.llm.errors import EmptyResponseError, RateLimitError, RefusalError
from context_graph.runtime.llm.token_tracker import TokenUsageTracker
from context_graph.runtime.rerank.noop_reranker import NoOpCrossEncoder


class _FakeEmbeddingClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def create_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(len(text))] for text in texts]


class _FakeLLMClient:
    async def generate_response(self, prompt, **kwargs):
        assert kwargs["prompt_name"] == "context_graph.query_parser.parse"
        assert prompt[0].role == "system"
        return {"high_level_keywords": ["Architecture", "Architecture", "Testing "]}


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes = None
        self.status_calls: list[tuple[object, object]] = []
        self.recorded: list[Exception] = []

    def set_attributes(self, attributes):
        self.attributes = attributes

    def set_status(self, status, description):
        self.status_calls.append((status, description))

    def record_exception(self, exception):
        self.recorded.append(exception)


class _BrokenSpan(_FakeSpan):
    def set_attributes(self, attributes):
        raise RuntimeError("bad attrs")

    def set_status(self, status, description):
        raise RuntimeError("bad status")

    def record_exception(self, exception):
        raise RuntimeError("bad record")


class _FakeTracer:
    def __init__(self, span: _FakeSpan, *, should_fail: bool = False) -> None:
        self.span = span
        self.should_fail = should_fail
        self.started: list[str] = []

    def start_as_current_span(self, name: str):
        self.started.append(name)
        if self.should_fail:
            raise RuntimeError("boom")

        span = self.span

        class _Manager:
            def __enter__(self):
                return span

            def __exit__(self, exc_type, exc, tb):
                return False

        return _Manager()


def _load_module_from_path(module_name: str, path: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_models_round_trip_maps_and_alias_properties() -> None:
    node = CanonicalNode(
        node_id="node-1",
        repo_id="repo-1",
        kind="Function",
        name="run",
        qualified_name="pkg.module.run",
        file_path="src/main.py",
        start_line=3,
        end_line=9,
        properties={"b": 2, "a": 1},
    )
    edge = CanonicalEdge(
        edge_id="edge-1",
        repo_id="repo-1",
        edge_type="CALLS",
        source_id="node-1",
        target_id="node-2",
        properties={"z": 9, "y": 8},
    )
    snapshot = GraphSnapshot(
        repo_id="repo-1",
        revision_id="rev-1",
        root_path="/tmp/repo",
        generated_at="2026-03-27T12:00:00+00:00",
        nodes=(node,),
        edges=(edge,),
        metadata={"source": "test"},
    )

    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert node.properties_json() == '{"a":1,"b":2}'
    assert edge.properties_json() == '{"y":8,"z":9}'
    assert snapshot.node_map()["node-1"] == node
    assert snapshot.edge_map()["edge-1"] == edge
    assert GraphSnapshot.from_dict(snapshot.to_dict()) == snapshot
    empty_diff = SnapshotDiff(
        repo_id="repo-1",
        previous_revision_id=None,
        new_revision_id="rev-1",
        added_nodes={},
        changed_nodes={},
        removed_nodes={},
        added_edges={},
        changed_edges={},
        removed_edges={},
    )
    assert empty_diff.is_empty()

    result = SnapshotApplyResult(
        snapshot=snapshot,
        diff=empty_diff,
        operations_emitted=4,
        operations_applied=2,
    )
    assert result.statements_emitted == 4
    assert result.statements_executed == 2


def test_embeddings_and_llm_settings_cover_env_validation_and_factories() -> None:
    provider = embeddings.EmbeddingProvider(_FakeEmbeddingClient())
    assert provider.embed_texts([" one\n", "", "two   words", "  "]) == [[3.0], [9.0]]
    assert provider.embed_texts(["", "   "]) == []
    assert embeddings.load_embedding_settings(SimpleNamespace(embed_provider=None)) is None

    with patch.dict(
        "os.environ",
        {
            "CONTEXT_GRAPH_EMBED_PROVIDER": "bedrock",
            "CONTEXT_GRAPH_EMBED_DIMENSIONS": "1536",
            "AWS_DEFAULT_REGION": "ca-central-1",
        },
        clear=True,
    ):
        settings = embeddings.load_embedding_settings()
    assert settings == embeddings.EmbeddingSettings(provider="bedrock", dimensions=1536, aws_region="ca-central-1")

    with pytest.raises(RuntimeError, match="unsupported embedding provider"):
        embeddings.load_embedding_settings(SimpleNamespace(embed_provider="custom"))

    with patch.dict(
        "os.environ",
        {
            "CONTEXT_GRAPH_EMBED_PROVIDER": "openai",
            "CONTEXT_GRAPH_EMBED_API_KEY": "env-secret",
        },
        clear=True,
    ):
        openai_settings = embeddings.load_embedding_settings()
    assert openai_settings.api_key == "env-secret"

    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(RuntimeError, match="OpenAI embeddings require"):
            embeddings.load_embedding_settings(SimpleNamespace(embed_provider="openai"))

    args = SimpleNamespace(
        llm_provider="openai",
        llm_model="gpt-4.1-mini",
        llm_small_model="gpt-4.1-nano",
        llm_base_url="https://example.test",
        llm_api_key="secret",
        aws_region=None,
    )
    llm_settings = llm.load_episode_llm_settings(args)
    assert llm_settings == llm.EpisodeLLMSettings(
        provider="openai",
        model="gpt-4.1-mini",
        small_model="gpt-4.1-nano",
        base_url="https://example.test",
        api_key="secret",
        aws_region=None,
    )

    fake_openai_module = SimpleNamespace(OpenAIClient=lambda **kwargs: ("openai", kwargs))
    fake_bedrock_module = SimpleNamespace(BedrockLLMClient=lambda **kwargs: ("bedrock", kwargs))
    with patch.dict(
        "sys.modules",
        {
            "context_graph.runtime.llm.openai_client": fake_openai_module,
            "context_graph.runtime.llm.bedrock_client": fake_bedrock_module,
            "context_graph.runtime.embedding.openai": SimpleNamespace(
                OpenAIEmbedder=lambda **kwargs: ("embed-openai", kwargs),
                OpenAIEmbedderConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
            "context_graph.runtime.embedding.bedrock": SimpleNamespace(
                BedrockTitanEmbedder=lambda **kwargs: ("embed-bedrock", kwargs),
                BedrockTitanEmbedderConfig=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
        },
    ):
        assert llm.make_episode_llm_client(llm_settings)[0] == "openai"
        assert llm.make_episode_llm_client(
            llm.EpisodeLLMSettings(provider="bedrock", aws_region="ca-central-1")
        )[0] == "bedrock"
        assert embeddings.make_embedding_provider(
            embeddings.EmbeddingSettings(provider="openai", api_key="secret", dimensions=256)
        ).client[0] == "embed-openai"
        assert embeddings.make_embedding_provider(
            embeddings.EmbeddingSettings(provider="bedrock", aws_region="ca-central-1", dimensions=512)
        ).client[0] == "embed-bedrock"

    assert embeddings.make_embedding_provider(None) is None
    with pytest.raises(RuntimeError, match="unsupported embedding provider"):
        embeddings.make_embedding_provider(
            embeddings.EmbeddingSettings(provider="custom")
        )

    class _Loop:
        def __init__(self) -> None:
            self.closed = False

        def run_until_complete(self, coro):
            coro.close()
            return "loop-result"

        def close(self):
            self.closed = True

    fake_loop = _Loop()
    with patch("context_graph.embeddings.asyncio.get_running_loop", return_value=object()), patch(
        "context_graph.embeddings.asyncio.new_event_loop",
        return_value=fake_loop,
    ):
        assert embeddings._run_async(__import__("asyncio").sleep(0)) == "loop-result"
    assert fake_loop.closed is True

    assert llm.load_episode_llm_settings() is None
    with pytest.raises(RuntimeError, match="unsupported LLM provider"):
        llm.load_episode_llm_settings(SimpleNamespace(llm_provider="custom"))
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(RuntimeError, match="OpenAI episode extraction requires"):
            llm.load_episode_llm_settings(SimpleNamespace(llm_provider="openai"))
    assert llm.make_episode_llm_client(None) is None
    with pytest.raises(RuntimeError, match="unsupported LLM provider"):
        llm.make_episode_llm_client(llm.EpisodeLLMSettings(provider="custom"))


def test_query_parser_extracts_anchors_keywords_and_intents() -> None:
    parser = query_parser.QueryParser(llm_client=_FakeLLMClient())
    result = parser.parse(
        'Why did `pkg.module.run` change in "src/main.py" after #42 and abcdef1 for /api/v1/files?'
    )

    anchors = {(anchor.kind, anchor.value) for anchor in result.anchors}
    assert ("quoted", "pkg.module.run") in anchors
    assert ("quoted", "src/main.py") in anchors
    assert ("artifact_number", "#42") in anchors
    assert ("commit_sha", "abcdef1") in anchors
    assert ("route", "/api/v1/files?") in anchors
    assert "Architecture" in result.high_level_keywords
    assert "Testing" in result.high_level_keywords
    assert result.intent_tags == ("why",)

    assert query_parser._dedupe_texts([" A  B ", "a b", "", "C"]) == ["A B", "C"]
    assert query_parser._normalize_query_response(SimpleNamespace(model_dump=lambda: {"high_level_keywords": ["X"]})) == {
        "high_level_keywords": ["X"]
    }
    assert query_parser._normalize_query_response(object()) == {"high_level_keywords": []}
    assert query_parser.PromptMessage("user", "hello").model_dump() == {"role": "user", "content": "hello"}
    assert query_parser._extract_deterministic_anchors('#1 #1 src/main.py src/main.py PKG_CONST helper_name')[-1].value == "helper_name"
    assert query_parser._extract_deterministic_anchors("   `   `") == ()
    anchors = query_parser._extract_deterministic_anchors("#skip src/main.py")
    assert any(anchor.kind == "symbol" and anchor.value == "main.py" for anchor in anchors)
    assert query_parser._looks_like_symbol("ABC") is False
    assert query_parser._looks_like_file_path("folder/noext") is True
    assert query_parser._looks_like_file_path("main.py") is True

    async def _inner_run_async_branch() -> None:
        task = __import__("asyncio").sleep(0, result="done")
        try:
            with pytest.raises(RuntimeError):
                query_parser._run_async(task)
        finally:
            task.close()

    import asyncio
    asyncio.run(_inner_run_async_branch())

    with patch.dict(sys.modules, {"pydantic": None}):
        module = _load_module_from_path(
            "query_parser_no_pydantic",
            str(Path("context_graph/query_parser.py").resolve()),
        )
    schema = module.ParsedHighLevelKeywords.model_json_schema()
    assert schema["required"] == ["high_level_keywords"]


def test_runtime_helpers_cover_tracing_cache_token_tracking_and_entrypoints() -> None:
    span = _FakeSpan()
    with patch.object(tracing, "OTEL_AVAILABLE", True), patch.object(
        tracing,
        "StatusCode",
        SimpleNamespace(ERROR="ERROR", OK="OK"),
        create=True,
    ):
        tracer = tracing.OpenTelemetryTracer(_FakeTracer(span), span_prefix="context-graph")
        with tracer.start_span("parse") as wrapped:
            wrapped.add_attributes({"ok": True, "skip": None, "complex": datetime(2026, 3, 27, 12, 0, 0)})
            wrapped.set_status("ok", "done")
            wrapped.record_exception(RuntimeError("boom"))

    assert span.attributes["ok"] is True
    assert "complex" in span.attributes
    assert span.status_calls == [("OK", "done")]
    assert isinstance(span.recorded[0], RuntimeError)

    with patch.object(tracing, "OTEL_AVAILABLE", True):
        fallback = tracing.OpenTelemetryTracer(_FakeTracer(span, should_fail=True))
        with fallback.start_span("ignored") as wrapped:
            assert isinstance(wrapped, tracing.NoOpSpan)
    assert isinstance(tracing.create_tracer(), tracing.NoOpTracer)
    with tracing.NoOpTracer().start_span("noop") as wrapped:
        wrapped.add_attributes({"x": 1})
        wrapped.set_status("ok", "fine")
        wrapped.record_exception(RuntimeError("ignored"))

    swallowing = tracing.OpenTelemetrySpan(_BrokenSpan())
    with patch.object(tracing, "OTEL_AVAILABLE", True), patch.object(
        tracing,
        "StatusCode",
        SimpleNamespace(ERROR="ERROR", OK="OK"),
        create=True,
    ):
        swallowing.add_attributes({"x": object()})
        swallowing.set_status("error", "bad")
        swallowing.record_exception(RuntimeError("ignored"))
        created = tracing.create_tracer(_FakeTracer(_FakeSpan()), "cg")
    assert isinstance(created, tracing.OpenTelemetryTracer)
    with patch.object(tracing, "OTEL_AVAILABLE", False):
        assert isinstance(tracing.create_tracer(_FakeTracer(_FakeSpan())), tracing.NoOpTracer)
        with pytest.raises(ImportError):
            tracing.OpenTelemetryTracer(_FakeTracer(_FakeSpan()))
    tracing.TracerSpan.add_attributes(object(), {})
    tracing.TracerSpan.set_status(object(), "ok")
    tracing.TracerSpan.record_exception(object(), RuntimeError("x"))
    tracing.Tracer.start_span(object(), "name")

    tracker = TokenUsageTracker()
    tracker.record("extract.claims", 10, 3)
    tracker.record(None, 4, 1)
    tracker.record("extract.claims", 2, 5)
    usage = tracker.get_usage()
    assert usage["extract.claims"].call_count == 2
    assert usage["extract.claims"].avg_output_tokens == 4
    assert tracker.get_total_usage().total_tokens == 25
    output = io.StringIO()
    with redirect_stdout(output):
        tracker.print_summary(sort_by="prompt_name")
    assert "TOKEN USAGE SUMMARY" in output.getvalue()
    tracker.reset()
    assert tracker.get_usage() == {}
    output = io.StringIO()
    with redirect_stdout(output):
        tracker.print_summary()
    assert "No token usage recorded." in output.getvalue()

    with tempfile.TemporaryDirectory() as tmpdir:
        cache = LLMCache(tmpdir)
        cache.set("ok", {"value": 1})
        cache.set("bad", {"value": {1, 2, 3}})
        assert cache.get("ok") == {"value": 1}
        cache._conn.execute("INSERT OR REPLACE INTO cache (key, value) VALUES (?, ?)", ("broken", "{"))
        cache._conn.commit()
        assert cache.get("broken") is None
        assert cache.get("missing") is None
        cache.close()
        stale = LLMCache(tmpdir)
        stale._conn.close()
        stale.__del__()

    assert str(RateLimitError()) == "Rate limit exceeded. Please try again later."
    assert str(RefusalError("no")) == "no"
    assert str(EmptyResponseError("empty")) == "empty"
    assert ModelSize.small.value == "small"
    assert LLMConfig().max_tokens == DEFAULT_MAX_TOKENS
    assert LLMConfig().temperature == DEFAULT_TEMPERATURE

    fake_cli_module = SimpleNamespace(main=lambda: 7)
    with patch.dict(sys.modules, {"context_graph.cli": fake_cli_module}):
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("context_graph", run_name="__main__")
    assert exc.value.code == 7

    fake_server_module = SimpleNamespace(main=lambda: 9)
    with patch.dict(sys.modules, {"context_graph.server.mcp.server": fake_server_module}):
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("context_graph.server.mcp", run_name="__main__")
    assert exc.value.code == 9


def test_package_exports_and_rerank_modules_cover_lazy_loading_paths() -> None:
    fake_mod = types.ModuleType("fake_mod")
    fake_mod.TokenUsageTracker = "tracker"
    fake_mod.CanonicalNode = "node"
    fake_mod.diff_snapshots = "diff"

    def fake_import_module(module_name: str, package: str | None = None):
        assert package is not None
        return fake_mod

    with patch("context_graph.runtime.llm.import_module", side_effect=fake_import_module):
        assert runtime_llm_pkg.TokenUsageTracker == "tracker"
    with patch("context_graph.import_module", side_effect=fake_import_module):
        assert context_graph.CanonicalNode == "node"
    with patch("context_graph.ingestion.import_module", side_effect=fake_import_module):
        assert ingestion_pkg.diff_snapshots == "diff"

    assert "TokenUsageTracker" in dir(runtime_llm_pkg)
    assert "CanonicalNode" in dir(context_graph)
    with pytest.raises(AttributeError):
        _ = runtime_llm_pkg.DoesNotExist
    with pytest.raises(AttributeError):
        _ = ingestion_pkg.DoesNotExist

    assert rerank_pkg.CrossEncoderClient is not None
    assert rerank_pkg.NoOpCrossEncoder is NoOpCrossEncoder
    ranked = embeddings._run_async(NoOpCrossEncoder().rank("query", ["a", "b"]))
    assert ranked == [("a", 1.0), ("b", 1.0)]
    assert embeddings._run_async(rerank_pkg.CrossEncoderClient.rank(object(), "q", [])) is None

    fake_trace_pkg = types.ModuleType("opentelemetry")
    fake_trace_mod = types.ModuleType("opentelemetry.trace")
    fake_trace_mod.Span = object
    fake_trace_mod.StatusCode = SimpleNamespace(ERROR="ERR", OK="OK")
    with patch.dict(sys.modules, {"opentelemetry": fake_trace_pkg, "opentelemetry.trace": fake_trace_mod}):
        tracing_with_otel = _load_module_from_path(
            "tracing_with_otel",
            str(Path("context_graph/runtime/tracing.py").resolve()),
        )
    assert tracing_with_otel.OTEL_AVAILABLE is True
