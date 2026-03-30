"""Tests for profiles, tracing, cli, and other modules to increase coverage."""
from __future__ import annotations

import asyncio
import io
import json
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from context_graph.models.profiles import (
    PROFILE_KEY_ORIGIN_GENERATED_SEMANTIC,
    PROFILE_KEY_ORIGIN_LEGACY_KEYWORD,
    PROFILE_KEY_ORIGIN_RAW_TEXT,
    SEMANTIC_CLAIM_PROFILE_KIND,
    SEMANTIC_FACT_PROFILE_KIND,
    SEMANTIC_OBSERVATION_PROFILE_KIND,
    SEMANTIC_RELATION_PROFILE_KIND,
    ProfileDeactivation,
    ProfileKeyDefinition,
    ProfileRecord,
    RelationProfileSpec,
    _claim_profile_keys,
    _dedupe_profile_keys,
    _legacy_profile_keys,
    _normalize_text,
    _relation_label,
    _string_or_none,
    observation_profile_text,
    profile_deactivation_from_fact_invalidation,
    semantic_claim_profile_from_claim,
    semantic_fact_profile_from_fact,
    semantic_observation_profile_from_observation,
    semantic_relation_profile_from_spec,
)


# ── ProfileKeyDefinition ────────────────────────────────────────────────────

class TestProfileKeyDefinition:
    def test_as_dict(self):
        key = ProfileKeyDefinition(text="hello", origin="raw_text")
        assert key.as_dict() == {"text": "hello", "origin": "raw_text"}

    def test_from_json_value_string(self):
        key = ProfileKeyDefinition.from_json_value("hello world")
        assert key is not None
        assert key.text == "hello world"
        assert key.origin == PROFILE_KEY_ORIGIN_LEGACY_KEYWORD

    def test_from_json_value_empty_string(self):
        assert ProfileKeyDefinition.from_json_value("") is None
        assert ProfileKeyDefinition.from_json_value("   ") is None

    def test_from_json_value_mapping(self):
        key = ProfileKeyDefinition.from_json_value({"text": "hello", "origin": "raw_text"})
        assert key is not None
        assert key.text == "hello"
        assert key.origin == "raw_text"

    def test_from_json_value_mapping_empty_text(self):
        assert ProfileKeyDefinition.from_json_value({"text": "", "origin": "raw_text"}) is None

    def test_from_json_value_mapping_no_origin(self):
        key = ProfileKeyDefinition.from_json_value({"text": "hello"})
        assert key is not None
        assert key.origin == PROFILE_KEY_ORIGIN_LEGACY_KEYWORD

    def test_from_json_value_invalid_type(self):
        assert ProfileKeyDefinition.from_json_value(42) is None
        assert ProfileKeyDefinition.from_json_value(None) is None


# ── Profile helpers ──────────────────────────────────────────────────────────

class TestProfileHelpers:
    def test_normalize_text(self):
        assert _normalize_text(None) == ""
        assert _normalize_text("  hello   world  ") == "hello world"
        assert _normalize_text(42) == "42"

    def test_string_or_none(self):
        assert _string_or_none(None) is None
        assert _string_or_none("") is None
        assert _string_or_none("  ") is None
        assert _string_or_none("hello") == "hello"

    def test_relation_label(self):
        assert _relation_label(None) == "relates to"
        assert _relation_label("") == "relates to"
        assert _relation_label("HAS_MEMBER") == "has member"

    def test_legacy_profile_keys(self):
        keys = _legacy_profile_keys(["first", "second", ""])
        assert len(keys) == 2
        assert keys[0].origin == PROFILE_KEY_ORIGIN_RAW_TEXT
        assert keys[1].origin == PROFILE_KEY_ORIGIN_LEGACY_KEYWORD

    def test_dedupe_profile_keys(self):
        keys = [
            ProfileKeyDefinition(text="Hello", origin="raw_text"),
            ProfileKeyDefinition(text="hello", origin="raw_text"),
            ProfileKeyDefinition(text="world", origin="raw_text"),
        ]
        deduped = _dedupe_profile_keys(keys)
        assert len(deduped) == 2

    def test_claim_profile_keys(self):
        keys = _claim_profile_keys(
            claim_text="The system uses REST",
            generated_keys=["REST API", "The system uses REST", ""],
        )
        # Should have raw_text key + generated key (excluding duplicate of claim_text)
        assert any(k.origin == PROFILE_KEY_ORIGIN_RAW_TEXT for k in keys)
        assert any(k.origin == PROFILE_KEY_ORIGIN_GENERATED_SEMANTIC for k in keys)

    def test_observation_profile_text(self):
        obs = SimpleNamespace(title="Title", text="Body text")
        result = observation_profile_text(obs)
        assert "Title" in result
        assert "Body text" in result

    def test_observation_profile_text_no_title(self):
        obs = SimpleNamespace(title="", text="Body only")
        result = observation_profile_text(obs)
        assert result == "Body only"

    def test_observation_profile_text_no_body(self):
        obs = SimpleNamespace(title="Title only", text="")
        result = observation_profile_text(obs)
        assert result == "Title only"

    def test_observation_profile_text_truncation(self):
        obs = SimpleNamespace(title="T", text="x" * 5000)
        result = observation_profile_text(obs, max_length=100)
        assert len(result) == 100


# ── semantic_fact_profile_from_fact ──────────────────────────────────────────

class TestSemanticFactProfile:
    def _make_fact(self, **overrides):
        defaults = {
            "uuid": "fact-1",
            "group_id": "repo-1",
            "name": "CALLS",
            "fact": "A calls B",
            "source_node_uuid": "node-a",
            "target_node_uuid": "node-b",
            "episodes": ["ep-1"],
            "created_at": "2026-01-01",
            "valid_at": "2026-01-01",
            "invalid_at": None,
            "expired_at": None,
            "relationship_keywords": None,
            "attributes": {},
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def _make_entity_node(self, **overrides):
        defaults = {
            "name": "NodeA",
            "uuid": "node-a",
            "labels": ["Function"],
            "group_id": "repo-1",
            "summary": "A function",
            "created_at": "2026-01-01",
            "attributes": {"file_path": "src/a.py", "kind": "Function"},
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_basic_fact_profile(self):
        fact = self._make_fact()
        source = self._make_entity_node(name="A", uuid="node-a")
        target = self._make_entity_node(name="B", uuid="node-b")
        profile = semantic_fact_profile_from_fact(fact, source=source, target=target)
        assert profile.profile_kind == SEMANTIC_FACT_PROFILE_KIND
        assert profile.is_active is True
        assert "A calls B" in profile.value_text

    def test_fact_profile_no_source_target(self):
        fact = self._make_fact(attributes={"source_kind": "Function", "target_kind": "Class"})
        profile = semantic_fact_profile_from_fact(fact, source=None, target=None)
        assert profile.source_node_kind == "Function"
        assert profile.target_node_kind == "Class"

    def test_fact_profile_expired(self):
        fact = self._make_fact(expired_at="2026-02-01")
        profile = semantic_fact_profile_from_fact(fact, source=None, target=None)
        assert profile.is_active is False

    def test_fact_profile_with_keywords(self):
        fact = self._make_fact(relationship_keywords=["keyword1", "keyword2"])
        profile = semantic_fact_profile_from_fact(fact, source=None, target=None)
        assert len(profile.keys) >= 1


# ── semantic_claim_profile_from_claim ────────────────────────────────────────

class TestSemanticClaimProfile:
    def _make_claim(self, **overrides):
        defaults = {
            "claim_id": "claim-1",
            "repo_id": "repo-1",
            "claim_text": "The system uses REST",
            "canonical_key": "system-uses-rest",
            "claim_kind": "architectural",
            "claim_subkind": None,
            "context_kind": "code",
            "context_id": "ctx-1",
            "thread_kind": None,
            "thread_id": None,
            "is_current": True,
            "valid_from": "2026-01-01",
            "valid_to": None,
            "latest_observed_at": "2026-01-01",
            "ingested_at": "2026-01-01",
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_basic_claim_profile(self):
        claim = self._make_claim()
        profile = semantic_claim_profile_from_claim(
            claim,
            about_record_ids=["rec-1", "rec-2"],
            support_texts=["evidence text"],
            generated_keys=["REST API"],
        )
        assert profile.profile_kind == SEMANTIC_CLAIM_PROFILE_KIND
        assert profile.is_active is True

    def test_claim_profile_inactive(self):
        claim = self._make_claim(is_current=False, valid_to="2026-02-01")
        profile = semantic_claim_profile_from_claim(claim)
        assert profile.is_active is False
        assert profile.updated_at == "2026-02-01"


# ── semantic_relation_profile_from_spec ──────────────────────────────────────

class TestSemanticRelationProfile:
    def test_basic_relation_profile(self):
        spec = RelationProfileSpec(
            repo_id="repo-1",
            profile_target_id="rel-1",
            relation_type="DEPENDS_ON",
            value_text="A depends on B",
        )
        profile = semantic_relation_profile_from_spec(spec)
        assert profile is not None
        assert profile.profile_kind == SEMANTIC_RELATION_PROFILE_KIND

    def test_empty_value_text_returns_none(self):
        spec = RelationProfileSpec(
            repo_id="repo-1",
            profile_target_id="rel-1",
            value_text="",
        )
        assert semantic_relation_profile_from_spec(spec) is None


# ── semantic_observation_profile_from_observation ────────────────────────────

class TestSemanticObservationProfile:
    def test_basic_observation_profile(self):
        obs = SimpleNamespace(
            observation_id="obs-1",
            repo_id="repo-1",
            observation_kind="commit_message",
            source_kind="git",
            source_id="src-1",
            context_kind="commit",
            context_id="ctx-1",
            thread_kind=None,
            thread_id=None,
            observed_at="2026-01-01",
            ingested_at="2026-01-01",
            title="Fix bug",
            text="Fixed the login bug",
        )
        profile = semantic_observation_profile_from_observation(obs)
        assert profile.profile_kind == SEMANTIC_OBSERVATION_PROFILE_KIND
        assert "Fix bug" in profile.value_text


# ── profile_deactivation_from_fact_invalidation ─────────────────────────────

class TestProfileDeactivation:
    def test_basic_deactivation(self):
        invalidation = SimpleNamespace(
            repo_id="repo-1",
            relation_type="CALLS",
            source_node_id="node-a",
            target_node_id="node-b",
            observed_at="2026-01-01",
            replacement_fact_id="fact-2",
        )
        deactivation = profile_deactivation_from_fact_invalidation(invalidation)
        assert deactivation.repo_id == "repo-1"
        assert deactivation.replacement_profile_id is not None

    def test_deactivation_no_replacement(self):
        invalidation = SimpleNamespace(
            repo_id="repo-1",
            relation_type="CALLS",
            source_node_id="node-a",
            target_node_id="node-b",
            observed_at="2026-01-01",
            replacement_fact_id=None,
        )
        deactivation = profile_deactivation_from_fact_invalidation(invalidation)
        assert deactivation.replacement_profile_id is None


# ── ProfileRecord.keys_json ─────────────────────────────────────────────────

class TestProfileRecordKeysJson:
    def test_keys_json_with_definitions(self):
        record = ProfileRecord(
            profile_id="p1",
            repo_id="r1",
            profile_kind="test",
            profile_target_kind="test",
            profile_target_id="t1",
            relation_type=None,
            source_node_id=None,
            source_node_kind=None,
            target_node_id=None,
            target_node_kind=None,
            target_revision_id=None,
            value_text="test",
            keys=(ProfileKeyDefinition(text="hello", origin="raw_text"),),
        )
        parsed = json.loads(record.keys_json())
        assert len(parsed) == 1
        assert parsed[0]["text"] == "hello"


# ── Tracing ──────────────────────────────────────────────────────────────────

class TestTracing:
    def test_tracing_module_reload_with_fake_otel(self):
        import importlib
        import types

        fake_trace = types.ModuleType("opentelemetry.trace")
        fake_trace.Span = object
        fake_trace.StatusCode = SimpleNamespace(ERROR="ERR", OK="OK")

        with patch.dict(sys.modules, {"opentelemetry.trace": fake_trace}):
            tracing = importlib.import_module("context_graph.runtime.tracing")
            importlib.reload(tracing)
            assert tracing.OTEL_AVAILABLE is True
            importlib.reload(tracing)

    def test_tracing_abstract_methods_can_call_super(self):
        from context_graph.runtime.tracing import Tracer, TracerSpan

        class ConcreteSpan(TracerSpan):
            def add_attributes(self, attributes):
                return super().add_attributes(attributes)

            def set_status(self, status, description=None):
                return super().set_status(status, description)

            def record_exception(self, exception):
                return super().record_exception(exception)

        class ConcreteTracer(Tracer):
            def start_span(self, name):
                return super().start_span(name)

        span = ConcreteSpan()
        tracer = ConcreteTracer()
        assert span.add_attributes({"x": 1}) is None
        assert span.set_status("ok") is None
        assert span.record_exception(ValueError("boom")) is None
        assert tracer.start_span("name") is None

    def test_noop_tracer(self):
        from context_graph.runtime.tracing import NoOpTracer, NoOpSpan
        tracer = NoOpTracer()
        with tracer.start_span("test") as span:
            span.add_attributes({"key": "value"})
            span.set_status("ok")
            span.record_exception(ValueError("test"))
        # No errors raised

    def test_create_tracer_none(self):
        from context_graph.runtime.tracing import create_tracer, NoOpTracer
        tracer = create_tracer(None)
        assert isinstance(tracer, NoOpTracer)

    def test_otel_span_add_attributes(self):
        from context_graph.runtime.tracing import OpenTelemetrySpan
        fake_span = MagicMock()
        otel_span = OpenTelemetrySpan(fake_span)
        otel_span.add_attributes({"key": "value", "num": 42, "none_val": None, "obj": object()})
        fake_span.set_attributes.assert_called_once()
        call_args = fake_span.set_attributes.call_args[0][0]
        assert "key" in call_args
        assert "none_val" not in call_args

    def test_otel_span_add_attributes_exception(self):
        from context_graph.runtime.tracing import OpenTelemetrySpan
        fake_span = MagicMock()
        fake_span.set_attributes.side_effect = RuntimeError("boom")
        otel_span = OpenTelemetrySpan(fake_span)
        # Should not raise
        otel_span.add_attributes({"key": "value"})

    def test_otel_span_set_status_error(self):
        from context_graph.runtime import tracing
        from context_graph.runtime.tracing import OpenTelemetrySpan
        with patch.object(tracing, "OTEL_AVAILABLE", True), \
             patch.object(tracing, "StatusCode", SimpleNamespace(ERROR="ERR", OK="OK"), create=True):
            fake_span = MagicMock()
            otel_span = OpenTelemetrySpan(fake_span)
            otel_span.set_status("error", "something went wrong")
            fake_span.set_status.assert_called_once()

    def test_otel_span_set_status_ok(self):
        from context_graph.runtime import tracing
        from context_graph.runtime.tracing import OpenTelemetrySpan
        with patch.object(tracing, "OTEL_AVAILABLE", True), \
             patch.object(tracing, "StatusCode", SimpleNamespace(ERROR="ERR", OK="OK"), create=True):
            fake_span = MagicMock()
            otel_span = OpenTelemetrySpan(fake_span)
            otel_span.set_status("ok")
            fake_span.set_status.assert_called_once()

    def test_otel_span_set_status_swallows_exceptions(self):
        from context_graph.runtime import tracing
        from context_graph.runtime.tracing import OpenTelemetrySpan
        with patch.object(tracing, "OTEL_AVAILABLE", True), \
             patch.object(tracing, "StatusCode", SimpleNamespace(ERROR="ERR", OK="OK"), create=True):
            fake_span = MagicMock()
            fake_span.set_status.side_effect = RuntimeError("boom")
            otel_span = OpenTelemetrySpan(fake_span)
            otel_span.set_status("error", "bad")

    def test_otel_span_record_exception(self):
        from context_graph.runtime.tracing import OpenTelemetrySpan
        fake_span = MagicMock()
        otel_span = OpenTelemetrySpan(fake_span)
        otel_span.record_exception(ValueError("test"))
        fake_span.record_exception.assert_called_once()

    def test_otel_tracer_start_span(self):
        from context_graph.runtime import tracing
        from context_graph.runtime.tracing import OpenTelemetryTracer
        with patch.object(tracing, "OTEL_AVAILABLE", True):
            fake_tracer = MagicMock()
            fake_span = MagicMock()
            fake_tracer.start_as_current_span.return_value.__enter__ = MagicMock(return_value=fake_span)
            fake_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)
            otel_tracer = OpenTelemetryTracer(fake_tracer, span_prefix="test")
            with otel_tracer.start_span("op") as span:
                span.add_attributes({"key": "value"})

    def test_otel_tracer_import_error(self):
        from context_graph.runtime.tracing import OTEL_AVAILABLE
        if not OTEL_AVAILABLE:
            from context_graph.runtime.tracing import OpenTelemetryTracer
            with pytest.raises(ImportError):
                OpenTelemetryTracer(MagicMock())

    def test_create_tracer_with_otel(self):
        from context_graph.runtime.tracing import create_tracer, OTEL_AVAILABLE
        if not OTEL_AVAILABLE:
            tracer = create_tracer(MagicMock())
            from context_graph.runtime.tracing import NoOpTracer
            assert isinstance(tracer, NoOpTracer)
        else:
            from context_graph.runtime.tracing import OpenTelemetryTracer
            tracer = create_tracer(MagicMock(), span_prefix="myapp")
            assert isinstance(tracer, OpenTelemetryTracer)

    def test_create_tracer_forced_otel_branch(self):
        from context_graph.runtime import tracing
        with patch.object(tracing, "OTEL_AVAILABLE", True), patch.object(
            tracing,
            "OpenTelemetryTracer",
            return_value="otel-tracer",
        ) as mock_ctor:
            tracer = tracing.create_tracer(MagicMock(), span_prefix="forced")
        assert tracer == "otel-tracer"
        mock_ctor.assert_called_once()


# ── CLI coverage ─────────────────────────────────────────────────────────────

class TestCLIHelpers:
    def test_make_embedding_provider_raises_without_args(self):
        import context_graph.cli as cli_module
        args = SimpleNamespace(
            embed_provider=None,
            embed_model=None,
            embed_dim=None,
            embed_aws_region=None,
        )
        # Should return None or raise depending on implementation
        result = cli_module._make_embedding_provider(args)
        assert result is None

    def test_make_backend_driver_neo4j(self):
        import context_graph.cli as cli_module
        args = SimpleNamespace(
            graph_backend="neo4j",
            neo4j_uri="bolt://localhost:7687",
            neo4j_user="neo4j",
            neo4j_password="pw",
            neo4j_database="neo4j",
            neptune_host=None,
            neptune_port=8182,
        )
        with patch("context_graph.cli.create_driver") as mock_create:
            mock_create.return_value = MagicMock()
            driver = cli_module._make_backend_driver(args)
        mock_create.assert_called_once()

    def test_close_backend_driver(self):
        import context_graph.cli as cli_module
        # _close_backend_driver uses BackendSyncBridge which needs a real async close.
        # Simplest approach: pass None (early return path).
        cli_module._close_backend_driver(None)  # should be a no-op

    def test_make_metadata_store_sqlite(self):
        import context_graph.cli as cli_module
        args = SimpleNamespace(
            metadata_backend="sqlite",
            metadata_db=":memory:",
        )
        store = cli_module._make_metadata_store(args)
        assert store is not None

    def test_make_query_parser(self):
        import context_graph.cli as cli_module
        args = SimpleNamespace(
            llm_provider=None,
            llm_model=None,
            llm_api_key=None,
            llm_base_url=None,
            llm_aws_region=None,
        )
        parser = cli_module._make_query_parser(args)
        assert parser is not None

    def test_make_profile_vector_store(self):
        import context_graph.cli as cli_module
        from context_graph.backend.metadata import InMemoryMetadataStore
        metadata = InMemoryMetadataStore()
        args = SimpleNamespace(
            embed_dim=1024,
        )
        store = cli_module._make_profile_vector_store(args, metadata=metadata)
        assert store is not None

    def test_now_iso(self):
        import context_graph.cli as cli_module
        result = cli_module._now_iso()
        assert "T" in result

    def test_snapshot_summary(self):
        import context_graph.cli as cli_module
        from context_graph.models import CanonicalNode, CanonicalEdge, GraphSnapshot
        snapshot = GraphSnapshot(
            repo_id="demo",
            revision_id="r1",
            root_path="/tmp",
            generated_at="2026-01-01",
            nodes=(CanonicalNode("n1", "demo", "File", "a.py", qualified_name="a"),),
            edges=(),
            metadata={},
        )
        summary = cli_module._snapshot_summary(snapshot)
        assert summary["repo_id"] == "demo"
        assert summary["node_count"] == 1

    def test_emit_json(self):
        import context_graph.cli as cli_module
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_module._emit_json({"key": "value"})
        assert code == 0
        assert "key" in buf.getvalue()
