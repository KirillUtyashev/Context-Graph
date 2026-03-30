"""Tests for bedrock_client, openai_base_client, and bedrock embedder to increase coverage."""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from context_graph.prompts.models import Message
from context_graph.runtime.llm.config import LLMConfig, ModelSize
from context_graph.runtime.llm.errors import RateLimitError, RefusalError


def _patch_boto3():
    """Create a mock boto3 module that can be injected."""
    fake_boto3 = MagicMock()
    fake_client = MagicMock()
    fake_client.exceptions = SimpleNamespace(
        ThrottlingException=type("ThrottlingException", (Exception,), {}),
    )
    fake_boto3.client.return_value = fake_client
    return fake_boto3, fake_client


class _FakeSpan:
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


class _FakeTracer:
    def __init__(self) -> None:
        self.span = _FakeSpan()

    @contextmanager
    def start_span(self, _name: str):
        yield self.span


# ── Bedrock LLM Client ──────────────────────────────────────────────────────

class TestBedrockLLMClient:
    def _make_client(self, converse_response=None, converse_side_effect=None):
        fake_boto3, fake_client = _patch_boto3()
        if converse_side_effect:
            fake_client.converse.side_effect = converse_side_effect
        elif converse_response:
            fake_client.converse.return_value = converse_response

        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            from context_graph.runtime.llm.bedrock_client import BedrockLLMClient
            client = BedrockLLMClient.__new__(BedrockLLMClient)
            # Manually init to avoid actual boto3 import
            config = LLMConfig(model="anthropic.claude-3-5-haiku-20241022-v1:0")
            super(BedrockLLMClient, client).__init__(config, False)
            client._bedrock = fake_client
        return client, fake_client

    def test_generate_response_tool_use(self):
        response = {
            "usage": {"inputTokens": 10, "outputTokens": 20},
            "output": {
                "message": {
                    "content": [
                        {"toolUse": {"name": "ResponseModel", "input": {"ok": True}}}
                    ]
                }
            },
        }

        class ResponseModel(BaseModel):
            ok: bool

        client, _ = self._make_client(converse_response=response)
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        result = asyncio.run(client.generate_response(messages, response_model=ResponseModel))
        assert result == {"ok": True}

    def test_generate_response_text_fallback(self):
        response = {
            "usage": {"inputTokens": 5, "outputTokens": 10},
            "output": {
                "message": {
                    "content": [{"text": '{"answer": 42}'}]
                }
            },
        }
        client, _ = self._make_client(converse_response=response)
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        result = asyncio.run(client.generate_response(messages))
        assert result == {"answer": 42}

    def test_generate_response_no_content_raises(self):
        response = {
            "usage": {},
            "output": {"message": {"content": []}},
        }
        client, _ = self._make_client(converse_response=response)
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        with pytest.raises(ValueError, match="Could not extract"):
            asyncio.run(client.generate_response(messages))

    def test_throttling_raises_rate_limit(self):
        _, fake_client = _patch_boto3()
        ThrottlingException = fake_client.exceptions.ThrottlingException
        fake_client.converse.side_effect = ThrottlingException("throttled")

        with patch.dict(sys.modules, {"boto3": MagicMock()}):
            from context_graph.runtime.llm.bedrock_client import BedrockLLMClient
            client = BedrockLLMClient.__new__(BedrockLLMClient)
            config = LLMConfig(model="test")
            super(BedrockLLMClient, client).__init__(config, False)
            client._bedrock = fake_client

        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        with pytest.raises(RateLimitError):
            asyncio.run(client.generate_response(messages))

    def test_access_denied_raises_refusal(self):
        _, fake_client = _patch_boto3()

        class AccessDeniedException(Exception):
            pass

        fake_client.converse.side_effect = AccessDeniedException("denied")

        with patch.dict(sys.modules, {"boto3": MagicMock()}):
            from context_graph.runtime.llm.bedrock_client import BedrockLLMClient
            client = BedrockLLMClient.__new__(BedrockLLMClient)
            config = LLMConfig(model="test")
            super(BedrockLLMClient, client).__init__(config, False)
            client._bedrock = fake_client

        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        with pytest.raises(RefusalError):
            asyncio.run(client.generate_response(messages))

    def test_retry_on_generic_error(self):
        call_count = 0

        def failing_converse(**kwargs):
            nonlocal call_count
            call_count += 1
            raise ValueError("bad response")

        _, fake_client = _patch_boto3()
        fake_client.converse.side_effect = failing_converse

        with patch.dict(sys.modules, {"boto3": MagicMock()}):
            from context_graph.runtime.llm.bedrock_client import BedrockLLMClient
            client = BedrockLLMClient.__new__(BedrockLLMClient)
            config = LLMConfig(model="test")
            super(BedrockLLMClient, client).__init__(config, False)
            client._bedrock = fake_client

        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        with pytest.raises(ValueError, match="bad response"):
            asyncio.run(client.generate_response(messages))
        assert call_count == 3  # initial + 2 retries

    def test_build_tool_config_with_model(self):
        client, _ = self._make_client()

        class MyModel(BaseModel):
            name: str

        config, tool_name = client._build_tool_config(MyModel)
        assert tool_name == "MyModel"
        assert config["tools"][0]["toolSpec"]["name"] == "MyModel"

    def test_build_tool_config_without_model(self):
        client, _ = self._make_client()
        config, tool_name = client._build_tool_config(None)
        assert tool_name == "generic_json_output"

    def test_extract_json_from_text(self):
        client, _ = self._make_client()
        result = client._extract_json_from_text('Some text {"key": "value"} more text')
        assert result == {"key": "value"}

    def test_extract_json_from_text_invalid(self):
        client, _ = self._make_client()
        with pytest.raises(ValueError, match="Could not extract JSON"):
            client._extract_json_from_text("no json here")

    def test_resolve_max_tokens(self):
        client, _ = self._make_client()
        assert client._resolve_max_tokens(100, "test") == 100
        assert client._resolve_max_tokens(None, "test") == client.max_tokens

    def test_get_max_tokens_for_model(self):
        client, _ = self._make_client()
        from context_graph.runtime.llm.bedrock_client import DEFAULT_BEDROCK_MAX_TOKENS
        assert client._get_max_tokens_for_model("anthropic.claude-3-5-haiku-20241022-v1:0") == 8192
        assert client._get_max_tokens_for_model("unknown-model") == DEFAULT_BEDROCK_MAX_TOKENS

    def test_small_model_size(self):
        response = {
            "usage": {"inputTokens": 5, "outputTokens": 10},
            "output": {"message": {"content": [{"text": '{"ok": true}'}]}},
        }
        client, _ = self._make_client(converse_response=response)
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        result = asyncio.run(client.generate_response(messages, model_size=ModelSize.small))
        assert result == {"ok": True}


# ── OpenAI Base Client ───────────────────────────────────────────────────────

def _make_base_client_class():
    from context_graph.runtime.llm.openai_base_client import BaseOpenAIClient

    class TestClient(BaseOpenAIClient):
        async def _create_completion(self, *args, **kwargs):
            pass

        async def _create_structured_completion(self, *args, **kwargs):
            pass

    return TestClient


class TestBaseOpenAIClient:
    def test_abstract_methods_call_super_pass(self):
        from context_graph.runtime.llm.openai_base_client import BaseOpenAIClient

        class TestClient(BaseOpenAIClient):
            async def _create_completion(self, *args, **kwargs):
                return await super()._create_completion(*args, **kwargs)

            async def _create_structured_completion(self, *args, **kwargs):
                return await super()._create_structured_completion(*args, **kwargs)

        client = TestClient()
        assert (
            asyncio.run(client._create_completion("model", [], None, 10, None)) is None
        )
        assert (
            asyncio.run(
                client._create_structured_completion(
                    "model",
                    [],
                    None,
                    10,
                    BaseModel,
                    None,
                    None,
                )
            )
            is None
        )

    def test_cache_not_implemented(self):
        TestClient = _make_base_client_class()
        with pytest.raises(NotImplementedError, match="Caching is not implemented"):
            TestClient(cache=True)

    def test_convert_messages(self):
        TestClient = _make_base_client_class()
        client = TestClient()
        messages = [
            Message(role="system", content="sys"),
            Message(role="user", content="hello"),
        ]
        converted = client._convert_messages_to_openai_format(messages)
        assert converted[0]["role"] == "system"
        assert converted[1]["role"] == "user"

    def test_get_model_for_size(self):
        TestClient = _make_base_client_class()
        client = TestClient(config=LLMConfig(model="gpt-4", small_model="gpt-mini"))
        assert client._get_model_for_size(ModelSize.small) == "gpt-mini"
        assert client._get_model_for_size(ModelSize.medium) == "gpt-4"

    def test_handle_structured_response(self):
        TestClient = _make_base_client_class()
        client = TestClient()
        response = SimpleNamespace(
            output_text='{"ok": true}',
            usage=SimpleNamespace(input_tokens=10, output_tokens=20),
        )
        result, inp, out = client._handle_structured_response(response)
        assert result == {"ok": True}
        assert inp == 10
        assert out == 20

    def test_handle_structured_response_no_usage(self):
        TestClient = _make_base_client_class()
        client = TestClient()
        response = SimpleNamespace(output_text='{"ok": true}', usage=None)
        result, inp, out = client._handle_structured_response(response)
        assert result == {"ok": True}
        assert inp == 0

    def test_handle_structured_response_refusal(self):
        TestClient = _make_base_client_class()
        client = TestClient()
        response = SimpleNamespace(output_text=None, refusal="I refuse", usage=None)
        with pytest.raises(RefusalError):
            client._handle_structured_response(response)

    def test_handle_structured_response_invalid(self):
        TestClient = _make_base_client_class()
        client = TestClient()
        response = SimpleNamespace(output_text=None, usage=None)
        with pytest.raises(Exception, match="Invalid response"):
            client._handle_structured_response(response)

    def test_handle_json_response(self):
        TestClient = _make_base_client_class()
        client = TestClient()
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=10),
        )
        result, inp, out = client._handle_json_response(response)
        assert result == {"ok": True}
        assert inp == 5
        assert out == 10

    def test_handle_json_response_no_usage(self):
        TestClient = _make_base_client_class()
        client = TestClient()
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))],
            usage=None,
        )
        result, inp, out = client._handle_json_response(response)
        assert inp == 0

    def test_generate_response_structured(self):
        from context_graph.runtime.llm.openai_base_client import BaseOpenAIClient

        class ResponseModel(BaseModel):
            ok: bool

        class TestClient(BaseOpenAIClient):
            async def _create_completion(self, *args, **kwargs):
                pass

            async def _create_structured_completion(self, *args, **kwargs):
                return SimpleNamespace(
                    output_text='{"ok": true}',
                    usage=SimpleNamespace(input_tokens=10, output_tokens=20),
                )

        client = TestClient()
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        result = asyncio.run(client.generate_response(messages, response_model=ResponseModel))
        assert result == {"ok": True}

    def test_generate_response_json(self):
        from context_graph.runtime.llm.openai_base_client import BaseOpenAIClient

        class TestClient(BaseOpenAIClient):
            async def _create_completion(self, *args, **kwargs):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"answer": 42}'))],
                    usage=SimpleNamespace(prompt_tokens=5, completion_tokens=10),
                )

            async def _create_structured_completion(self, *args, **kwargs):
                pass

        client = TestClient()
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        result = asyncio.run(client.generate_response(messages))
        assert result == {"answer": 42}

    def test_generate_response_rate_limit(self):
        import openai
        from context_graph.runtime.llm.openai_base_client import BaseOpenAIClient

        class TestClient(BaseOpenAIClient):
            async def _create_completion(self, *args, **kwargs):
                raise openai.RateLimitError(
                    message="rate limited",
                    response=MagicMock(status_code=429),
                    body=None,
                )

            async def _create_structured_completion(self, *args, **kwargs):
                pass

        client = TestClient()
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        with pytest.raises(RateLimitError):
            asyncio.run(client.generate_response(messages))

    def test_generate_response_retry_on_error(self):
        from context_graph.runtime.llm.openai_base_client import BaseOpenAIClient

        call_count = 0

        class TestClient(BaseOpenAIClient):
            async def _create_completion(self, *args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    raise ValueError("bad")
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
                    usage=SimpleNamespace(prompt_tokens=5, completion_tokens=10),
                )

            async def _create_structured_completion(self, *args, **kwargs):
                pass

        client = TestClient()
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        result = asyncio.run(client.generate_response(messages))
        assert result == {"ok": True}
        assert call_count == 3

    def test_generate_response_max_retries_exceeded(self):
        from context_graph.runtime.llm.openai_base_client import BaseOpenAIClient

        class TestClient(BaseOpenAIClient):
            async def _create_completion(self, *args, **kwargs):
                raise ValueError("always fails")

            async def _create_structured_completion(self, *args, **kwargs):
                pass

        client = TestClient()
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        with pytest.raises(ValueError, match="always fails"):
            asyncio.run(client.generate_response(messages))

    def test_generate_response_connection_error(self):
        from context_graph.runtime.llm.openai_base_client import BaseOpenAIClient

        class TestClient(BaseOpenAIClient):
            async def _create_completion(self, *args, **kwargs):
                raise RuntimeError("Connection error: refused")

            async def _create_structured_completion(self, *args, **kwargs):
                pass

        client = TestClient()
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        with pytest.raises(RuntimeError, match="Connection error"):
            asyncio.run(client.generate_response(messages))

    def test_generate_response_authentication_error_logs(self):
        import openai
        from context_graph.runtime.llm.openai_base_client import BaseOpenAIClient

        class TestClient(BaseOpenAIClient):
            async def _create_completion(self, *args, **kwargs):
                raise openai.AuthenticationError(
                    message="bad key",
                    response=MagicMock(status_code=401),
                    body=None,
                )

            async def _create_structured_completion(self, *args, **kwargs):
                pass

        client = TestClient()
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        with patch("context_graph.runtime.llm.openai_base_client.logger.error") as mock_error:
            with pytest.raises(openai.AuthenticationError):
                asyncio.run(client._generate_response(messages))
        mock_error.assert_called()

    def test_generate_response_timeout_branch_sets_error_status(self):
        from context_graph.runtime.llm import openai_base_client as module

        class FakeTimeoutError(Exception):
            pass

        class TestClient(module.BaseOpenAIClient):
            async def _create_completion(self, *args, **kwargs):
                raise AssertionError("should not be called")

            async def _create_structured_completion(self, *args, **kwargs):
                raise AssertionError("should not be called")

            async def _generate_response(self, *args, **kwargs):
                raise FakeTimeoutError("timed out")

        client = TestClient()
        client.tracer = _FakeTracer()
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        with patch.object(module.openai, "APITimeoutError", FakeTimeoutError):
            with pytest.raises(FakeTimeoutError, match="timed out"):
                asyncio.run(client.generate_response(messages))
        assert client.tracer.span.status_calls == [("error", "None")]

    def test_generate_response_prompt_name_and_terminal_raise_path(self):
        from context_graph.runtime.llm.openai_base_client import BaseOpenAIClient

        class TestClient(BaseOpenAIClient):
            async def _create_completion(self, *args, **kwargs):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
                )

            async def _create_structured_completion(self, *args, **kwargs):
                pass

        client = TestClient()
        client.tracer = _FakeTracer()
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        assert asyncio.run(client.generate_response(messages, prompt_name="prompt.name")) == {"ok": True}
        assert any(attrs.get("prompt.name") == "prompt.name" for attrs in client.tracer.span.attributes)

        failing_client = TestClient()
        failing_client.tracer = _FakeTracer()
        failing_client.MAX_RETRIES = -1
        with pytest.raises(Exception, match="Max retries exceeded with no specific error"):
            asyncio.run(
                failing_client.generate_response(
                    [Message(role="system", content="sys"), Message(role="user", content="hi")]
                )
            )

    def test_generate_response_length_error(self):
        import openai
        from context_graph.runtime.llm.openai_base_client import BaseOpenAIClient

        fake_completion = MagicMock()

        class TestClient(BaseOpenAIClient):
            async def _create_completion(self, *args, **kwargs):
                raise openai.LengthFinishReasonError(completion=fake_completion)

            async def _create_structured_completion(self, *args, **kwargs):
                pass

        client = TestClient()
        messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]
        with pytest.raises(Exception, match="Output length exceeded"):
            asyncio.run(client.generate_response(messages))


# ── Bedrock Embedder ─────────────────────────────────────────────────────────

class TestBedrockEmbedder:
    def test_cohere_embedder_init_with_default_config(self):
        fake_boto3, fake_client = _patch_boto3()
        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            from context_graph.runtime.embedding_clients.bedrock import BedrockEmbedder

            embedder = BedrockEmbedder()

        assert embedder._client is fake_client

    def test_cohere_embedder(self):
        fake_boto3, fake_client = _patch_boto3()
        fake_client.invoke_model.return_value = {
            "body": SimpleNamespace(read=lambda: json.dumps({"embeddings": [[1.0, 2.0, 3.0]]}).encode())
        }
        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            from context_graph.runtime.embedding_clients.bedrock import BedrockEmbedder, BedrockEmbedderConfig
            embedder = BedrockEmbedder.__new__(BedrockEmbedder)
            embedder.config = BedrockEmbedderConfig(embedding_dim=2)
            embedder._client = fake_client
        result = asyncio.run(embedder.create("hello"))
        assert result == [1.0, 2.0]

    def test_cohere_embedder_batch(self):
        fake_boto3, fake_client = _patch_boto3()
        fake_client.invoke_model.return_value = {
            "body": SimpleNamespace(read=lambda: json.dumps({"embeddings": [[1.0, 2.0], [3.0, 4.0]]}).encode())
        }
        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            from context_graph.runtime.embedding_clients.bedrock import BedrockEmbedder, BedrockEmbedderConfig
            embedder = BedrockEmbedder.__new__(BedrockEmbedder)
            embedder.config = BedrockEmbedderConfig(embedding_dim=2)
            embedder._client = fake_client
        result = asyncio.run(embedder.create_batch(["a", "b"]))
        assert len(result) == 2

    def test_titan_embedder(self):
        fake_boto3, fake_client = _patch_boto3()
        fake_client.invoke_model.return_value = {
            "body": SimpleNamespace(read=lambda: json.dumps({"embedding_clients": [1.0, 2.0, 3.0]}).encode())
        }
        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            from context_graph.runtime.embedding_clients.bedrock import BedrockTitanEmbedder, BedrockTitanEmbedderConfig
            embedder = BedrockTitanEmbedder.__new__(BedrockTitanEmbedder)
            embedder.config = BedrockTitanEmbedderConfig(embedding_dim=3)
            embedder._client = fake_client
        result = asyncio.run(embedder.create("hello"))
        assert result == [1.0, 2.0, 3.0]

    def test_titan_embedder_init_with_default_config(self):
        fake_boto3, fake_client = _patch_boto3()
        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            from context_graph.runtime.embedding_clients.bedrock import BedrockTitanEmbedder

            embedder = BedrockTitanEmbedder()

        assert embedder._client is fake_client

    def test_titan_embedder_batch(self):
        fake_boto3, fake_client = _patch_boto3()
        fake_client.invoke_model.return_value = {
            "body": SimpleNamespace(read=lambda: json.dumps({"embedding_clients": [1.0, 2.0]}).encode())
        }
        with patch.dict(sys.modules, {"boto3": fake_boto3}):
            from context_graph.runtime.embedding_clients.bedrock import BedrockTitanEmbedder, BedrockTitanEmbedderConfig
            embedder = BedrockTitanEmbedder.__new__(BedrockTitanEmbedder)
            embedder.config = BedrockTitanEmbedderConfig(embedding_dim=2)
            embedder._client = fake_client
        result = asyncio.run(embedder.create_batch(["a", "b"]))
        assert len(result) == 2
