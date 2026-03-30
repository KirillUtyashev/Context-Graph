from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
import sys
from types import SimpleNamespace

import httpx
import pytest
from pydantic import BaseModel

from context_graph.prompts.models import Message
from context_graph.runtime import embeddings
from context_graph.runtime.embedding_clients import client as embedder_client_module
from context_graph.runtime.embedding_clients import openai as openai_embedder_module
from context_graph.runtime.llm import client as llm_client_module
from context_graph.runtime.llm import openai_client as openai_client_module
from context_graph.runtime.llm.config import LLMConfig, ModelSize
from context_graph.runtime.llm.errors import RateLimitError


class PassthroughEmbedder(embedder_client_module.EmbedderClient):
    async def create(self, input_data):
        return await super().create(input_data)


class FakeCache:
    def __init__(self, _directory: str) -> None:
        self.storage: dict[str, dict[str, object]] = {}

    def get(self, key: str):
        return self.storage.get(key)

    def set(self, key: str, value) -> None:
        self.storage[key] = value


class FakeSpan:
    def __init__(self) -> None:
        self.attributes: list[dict[str, object]] = []
        self.status_calls: list[tuple[str, str | None]] = []
        self.recorded: list[Exception] = []

    def add_attributes(self, attributes: dict[str, object]) -> None:
        self.attributes.append(attributes)

    def set_status(self, status: str, description: str | None = None) -> None:
        self.status_calls.append((status, description))

    def record_exception(self, exception: Exception) -> None:
        self.recorded.append(exception)


class FakeTracer:
    def __init__(self) -> None:
        self.span = FakeSpan()
        self.started_names: list[str] = []

    @contextmanager
    def start_span(self, name: str):
        self.started_names.append(name)
        yield self.span


class RecordingLLMClient(llm_client_module.LLMClient):
    def __init__(self, config: LLMConfig | None = None, cache: bool = False) -> None:
        self.calls: list[tuple[list[Message], type[BaseModel] | None, int, ModelSize]] = []
        self.next_response: dict[str, object] = {"ok": True}
        super().__init__(config, cache)

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = llm_client_module.DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, object]:
        self.calls.append((messages, response_model, max_tokens, model_size))
        return dict(self.next_response)


class OpenAIishLLM(RecordingLLMClient):
    pass


class AnthropicLLM(RecordingLLMClient):
    pass


class GeminiLLM(RecordingLLMClient):
    pass


class GroqLLM(RecordingLLMClient):
    pass


class SuperCallingLLMClient(llm_client_module.LLMClient):
    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = llm_client_module.DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, object]:
        return await super()._generate_response(messages, response_model, max_tokens, model_size)


class RateLimitingLLMClient(llm_client_module.LLMClient):
    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = llm_client_module.DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, object]:
        raise RateLimitError()


class ResponseSchema(BaseModel):
    ok: bool


def test_embedder_client_base_and_openai_embedder_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    embedder = PassthroughEmbedder()
    assert asyncio.run(embedder.create("hello")) is None
    with pytest.raises(NotImplementedError):
        asyncio.run(embedder.create_batch(["hello"]))

    created_clients: list[dict[str, object]] = []

    def fake_async_openai(**kwargs):
        created_clients.append(kwargs)
        return SimpleNamespace(embeddings=SimpleNamespace(create=None))

    monkeypatch.setattr(openai_embedder_module, "AsyncOpenAI", fake_async_openai)
    default_embedder = openai_embedder_module.OpenAIEmbedder()
    assert default_embedder.config.embedding_model == openai_embedder_module.DEFAULT_EMBEDDING_MODEL
    assert created_clients == [{"api_key": None, "base_url": None}]

    class FakeEmbeddingsAPI:
        def __init__(self) -> None:
            self.calls: list[tuple[object, str]] = []

        async def create(self, *, input, model):
            self.calls.append((input, model))
            payload = [SimpleNamespace(embedding=[1.0, 2.0, 3.0])]
            if isinstance(input, list):
                payload = [
                    SimpleNamespace(embedding=[1.0, 2.0, 3.0]),
                    SimpleNamespace(embedding=[4.0, 5.0, 6.0]),
                ]
            return SimpleNamespace(data=payload)

    api = FakeEmbeddingsAPI()
    configured_embedder = openai_embedder_module.OpenAIEmbedder(
        config=openai_embedder_module.OpenAIEmbedderConfig(
            embedding_model="demo-model",
            embedding_dim=2,
        ),
        client=SimpleNamespace(embeddings=api),
    )
    assert asyncio.run(configured_embedder.create("hello")) == [1.0, 2.0]
    assert asyncio.run(configured_embedder.create_batch(["one", "two"])) == [
        [1.0, 2.0],
        [4.0, 5.0],
    ]
    assert api.calls == [
        ("hello", "demo-model"),
        (["one", "two"], "demo-model"),
    ]


def test_embeddings_make_provider_and_run_async_error_paths() -> None:
    class FakeBedrockTitanEmbedderConfig:
        def __init__(self, *, embedding_model: str, embedding_dim: int, region_name: str | None):
            self.embedding_model = embedding_model
            self.embedding_dim = embedding_dim
            self.region_name = region_name

    class FakeBedrockTitanEmbedder:
        def __init__(self, *, config) -> None:
            self.config = config

    fake_bedrock_module = SimpleNamespace(
        BedrockTitanEmbedder=FakeBedrockTitanEmbedder,
        BedrockTitanEmbedderConfig=FakeBedrockTitanEmbedderConfig,
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setitem(
            sys.modules,
            "context_graph.runtime.embedding_clients.bedrock",
            fake_bedrock_module,
        )
        provider = embeddings.make_embedding_provider(
            embeddings.EmbeddingSettings(
                provider="bedrock",
                model="amazon.titan-embed-text-v2:0",
                dimensions=256,
                aws_region="ca-central-1",
            )
        )

    assert isinstance(provider, embeddings.EmbeddingProvider)
    assert provider.client.config.embedding_model == "amazon.titan-embed-text-v2:0"
    assert provider.client.config.embedding_dim == 256
    assert provider.client.config.region_name == "ca-central-1"

    with pytest.raises(RuntimeError, match="unsupported embedding_clients provider 'custom'"):
        embeddings.make_embedding_provider(embeddings.EmbeddingSettings(provider="custom"))

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("OPENAI_API_KEY", "secret")
        assert embeddings._infer_embedding_provider() == "openai"

    async def bad_coro() -> None:
        raise ValueError("boom")

    async def run_inside_loop() -> None:
        with pytest.raises(ValueError, match="boom"):
            embeddings._run_async(bad_coro())

    asyncio.run(run_inside_loop())


def test_llm_client_helpers_cover_cache_tracing_and_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_client_module, "LLMCache", FakeCache)

    assert "same language" in llm_client_module.get_extraction_language_instruction("demo")
    assert llm_client_module.is_server_or_retry_error(RateLimitError()) is True
    assert llm_client_module.is_server_or_retry_error(json.JSONDecodeError("bad", "{}", 0)) is True

    request = httpx.Request("GET", "https://example.test")
    retry_error = httpx.HTTPStatusError(
        "boom",
        request=request,
        response=httpx.Response(503, request=request),
    )
    non_retry_error = httpx.HTTPStatusError(
        "boom",
        request=request,
        response=httpx.Response(400, request=request),
    )
    assert llm_client_module.is_server_or_retry_error(retry_error) is True
    assert llm_client_module.is_server_or_retry_error(non_retry_error) is False

    default_client = RecordingLLMClient(None, cache=False)
    assert isinstance(default_client.config, LLMConfig)

    client = OpenAIishLLM(LLMConfig(model="gpt-test", max_tokens=123), cache=True)
    tracer = FakeTracer()
    client.set_tracer(tracer)
    assert client.tracer is tracer
    assert client.cache_dir is not None
    assert client._clean_input("A\u200bB\x00C\n") == "ABC\n"

    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="hello"),
    ]
    result = asyncio.run(
        client.generate_response(
            messages,
            response_model=ResponseSchema,
            max_tokens=None,
            model_size=ModelSize.small,
            group_id="demo",
            prompt_name="prompt.test",
        )
    )
    assert result == {"ok": True}
    assert client.calls[0][2] == 123
    assert client.calls[0][3] is ModelSize.small
    assert "high_level_keywords" not in messages[-1].content
    assert "Respond with a JSON object" in messages[-1].content
    assert "same language" in messages[0].content
    cache_key = client._get_cache_key(messages)
    assert client.cache_dir.storage[cache_key] == {"ok": True}
    assert tracer.started_names == ["llm.generate"]
    assert {"cache.hit": False} in tracer.span.attributes
    assert any(attrs.get("prompt.name") == "prompt.test" for attrs in tracer.span.attributes)

    cached_client = RecordingLLMClient(LLMConfig(model="gpt-test"), cache=True)
    cached_tracer = FakeTracer()
    cached_client.set_tracer(cached_tracer)
    cached_client.cache_dir.get = lambda key: {"cached": True}
    fresh_messages = [
        Message(role="system", content="system"),
        Message(role="user", content="cached"),
    ]
    assert asyncio.run(cached_client.generate_response(fresh_messages)) == {"cached": True}
    assert cached_client.calls == []
    assert {"cache.hit": True} in cached_tracer.span.attributes

    error_client = RecordingLLMClient(LLMConfig(model="gpt-test"), cache=False)
    error_tracer = FakeTracer()
    error_client.set_tracer(error_tracer)

    async def fail(*_args, **_kwargs):
        raise ValueError("bad output")

    monkeypatch.setattr(error_client, "_generate_response_with_retry", fail)
    with pytest.raises(ValueError, match="bad output"):
        asyncio.run(error_client.generate_response([Message(role="system", content="system")]))
    assert error_tracer.span.status_calls == [("error", "bad output")]
    assert len(error_tracer.span.recorded) == 1

    assert client._get_provider_type() == "openai"
    assert AnthropicLLM()._get_provider_type() == "anthropic"
    assert GeminiLLM()._get_provider_type() == "gemini"
    assert GroqLLM()._get_provider_type() == "groq"
    assert RecordingLLMClient()._get_provider_type() == "unknown"

    long_output = "x" * 600
    failure_log = client._get_failed_generation_log(messages, long_output)
    assert "Input messages: 2 message(s)" in failure_log
    assert "Raw output (truncated):" in failure_log
    assert client._get_failed_generation_log(messages, None).endswith("No raw output available")


def test_llm_client_retry_wrapper_reraises_and_super_abstract_method() -> None:
    client = RateLimitingLLMClient(None)
    with pytest.raises(RateLimitError):
        asyncio.run(
            llm_client_module.LLMClient._generate_response_with_retry.__wrapped__(  # type: ignore[attr-defined]
                client,
                [Message(role="system", content="sys")],
            )
        )
    abstract_client = SuperCallingLLMClient(None)
    assert asyncio.run(
        abstract_client._generate_response([Message(role="system", content="sys")])
    ) is None


def test_openai_client_builds_structured_and_json_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    created_clients: list[dict[str, object]] = []

    class FakeResponsesAPI:
        def __init__(self) -> None:
            self.parse_calls: list[dict[str, object]] = []

        async def parse(self, **kwargs):
            self.parse_calls.append(kwargs)
            return {"structured": True}

    class FakeChatCompletionsAPI:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return {"json": True}

    def fake_async_openai(**kwargs):
        created_clients.append(kwargs)
        return SimpleNamespace(
            responses=FakeResponsesAPI(),
            chat=SimpleNamespace(completions=FakeChatCompletionsAPI()),
        )

    monkeypatch.setattr(openai_client_module, "AsyncOpenAI", fake_async_openai)
    default_client = openai_client_module.OpenAIClient()
    assert created_clients == [{"api_key": None, "base_url": None}]

    fake_client = SimpleNamespace(
        responses=FakeResponsesAPI(),
        chat=SimpleNamespace(completions=FakeChatCompletionsAPI()),
    )
    client = openai_client_module.OpenAIClient(
        config=LLMConfig(api_key="secret", base_url="https://example.test"),
        client=fake_client,
    )

    structured_response = asyncio.run(
        client._create_structured_completion(
            model="gpt-5-mini",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.7,
            max_tokens=50,
            response_model=ResponseSchema,
            reasoning="minimal",
            verbosity="low",
        )
    )
    assert structured_response == {"structured": True}
    structured_kwargs = fake_client.responses.parse_calls[-1]
    assert structured_kwargs["model"] == "gpt-5-mini"
    assert "temperature" not in structured_kwargs
    assert structured_kwargs["reasoning"] == {"effort": "minimal"}
    assert structured_kwargs["text"] == {"verbosity": "low"}

    asyncio.run(
        client._create_structured_completion(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.4,
            max_tokens=25,
            response_model=ResponseSchema,
            reasoning="minimal",
            verbosity="low",
        )
    )
    non_reasoning_kwargs = fake_client.responses.parse_calls[-1]
    assert non_reasoning_kwargs["temperature"] == 0.4
    assert "reasoning" not in non_reasoning_kwargs
    assert "text" not in non_reasoning_kwargs

    json_response = asyncio.run(
        client._create_completion(
            model="gpt-5-mini",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.5,
            max_tokens=30,
        )
    )
    assert json_response == {"json": True}
    json_kwargs = fake_client.chat.completions.calls[-1]
    assert json_kwargs["temperature"] is None
    assert json_kwargs["response_format"] == {"type": "json_object"}

    asyncio.run(
        client._create_completion(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.25,
            max_tokens=30,
        )
    )
    non_reasoning_json_kwargs = fake_client.chat.completions.calls[-1]
    assert non_reasoning_json_kwargs["temperature"] == 0.25
