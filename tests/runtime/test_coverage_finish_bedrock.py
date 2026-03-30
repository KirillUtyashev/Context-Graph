from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel, ValidationError

from context_graph.prompts.models import Message


class _RecordingSpan:
    def __init__(self) -> None:
        self.attributes: list[dict[str, object]] = []
        self.statuses: list[tuple[str, str | None]] = []
        self.recorded: list[Exception] = []

    def add_attributes(self, attributes: dict[str, object]) -> None:
        self.attributes.append(attributes)

    def set_status(self, status: str, description: str | None = None) -> None:
        self.statuses.append((status, description))

    def record_exception(self, exception: Exception) -> None:
        self.recorded.append(exception)


class _RecordingTracer:
    def __init__(self) -> None:
        self.span = _RecordingSpan()

    @contextmanager
    def start_span(self, _name: str):
        yield self.span


def _messages() -> list[Message]:
    return [
        Message(role="system", content="system prompt"),
        Message(role="user", content="hello"),
    ]


def test_bedrock_constructor_defaults_and_max_token_fallback() -> None:
    fake_client = MagicMock()
    fake_boto3 = SimpleNamespace(client=MagicMock(return_value=fake_client))

    with patch.dict(sys.modules, {"boto3": fake_boto3}):
        from context_graph.runtime.llm.bedrock_client import (
            BedrockLLMClient,
            DEFAULT_BEDROCK_MAX_TOKENS,
            DEFAULT_MODEL,
            DEFAULT_SMALL_MODEL,
        )

        client = BedrockLLMClient()
        assert client.config.model == DEFAULT_MODEL
        assert client.config.small_model == DEFAULT_SMALL_MODEL
        client.max_tokens = None
        assert client._resolve_max_tokens(None, "unknown-model") == DEFAULT_BEDROCK_MAX_TOKENS


def test_bedrock_generate_response_parses_non_dict_tool_input() -> None:
    fake_boto3 = SimpleNamespace(client=MagicMock(return_value=MagicMock()))

    with patch.dict(sys.modules, {"boto3": fake_boto3}):
        from context_graph.runtime.llm.bedrock_client import BedrockLLMClient
        from context_graph.runtime.llm.config import LLMConfig

        client = BedrockLLMClient(config=LLMConfig(model="anthropic.claude-3-5-haiku-20241022-v1:0"))
        client._bedrock.converse.return_value = {
            "usage": {"inputTokens": 1, "outputTokens": 2},
            "output": {
                "message": {
                    "content": [
                        {"toolUse": {"name": "generic_json_output", "input": '{"ok": true}'}}
                    ]
                }
            },
        }

        result, input_tokens, output_tokens = asyncio.run(client._generate_response(_messages()))
        assert result == {"ok": True}
        assert (input_tokens, output_tokens) == (1, 2)


def test_bedrock_generate_response_records_prompt_name_attribute() -> None:
    fake_boto3 = SimpleNamespace(client=MagicMock(return_value=MagicMock()))

    with patch.dict(sys.modules, {"boto3": fake_boto3}):
        from context_graph.runtime.llm.bedrock_client import BedrockLLMClient
        from context_graph.runtime.llm.config import LLMConfig

        client = BedrockLLMClient(config=LLMConfig(model="anthropic.claude-3-5-haiku-20241022-v1:0"))
        client.tracer = _RecordingTracer()
        client._generate_response = AsyncMock(return_value=({"ok": True}, 3, 4))

        result = asyncio.run(client.generate_response(_messages(), prompt_name="claim-resolution"))
        assert result == {"ok": True}
        assert client.tracer.span.attributes[-1]["prompt.name"] == "claim-resolution"


def test_bedrock_validation_retries_append_model_name_and_log_final_error() -> None:
    fake_boto3 = SimpleNamespace(client=MagicMock(return_value=MagicMock()))

    with patch.dict(sys.modules, {"boto3": fake_boto3}):
        from context_graph.runtime.llm.bedrock_client import BedrockLLMClient
        from context_graph.runtime.llm.config import LLMConfig
        import context_graph.runtime.llm.bedrock_client as bedrock_module

        class ResponseModel(BaseModel):
            ok: bool

        client = BedrockLLMClient(config=LLMConfig(model="anthropic.claude-3-5-haiku-20241022-v1:0"))
        client.tracer = _RecordingTracer()
        client._generate_response = AsyncMock(side_effect=[({}, 0, 0), ({}, 0, 0), ({}, 0, 0)])
        messages = _messages()

        with patch.object(bedrock_module.logger, "error") as log_error:
            with pytest.raises(ValidationError):
                asyncio.run(client.generate_response(messages, response_model=ResponseModel))

        assert any("ResponseModel" in message.content for message in messages[2:])
        assert log_error.call_count >= 1
