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
from collections.abc import Iterable

from pydantic import Field

from .client import EmbedderClient, EmbedderConfig

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = 'cohere.embed-english-v3'
DEFAULT_EMBEDDING_DIM = 1024

DEFAULT_TITAN_EMBEDDING_MODEL = 'amazon.titan-embed-text-v2:0'
DEFAULT_TITAN_EMBEDDING_DIM = 1024


class BedrockEmbedderConfig(EmbedderConfig):
    embedding_model: str = Field(default=DEFAULT_EMBEDDING_MODEL)
    embedding_dim: int = Field(default=DEFAULT_EMBEDDING_DIM)
    region_name: str | None = Field(
        default=None,
        description='AWS region. Defaults to boto3 session default (env var or ~/.aws/config).',
    )
    input_type: str = Field(
        default='search_document',
        description=(
            'Cohere input_type: "search_document" for storing, "search_query" for retrieval. '
            'Using search_document for both is safe and consistent.'
        ),
    )


class BedrockEmbedder(EmbedderClient):
    """Embedder backed by AWS Bedrock InvokeModel (Cohere embed models).

    Uses boto3 standard credential chain — IAM instance role on EC2, or
    ~/.aws/credentials / AWS_PROFILE locally. No API key required.

    Default model: cohere.embed-english-v3 (1024 dims).
    """

    def __init__(self, config: BedrockEmbedderConfig | None = None):
        if config is None:
            config = BedrockEmbedderConfig()
        self.config = config
        import boto3

        self._client = boto3.client('bedrock-runtime', region_name=config.region_name)

    def _invoke_sync(self, texts: list[str]) -> list[list[float]]:
        """Synchronous Bedrock InvokeModel call — run via executor to avoid blocking the event loop."""
        body = json.dumps({'texts': texts, 'input_type': self.config.input_type})
        response = self._client.invoke_model(
            modelId=self.config.embedding_model,
            contentType='application/json',
            accept='application/json',
            body=body,
        )
        result = json.loads(response['body'].read())
        return [emb[: self.config.embedding_dim] for emb in result['embeddings']]

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        text = input_data if isinstance(input_data, str) else str(input_data)
        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(None, self._invoke_sync, [text])
        return embeddings[0]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._invoke_sync, input_data_list)


class BedrockTitanEmbedderConfig(EmbedderConfig):
    embedding_model: str = Field(default=DEFAULT_TITAN_EMBEDDING_MODEL)
    embedding_dim: int = Field(default=DEFAULT_TITAN_EMBEDDING_DIM)
    region_name: str | None = Field(
        default=None,
        description='AWS region. Defaults to boto3 session default (env var or ~/.aws/config).',
    )
    normalize: bool = Field(default=True)


class BedrockTitanEmbedder(EmbedderClient):
    """Embedder backed by AWS Bedrock InvokeModel (Amazon Titan Text Embeddings v2).

    No Marketplace subscription required — Amazon-native model available to all
    Bedrock-enabled accounts. Uses boto3 standard credential chain.

    Default model: amazon.titan-embed-text-v2:0 (supports 256 / 512 / 1024 dims).
    """

    def __init__(self, config: BedrockTitanEmbedderConfig | None = None):
        if config is None:
            config = BedrockTitanEmbedderConfig()
        self.config = config
        import boto3

        self._client = boto3.client('bedrock-runtime', region_name=config.region_name)

    def _invoke_one(self, text: str) -> list[float]:
        body = json.dumps(
            {
                'inputText': text,
                'dimensions': self.config.embedding_dim,
                'normalize': self.config.normalize,
            }
        )
        response = self._client.invoke_model(
            modelId=self.config.embedding_model,
            contentType='application/json',
            accept='application/json',
            body=body,
        )
        result = json.loads(response['body'].read())
        return result['embedding_clients']

    def _invoke_batch_sync(self, texts: list[str]) -> list[list[float]]:
        return [self._invoke_one(t) for t in texts]

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        text = input_data if isinstance(input_data, str) else str(input_data)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._invoke_one, text)

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._invoke_batch_sync, input_data_list)
