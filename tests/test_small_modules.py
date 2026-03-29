from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib
import io
import runpy
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from context_graph import embeddings
from context_graph import llm
from context_graph.memory import datetime_utils, errors, text_utils
from context_graph.query_parser import (
    _dedupe_texts,
    _normalize_query_response,
    _query_prompt,
    QueryParser,
)
from context_graph.runtime import tracing
from context_graph.runtime.llm import config as runtime_llm_config
from context_graph.runtime.llm import errors as runtime_llm_errors
from context_graph.runtime.llm.token_tracker import TokenUsageTracker


class _FakeEmbeddingClient:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def create_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(len(text))] for text in texts]


class _FakeResponseModel:
    def model_dump(self) -> dict[str, object]:
        return {"high_level_keywords": ["Architecture", "Testing"]}


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] | None = None
        self.status_calls: list[tuple[object, object]] = []
        self.recorded: list[Exception] = []

    def set_attributes(self, attributes: dict[str, object]) -> None:
        self.attributes = attributes

    def set_status(self, status: object, description: object) -> None:
        self.status_calls.append((status, description))

    def record_exception(self, exception: Exception) -> None:
        self.recorded.append(exception)


class _FakeTracer:
    def __init__(self, span: _FakeSpan | None = None, *, should_fail: bool = False) -> None:
        self.span = span or _FakeSpan()
        self.should_fail = should_fail
        self.started_names: list[str] = []

    def start_as_current_span(self, name: str):
        self.started_names.append(name)
        if self.should_fail:
            raise RuntimeError("boom")

        span = self.span

        class _Manager:
            def __enter__(self_inner):
                return span

            def __exit__(self_inner, exc_type, exc, tb):
                return False

        return _Manager()


class SmallModuleTests(unittest.TestCase):
    def test_package_main_entrypoints_raise_system_exit_with_main_return_code(self) -> None:
        with patch("context_graph.cli.main", return_value=17):
            with self.assertRaises(SystemExit) as exc:
                runpy.run_module("context_graph", run_name="__main__")
        self.assertEqual(exc.exception.code, 17)

        fake_server_module = SimpleNamespace(main=lambda: 23)
        with patch.dict(sys.modules, {"context_graph.server.mcp.server": fake_server_module}):
            with self.assertRaises(SystemExit) as exc:
                runpy.run_module("context_graph.server.mcp", run_name="__main__")
        self.assertEqual(exc.exception.code, 23)

    def test_embedding_provider_cleans_texts_and_returns_async_results(self) -> None:
        client = _FakeEmbeddingClient()
        provider = embeddings.EmbeddingProvider(client)

        result = provider.embed_texts(["  one\n", "", "two   words", "   "])

        self.assertEqual(result, [[3.0], [9.0]])
        self.assertEqual(client.calls, [["one", "two words"]])
        self.assertEqual(provider.embed_texts(["", "  "]), [])

    def test_load_embedding_settings_prefers_args_and_validates_openai_key(self) -> None:
        args = SimpleNamespace(
            embed_provider=" OpenAI ",
            embed_model="text-embedding-3-small",
            embed_dimensions="256",
            embed_base_url="https://example.test",
            embed_api_key="secret",
            aws_region=None,
        )

        settings = embeddings.load_embedding_settings(args)

        self.assertIsNotNone(settings)
        self.assertEqual(settings.provider, "openai")
        self.assertEqual(settings.dimensions, 256)
        self.assertEqual(settings.base_url, "https://example.test")
        self.assertEqual(settings.api_key, "secret")

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                embeddings.load_embedding_settings(
                    SimpleNamespace(
                        embed_provider="openai",
                        embed_model=None,
                        embed_dimensions=None,
                        embed_base_url=None,
                        embed_api_key=None,
                        aws_region=None,
                    )
                )

    def test_load_embedding_settings_supports_bedrock_and_rejects_unknown_provider(self) -> None:
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

        self.assertIsNotNone(settings)
        self.assertEqual(settings.provider, "bedrock")
        self.assertEqual(settings.dimensions, 1536)
        self.assertEqual(settings.aws_region, "ca-central-1")

        with self.assertRaises(RuntimeError):
            embeddings.load_embedding_settings(SimpleNamespace(embed_provider="custom"))

    def test_load_embedding_settings_infers_provider_from_llm_or_available_credentials(self) -> None:
        with patch.dict("os.environ", {"OPENAI_API_KEY": "secret"}, clear=True):
            settings = embeddings.load_embedding_settings(
                SimpleNamespace(
                    embed_provider=None,
                    llm_provider="openai",
                    embed_model=None,
                    embed_dimensions=None,
                    embed_base_url=None,
                    embed_api_key=None,
                    aws_region=None,
                )
            )

        self.assertIsNotNone(settings)
        self.assertEqual(settings.provider, "openai")
        self.assertEqual(settings.api_key, "secret")

        with patch.dict("os.environ", {"AWS_DEFAULT_REGION": "ca-central-1"}, clear=True):
            settings = embeddings.load_embedding_settings(
                SimpleNamespace(
                    embed_provider=None,
                    llm_provider=None,
                    embed_model=None,
                    embed_dimensions=None,
                    embed_base_url=None,
                    embed_api_key=None,
                    aws_region=None,
                )
            )

        self.assertIsNotNone(settings)
        self.assertEqual(settings.provider, "bedrock")
        self.assertEqual(settings.aws_region, "ca-central-1")

    def test_load_episode_llm_settings_validates_provider_and_api_key(self) -> None:
        args = SimpleNamespace(
            llm_provider="openai",
            llm_model="gpt-4.1-mini",
            llm_small_model="gpt-4.1-nano",
            llm_base_url="https://example.test",
            llm_api_key="key",
            aws_region=None,
        )

        settings = llm.load_episode_llm_settings(args)

        self.assertIsNotNone(settings)
        self.assertEqual(settings.provider, "openai")
        self.assertEqual(settings.small_model, "gpt-4.1-nano")

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                llm.load_episode_llm_settings(SimpleNamespace(llm_provider="openai", llm_api_key=None))

        with self.assertRaises(RuntimeError):
            llm.load_episode_llm_settings(SimpleNamespace(llm_provider="unsupported"))

    def test_make_episode_llm_client_selects_provider_and_handles_none(self) -> None:
        self.assertIsNone(llm.make_episode_llm_client(None))

        openai_settings = llm.EpisodeLLMSettings(
            provider="openai",
            model="gpt-4.1-mini",
            small_model="gpt-4.1-nano",
            base_url="https://example.test",
            api_key="secret",
        )
        bedrock_settings = llm.EpisodeLLMSettings(
            provider="bedrock",
            model="sonnet",
            small_model="sonnet",
            aws_region="ca-central-1",
        )

        with patch("context_graph.runtime.llm.openai_client.OpenAIClient", side_effect=lambda **kwargs: ("openai", kwargs)), patch(
            "context_graph.runtime.llm.bedrock_client.BedrockLLMClient",
            side_effect=lambda **kwargs: ("bedrock", kwargs),
        ):
            openai_client = llm.make_episode_llm_client(openai_settings)
            self.assertEqual(openai_client[0], "openai")
            self.assertEqual(openai_client[1]["config"].model, "gpt-4.1-mini")

            bedrock_client = llm.make_episode_llm_client(bedrock_settings)
            self.assertEqual(bedrock_client[0], "bedrock")
            self.assertEqual(bedrock_client[1]["region_name"], "ca-central-1")

        with self.assertRaises(RuntimeError):
            llm.make_episode_llm_client(
                llm.EpisodeLLMSettings(provider="unsupported")
            )

    def test_datetime_helpers_normalize_and_stringify_nested_values(self) -> None:
        naive = datetime(2024, 1, 2, 3, 4, 5)
        eastern = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=-5)))
        aware_utc = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

        self.assertIsNone(datetime_utils.ensure_utc(None))
        self.assertEqual(datetime_utils.ensure_utc(naive).tzinfo, timezone.utc)
        self.assertEqual(
            datetime_utils.ensure_utc(eastern),
            datetime(2024, 1, 2, 8, 4, 5, tzinfo=timezone.utc),
        )
        self.assertIs(datetime_utils.ensure_utc(aware_utc), aware_utc)
        self.assertEqual(datetime_utils.utc_now().tzinfo, timezone.utc)

        payload = {
            "created_at": aware_utc,
            "items": [aware_utc, (aware_utc, {"updated_at": aware_utc})],
        }
        converted = datetime_utils.convert_datetimes_to_strings(payload)
        expected = aware_utc.isoformat()
        self.assertEqual(converted["created_at"], expected)
        self.assertEqual(converted["items"][0], expected)
        self.assertEqual(converted["items"][1][0], expected)
        self.assertEqual(converted["items"][1][1]["updated_at"], expected)

    def test_truncate_at_sentence_handles_boundaries_and_fallback(self) -> None:
        self.assertEqual(text_utils.truncate_at_sentence("Short", 50), "Short")
        self.assertEqual(
            text_utils.truncate_at_sentence("First sentence. Second sentence.", 20),
            "First sentence.",
        )
        self.assertEqual(
            text_utils.truncate_at_sentence("No boundary in this fragment", 10),
            "No boundar",
        )

    def test_error_types_expose_expected_messages(self) -> None:
        self.assertEqual(str(errors.EdgeNotFoundError("e1")), "edge e1 not found")
        self.assertEqual(
            str(errors.EdgesNotFoundError(["a", "b"])),
            "None of the edges for ['a', 'b'] were found.",
        )
        self.assertEqual(
            str(errors.GroupsEdgesNotFoundError(["g1"])),
            "no edges found for group ids ['g1']",
        )
        self.assertEqual(
            str(errors.GroupsNodesNotFoundError(["g1"])),
            "no nodes found for group ids ['g1']",
        )
        self.assertEqual(str(errors.NodeNotFoundError("n1")), "node n1 not found")
        self.assertEqual(str(errors.SearchRerankerError("bad rerank")), "bad rerank")
        self.assertIn(
            "kind cannot be used as an attribute for Entity",
            str(errors.EntityTypeValidationError("Entity", "kind")),
        )
        self.assertIn(
            'group_id "team/a" must contain only alphanumeric characters',
            str(errors.GroupIdValidationError("team/a")),
        )
        invalid = errors.NodeLabelValidationError(["Bad-Label"])
        self.assertIsInstance(invalid, ValueError)
        self.assertIn('"Bad-Label"', str(invalid))

    def test_runtime_llm_config_and_error_types(self) -> None:
        config = runtime_llm_config.LLMConfig(api_key="secret")
        self.assertEqual(config.api_key, "secret")
        self.assertIsNone(config.model)
        self.assertEqual(config.temperature, runtime_llm_config.DEFAULT_TEMPERATURE)
        self.assertEqual(config.max_tokens, runtime_llm_config.DEFAULT_MAX_TOKENS)
        self.assertEqual(runtime_llm_config.ModelSize.small.value, "small")
        self.assertEqual(runtime_llm_config.ModelSize.medium.value, "medium")

        self.assertEqual(
            str(runtime_llm_errors.RateLimitError()),
            "Rate limit exceeded. Please try again later.",
        )
        self.assertEqual(str(runtime_llm_errors.RefusalError("nope")), "nope")
        self.assertEqual(str(runtime_llm_errors.EmptyResponseError("empty")), "empty")

    def test_token_usage_tracker_accumulates_resets_and_prints_summary(self) -> None:
        tracker = TokenUsageTracker()
        tracker.record("extract", 10, 2)
        tracker.record("extract", 4, 1)
        tracker.record(None, 3, 5)

        usage = tracker.get_usage()
        self.assertEqual(usage["extract"].call_count, 2)
        self.assertEqual(usage["extract"].total_tokens, 17)
        self.assertEqual(usage["extract"].avg_input_tokens, 7)
        self.assertEqual(usage["unknown"].avg_output_tokens, 5)

        usage["extract"].call_count = 999
        self.assertEqual(tracker.get_usage()["extract"].call_count, 2)

        total = tracker.get_total_usage()
        self.assertEqual(total.input_tokens, 17)
        self.assertEqual(total.output_tokens, 8)
        self.assertEqual(total.total_tokens, 25)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            tracker.print_summary(sort_by="prompt_name")
        summary = buffer.getvalue()
        self.assertIn("TOKEN USAGE SUMMARY", summary)
        self.assertIn("extract", summary)
        self.assertIn("unknown", summary)

        tracker.reset()
        self.assertEqual(tracker.get_usage(), {})
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            tracker.print_summary()
        self.assertIn("No token usage recorded.", buffer.getvalue())

    def test_tracing_helpers_cover_noop_and_opentelemetry_fallbacks(self) -> None:
        tracer = tracing.create_tracer()
        self.assertIsInstance(tracer, tracing.NoOpTracer)
        with tracer.start_span("ignored") as span:
            span.add_attributes({"ok": 1})
            span.set_status("ok")
            span.record_exception(RuntimeError("ignored"))

        fake_span = _FakeSpan()
        wrapper = tracing.OpenTelemetrySpan(fake_span)
        wrapper.add_attributes({"count": 3, "skip": None, "meta": {"a": 1}})
        self.assertEqual(fake_span.attributes, {"count": 3, "meta": "{'a': 1}"})
        wrapper.record_exception(ValueError("bad"))
        self.assertEqual(len(fake_span.recorded), 1)

        with patch.object(tracing, "OTEL_AVAILABLE", True), patch.object(
            tracing, "StatusCode", SimpleNamespace(ERROR="ERR", OK="OK"), create=True
        ):
            wrapper.set_status("error", "boom")
            wrapper.set_status("ok", "fine")
        self.assertEqual(fake_span.status_calls, [("ERR", "boom"), ("OK", "fine")])

        with patch.object(tracing, "OTEL_AVAILABLE", True):
            tracer_wrapper = tracing.OpenTelemetryTracer(_FakeTracer(), span_prefix="cg.test")
            with tracer_wrapper.start_span("operation") as span:
                self.assertIsInstance(span, tracing.OpenTelemetrySpan)

            fallback_tracer = tracing.OpenTelemetryTracer(
                _FakeTracer(should_fail=True),
                span_prefix="cg.test",
            )
            with fallback_tracer.start_span("operation") as span:
                self.assertIsInstance(span, tracing.NoOpSpan)

    def test_query_parser_helpers_cover_response_normalization_and_deduping(self) -> None:
        self.assertEqual(_normalize_query_response({"high_level_keywords": ["x"]}), {"high_level_keywords": ["x"]})
        self.assertEqual(
            _normalize_query_response(_FakeResponseModel()),
            {"high_level_keywords": ["Architecture", "Testing"]},
        )
        self.assertEqual(_normalize_query_response(object()), {"high_level_keywords": []})
        self.assertEqual(_dedupe_texts(["  Alpha ", "alpha", "Beta", "", " beta  "]), ["Alpha", "Beta"])

        prompt = _query_prompt("How did auth evolve?")
        self.assertEqual(len(prompt), 2)
        self.assertEqual(prompt[0].role, "system")
        self.assertIn("How did auth evolve?", prompt[1].content)

    def test_query_parser_extract_high_level_keywords_filters_empty_values(self) -> None:
        class FakeLLM:
            async def generate_response(self, *_args, **_kwargs):
                return {"high_level_keywords": ["Architecture", " ", None, "Testing"]}

        parser = QueryParser(llm_client=FakeLLM())
        result = parser.parse("why did auth_service change?", llm_enabled=True)

        self.assertEqual(result.high_level_keywords, ("Architecture", "Testing"))
        self.assertIn("why", result.intent_tags)

    def test_embedding_run_async_raises_when_nested_event_loop_is_active(self) -> None:
        async def inner() -> None:
            coro = asyncio.sleep(0, result="done")
            with self.assertRaises(RuntimeError):
                embeddings._run_async(coro)
            coro.close()

        async def runner() -> None:
            await inner()

        asyncio.run(runner())

    def test_mcp_config_helpers_expand_yaml_and_apply_cli_overrides(self) -> None:
        try:
            mcp_config = importlib.import_module("context_graph.server.mcp.config")
        except ModuleNotFoundError:
            self.skipTest("pydantic-settings is not installed")

        source = mcp_config.YamlSettingsSource(mcp_config.MCPServerConfig)

        with patch.dict("os.environ", {"API_KEY": "secret", "FLAG": "true"}, clear=True):
            expanded = source._expand_env_vars(
                {
                    "token": "${API_KEY}",
                    "enabled": "${FLAG:false}",
                    "missing": "${NOT_SET}",
                    "message": "Bearer ${API_KEY}",
                }
            )

        self.assertEqual(expanded["token"], "secret")
        self.assertTrue(expanded["enabled"])
        self.assertIsNone(expanded["missing"])
        self.assertEqual(expanded["message"], "Bearer secret")
        self.assertEqual(source.get_field_value("server", None), None)

        namespace_config = mcp_config.NamespaceConfig(episode_id_prefix=None)
        self.assertEqual(namespace_config.episode_id_prefix, "")

        server_config = mcp_config.MCPServerConfig()
        server_config.apply_cli_overrides(
            SimpleNamespace(
                transport="stdio",
                llm_provider="bedrock",
                model="sonnet",
                temperature=0.2,
                embedder_provider="bedrock",
                embedder_model="titan",
                database_provider="neptune",
                group_id="course-team",
                user_id="student1",
            )
        )
        self.assertEqual(server_config.server.transport, "stdio")
        self.assertEqual(server_config.llm.provider, "bedrock")
        self.assertEqual(server_config.llm.model, "sonnet")
        self.assertEqual(server_config.embedder.provider, "bedrock")
        self.assertEqual(server_config.database.provider, "neptune")
        self.assertEqual(server_config.namespace.group_id, "course-team")
        self.assertEqual(server_config.namespace.user_id, "student1")

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = importlib.import_module("pathlib").Path(temp_dir) / "config.yaml"
            config_path.write_text(
                "server:\n"
                "  host: ${HOST:127.0.0.1}\n"
                "  port: 9000\n"
                "namespace:\n"
                "  episode_id_prefix: ${PREFIX:cg}\n",
                encoding="utf-8",
            )
            yaml_source = mcp_config.YamlSettingsSource(mcp_config.MCPServerConfig, config_path)
            with patch.dict("os.environ", {"HOST": "0.0.0.0"}, clear=True):
                loaded = yaml_source()
            self.assertEqual(loaded["server"]["host"], "0.0.0.0")
            self.assertEqual(loaded["server"]["port"], 9000)
            self.assertEqual(loaded["namespace"]["episode_id_prefix"], "cg")

            missing_source = mcp_config.YamlSettingsSource(
                mcp_config.MCPServerConfig,
                config_path.with_name("missing.yaml"),
            )
            self.assertEqual(missing_source(), {})

            customized = mcp_config.MCPServerConfig.settings_customise_sources(
                mcp_config.MCPServerConfig,
                mcp_config.PydanticBaseSettingsSource,
                mcp_config.PydanticBaseSettingsSource,
                mcp_config.PydanticBaseSettingsSource,
                mcp_config.PydanticBaseSettingsSource,
            )
            self.assertEqual(len(customized), 4)

    def test_runtime_factories_validate_and_build_expected_clients(self) -> None:
        try:
            factories = importlib.import_module("context_graph.runtime.providers.factories")
            mcp_config = importlib.import_module("context_graph.server.mcp.config")
        except ModuleNotFoundError:
            self.skipTest("pydantic-settings is not installed")

        logger = SimpleNamespace(info=lambda *args, **kwargs: None)

        with self.assertRaises(ValueError):
            factories._validate_api_key("OpenAI", None, logger)
        self.assertEqual(factories._validate_api_key("OpenAI", "key", logger), "key")

        llm_openai = mcp_config.LLMConfig(
            provider="openai",
            model="gpt-5-mini",
            temperature=0.1,
            max_tokens=123,
            providers=mcp_config.LLMProvidersConfig(
                openai=mcp_config.OpenAIProviderConfig(api_key="secret", api_url="https://example.test")
            ),
        )
        llm_bedrock = mcp_config.LLMConfig(provider="bedrock", model="sonnet", temperature=None, max_tokens=456)

        with patch.object(factories, "OpenAIClient", side_effect=lambda **kwargs: ("openai", kwargs)), patch.object(
            factories,
            "BedrockLLMClient",
            side_effect=lambda **kwargs: ("bedrock", kwargs),
        ):
            reasoning_client = factories.LLMClientFactory.create(llm_openai)
            self.assertEqual(reasoning_client[0], "openai")
            self.assertEqual(reasoning_client[1]["reasoning"], "minimal")
            self.assertEqual(reasoning_client[1]["verbosity"], "low")

            bedrock_client = factories.LLMClientFactory.create(llm_bedrock)
            self.assertEqual(bedrock_client[0], "bedrock")
            self.assertEqual(bedrock_client[1]["config"].temperature, 1.0)

            non_reasoning_client = factories.LLMClientFactory.create(
                mcp_config.LLMConfig(
                    provider="openai",
                    model="gpt-4o-mini",
                    temperature=0.3,
                    max_tokens=111,
                    providers=mcp_config.LLMProvidersConfig(
                        openai=mcp_config.OpenAIProviderConfig(
                            api_key="secret",
                            api_url="https://example.test",
                        )
                    ),
                )
            )
            self.assertEqual(non_reasoning_client[0], "openai")
            self.assertIsNone(non_reasoning_client[1]["reasoning"])
            self.assertIsNone(non_reasoning_client[1]["verbosity"])

        with self.assertRaises(ValueError):
            factories.LLMClientFactory.create(
                mcp_config.LLMConfig(
                    provider="openai",
                    providers=mcp_config.LLMProvidersConfig(openai=None),
                )
            )
        with self.assertRaises(ValueError):
            factories.LLMClientFactory.create(mcp_config.LLMConfig(provider="unknown"))

        embed_openai = mcp_config.EmbedderConfig(
            provider="openai",
            model="embed-small",
            dimensions=256,
            providers=mcp_config.EmbedderProvidersConfig(
                openai=mcp_config.OpenAIProviderConfig(api_key="secret", api_url="https://embed.example")
            ),
        )
        embed_bedrock = mcp_config.EmbedderConfig(
            provider="bedrock",
            model="",
            dimensions=0,
            providers=mcp_config.EmbedderProvidersConfig(
                bedrock=mcp_config.BedrockEmbedderProviderConfig(region_name="ca-central-1")
            ),
        )

        with patch.object(
            factories,
            "OpenAIEmbedder",
            side_effect=lambda **kwargs: ("embed-openai", kwargs),
        ), patch.object(
            factories,
            "BedrockTitanEmbedder",
            side_effect=lambda **kwargs: ("embed-bedrock", kwargs),
        ):
            openai_embedder = factories.EmbedderFactory.create(embed_openai)
            self.assertEqual(openai_embedder[0], "embed-openai")
            self.assertEqual(openai_embedder[1]["config"].embedding_dim, 256)

            bedrock_embedder = factories.EmbedderFactory.create(embed_bedrock)
            self.assertEqual(bedrock_embedder[0], "embed-bedrock")
            self.assertEqual(bedrock_embedder[1]["config"].embedding_model, "amazon.titan-embed-text-v2:0")
            self.assertEqual(bedrock_embedder[1]["config"].embedding_dim, 1024)

        with self.assertRaises(ValueError):
            factories.EmbedderFactory.create(
                mcp_config.EmbedderConfig(
                    provider="openai",
                    providers=mcp_config.EmbedderProvidersConfig(openai=None),
                )
            )
        with self.assertRaises(ValueError):
            factories.EmbedderFactory.create(mcp_config.EmbedderConfig(provider="unknown"))

    def test_database_driver_factory_uses_env_overrides_and_validates_neptune(self) -> None:
        try:
            factories = importlib.import_module("context_graph.runtime.providers.factories")
            mcp_config = importlib.import_module("context_graph.server.mcp.config")
        except ModuleNotFoundError:
            self.skipTest("pydantic-settings is not installed")

        neo4j_config = mcp_config.DatabaseConfig(
            provider="neo4j",
            providers=mcp_config.DatabaseProvidersConfig(
                neo4j=mcp_config.Neo4jProviderConfig(
                    uri="bolt://db:7687",
                    username="neo",
                    password="pass",
                    database="graph",
                )
            ),
        )
        with patch.dict(
            "os.environ",
            {"NEO4J_URI": "bolt://override:7687", "NEO4J_USER": "override-user"},
            clear=True,
        ):
            driver_config = factories.DatabaseDriverFactory.create_config(neo4j_config)
            backend_config = factories.DatabaseDriverFactory.create_backend_config(neo4j_config)
        self.assertEqual(driver_config["uri"], "bolt://override:7687")
        self.assertEqual(driver_config["user"], "override-user")
        self.assertEqual(driver_config["password"], "pass")
        self.assertEqual(driver_config["database"], "graph")
        self.assertEqual(backend_config.uri, "bolt://override:7687")
        self.assertEqual(backend_config.user, "override-user")
        self.assertEqual(backend_config.password, "pass")
        self.assertEqual(backend_config.database, "graph")

        with self.assertRaises(ValueError):
            factories.DatabaseDriverFactory.create_config(
                mcp_config.DatabaseConfig(provider="neptune")
            )

        neptune_config = mcp_config.DatabaseConfig(
            provider="neptune",
            providers=mcp_config.DatabaseProvidersConfig(
                neptune=mcp_config.NeptuneProviderConfig(
                    host="neptune-db://example",
                    aoss_host="aoss.example",
                )
            ),
        )
        with patch.dict("os.environ", {"AOSS_HOST": "aoss.env"}, clear=True):
            driver_config = factories.DatabaseDriverFactory.create_config(neptune_config)
            backend_config = factories.DatabaseDriverFactory.create_backend_config(neptune_config)
        self.assertEqual(driver_config["host"], "neptune-db://example")
        self.assertEqual(driver_config["aoss_host"], "aoss.env")
        self.assertEqual(backend_config.host, "neptune-db://example")
        self.assertEqual(backend_config.aoss_host, "aoss.env")

        with self.assertRaises(ValueError):
            factories.DatabaseDriverFactory.create_config(
                mcp_config.DatabaseConfig(provider="unsupported")
            )

    def test_mcp_formatting_helpers_strip_embeddings(self) -> None:
        formatting = importlib.import_module("context_graph.server.mcp.formatting")

        node = SimpleNamespace(
            model_dump=lambda **kwargs: {
                "uuid": "n1",
                "attributes": {"name_embedding": [1.0, 2.0], "kind": "Function"},
            }
        )
        edge = SimpleNamespace(
            model_dump=lambda **kwargs: {
                "uuid": "e1",
                "attributes": {"fact_embedding": [1.0, 2.0], "weight": 0.7},
            }
        )

        formatted_node = formatting.format_node_result(node)
        formatted_edge = formatting.format_fact_result(edge)

        self.assertEqual(formatted_node["uuid"], "n1")
        self.assertEqual(formatted_node["attributes"], {"kind": "Function"})
        self.assertEqual(formatted_edge["uuid"], "e1")
        self.assertEqual(formatted_edge["attributes"], {"weight": 0.7})


if __name__ == "__main__":
    unittest.main()
