"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import asyncio
import json
import logging
import typing
from json import JSONDecodeError
from typing import Any

import boto3
from pydantic import BaseModel, ValidationError

from context_graph.memory.prompts.models import Message
from .client import LLMClient
from .config import DEFAULT_MAX_TOKENS, LLMConfig, ModelSize
from .errors import RateLimitError, RefusalError

logger = logging.getLogger(__name__)

# Default Bedrock Claude model IDs (on-demand inference)
DEFAULT_MODEL = 'anthropic.claude-3-5-haiku-20241022-v1:0'
DEFAULT_SMALL_MODEL = 'anthropic.claude-3-haiku-20240307-v1:0'

# Maximum output tokens per Bedrock Claude model.
# Based on Anthropic docs — Bedrock enforces the same limits as the direct API.
BEDROCK_MODEL_MAX_TOKENS: dict[str, int] = {
    # Claude 3.5 / 3.7 / 4 family on Bedrock
    'anthropic.claude-sonnet-4-6': 8192,
    'anthropic.claude-opus-4-6-v1': 8192,
    'anthropic.claude-3-7-sonnet-20250219-v1:0': 65536,
    'anthropic.claude-3-5-sonnet-20241022-v2:0': 8192,
    'anthropic.claude-3-5-sonnet-20240620-v1:0': 8192,
    'anthropic.claude-3-5-haiku-20241022-v1:0': 8192,
    # Claude 3 family on Bedrock
    'anthropic.claude-3-opus-20240229-v1:0': 4096,
    'anthropic.claude-3-sonnet-20240229-v1:0': 4096,
    'anthropic.claude-3-haiku-20240307-v1:0': 4096,
}

DEFAULT_BEDROCK_MAX_TOKENS = 8192


class BedrockLLMClient(LLMClient):
    """LLM client for AWS Bedrock using the Converse API.

    Uses IAM role-based authentication (no API key required). The EC2 instance
    running this client must have an IAM role with bedrock:Converse permission.

    Structured output is implemented via Bedrock's toolConfig / toolUse, mirroring
    the approach used in AnthropicClient.

    Args:
        config: LLMConfig. The ``api_key`` field is ignored — Bedrock uses IAM auth.
            Set ``model`` to a Bedrock model ID such as
            ``'anthropic.claude-3-5-haiku-20241022-v1:0'``.
        cache: Whether to cache LLM responses.
        region_name: AWS region for Bedrock. Defaults to the boto3 session default.
        max_tokens: Override the default max output tokens.
    """

    def __init__(
        self,
        config: LLMConfig | None = None,
        cache: bool = False,
        region_name: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        if config is None:
            config = LLMConfig()
            config.max_tokens = max_tokens

        if config.model is None:
            config.model = DEFAULT_MODEL

        if config.small_model is None:
            config.small_model = DEFAULT_SMALL_MODEL

        super().__init__(config, cache)

        self._bedrock = boto3.client('bedrock-runtime', region_name=region_name)

    def _get_max_tokens_for_model(self, model: str) -> int:
        return BEDROCK_MODEL_MAX_TOKENS.get(model, DEFAULT_BEDROCK_MAX_TOKENS)

    def _resolve_max_tokens(self, requested_max_tokens: int | None, model: str) -> int:
        if requested_max_tokens is not None:
            return requested_max_tokens
        if self.max_tokens is not None:
            return self.max_tokens
        return self._get_max_tokens_for_model(model)

    def _build_tool_config(
        self, response_model: type[BaseModel] | None
    ) -> tuple[dict[str, Any], str]:
        """Build a Bedrock toolConfig dict and return (tool_config, tool_name).

        Bedrock Converse API uses ``toolConfig`` with ``toolChoice`` to force
        the model to call a specific tool — equivalent to Anthropic's tool_use
        approach for structured output.
        """
        if response_model is not None:
            schema = response_model.model_json_schema()
            tool_name = response_model.__name__
            description = schema.get('description', f'Extract {tool_name} information')
        else:
            tool_name = 'generic_json_output'
            description = 'Output data in JSON format'
            schema = {
                'type': 'object',
                'additionalProperties': True,
                'description': 'Any JSON object containing the requested information',
            }

        # Bedrock Converse API tool schema format
        tool_config = {
            'tools': [
                {
                    'toolSpec': {
                        'name': tool_name,
                        'description': description,
                        'inputSchema': {'json': schema},
                    }
                }
            ],
            'toolChoice': {'tool': {'name': tool_name}},
        }
        return tool_config, tool_name

    def _extract_json_from_text(self, text: str) -> dict[str, Any]:
        try:
            json_start = text.find('{')
            json_end = text.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                return json.loads(text[json_start:json_end])
            raise ValueError(f'Could not extract JSON from model response: {text}')
        except (JSONDecodeError, ValueError) as e:
            raise ValueError(f'Could not extract JSON from model response: {text}') from e

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
    ) -> tuple[dict[str, Any], int, int]:
        """Call Bedrock Converse API and return (response_dict, input_tokens, output_tokens)."""
        model_id = (
            self.config.model if model_size == ModelSize.medium else self.config.small_model
        ) or DEFAULT_MODEL

        system_message = messages[0]
        converse_messages = [
            {'role': m.role, 'content': [{'text': m.content}]} for m in messages[1:]
        ]

        resolved_max_tokens = self._resolve_max_tokens(max_tokens, model_id)
        tool_config, tool_name = self._build_tool_config(response_model)

        request: dict[str, Any] = {
            'modelId': model_id,
            'system': [{'text': system_message.content}],
            'messages': converse_messages,
            'toolConfig': tool_config,
            'inferenceConfig': {
                'maxTokens': resolved_max_tokens,
                'temperature': float(self.temperature),
            },
        }

        try:
            response = await asyncio.to_thread(self._bedrock.converse, **request)
        except self._bedrock.exceptions.ThrottlingException as e:
            raise RateLimitError(
                f'Bedrock rate limit / throttling exceeded. Error: {e}'
            ) from e
        except Exception as e:
            if 'AccessDenied' in str(type(e).__name__) or 'AccessDeniedException' in str(e):
                raise RefusalError(
                    f'Bedrock access denied — check IAM permissions. Error: {e}'
                ) from e
            raise

        usage = response.get('usage', {})
        input_tokens: int = usage.get('inputTokens', 0)
        output_tokens: int = usage.get('outputTokens', 0)

        output = response.get('output', {})
        content_blocks = output.get('message', {}).get('content', [])

        # Extract tool use result (structured output)
        for block in content_blocks:
            if 'toolUse' in block and block['toolUse'].get('name') == tool_name:
                tool_input = block['toolUse'].get('input', {})
                if isinstance(tool_input, dict):
                    return tool_input, input_tokens, output_tokens
                return json.loads(str(tool_input)), input_tokens, output_tokens

        # Fallback: try to extract JSON from a text block
        for block in content_blocks:
            if 'text' in block:
                return self._extract_json_from_text(block['text']), input_tokens, output_tokens

        raise ValueError(
            f'Could not extract structured data from Bedrock response: {content_blocks}'
        )

    async def generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
        group_id: str | None = None,
        prompt_name: str | None = None,
    ) -> dict[str, Any]:
        """Generate a response from AWS Bedrock.

        Args:
            messages: List of Message objects to send to the model.
            response_model: Optional Pydantic model for structured output.
            max_tokens: Override the default max output tokens.
            model_size: Use the main model (medium) or the cheaper small model.
            group_id: Unused, kept for interface compatibility.
            prompt_name: Used for token tracking and tracing.

        Returns:
            Dictionary containing the structured response.
        """
        if max_tokens is None:
            max_tokens = self.max_tokens

        with self.tracer.start_span('llm.generate') as span:
            attributes: dict[str, Any] = {
                'llm.provider': 'bedrock',
                'model.size': model_size.value,
                'max_tokens': max_tokens,
            }
            if prompt_name:
                attributes['prompt.name'] = prompt_name
            span.add_attributes(attributes)

            retry_count = 0
            max_retries = 2
            last_error: Exception | None = None
            total_input_tokens = 0
            total_output_tokens = 0

            while retry_count <= max_retries:
                try:
                    response, input_tokens, output_tokens = await self._generate_response(
                        messages, response_model, max_tokens, model_size
                    )
                    total_input_tokens += input_tokens
                    total_output_tokens += output_tokens

                    self.token_tracker.record(prompt_name, total_input_tokens, total_output_tokens)

                    if response_model is not None:
                        model_instance = response_model(**response)
                        return model_instance.model_dump()

                    return response

                except (RateLimitError, RefusalError):
                    span.set_status('error', str(last_error))
                    raise
                except Exception as e:
                    last_error = e

                    if retry_count >= max_retries:
                        if isinstance(e, ValidationError):
                            logger.error(
                                f'Validation error after {retry_count}/{max_retries} attempts: {e}'
                            )
                        else:
                            logger.error(f'Max retries ({max_retries}) exceeded. Last error: {e}')
                        span.set_status('error', str(e))
                        span.record_exception(e)
                        raise e

                    if isinstance(e, ValidationError):
                        response_model_cast = typing.cast(type[BaseModel], response_model)
                        error_context = (
                            f'The previous response was invalid. Please provide a valid '
                            f'{response_model_cast.__name__} object. Error: {e}'
                        )
                    else:
                        error_context = (
                            f'The previous response attempt was invalid. '
                            f'Error type: {e.__class__.__name__}. '
                            f'Error details: {str(e)}. '
                            f'Please try again with a valid response.'
                        )

                    retry_count += 1
                    messages.append(Message(role='user', content=error_context))
                    logger.warning(
                        f'Retrying after error (attempt {retry_count}/{max_retries}): {e}'
                    )

            span.set_status('error', str(last_error))
            raise last_error or Exception('Max retries exceeded with no specific error')
