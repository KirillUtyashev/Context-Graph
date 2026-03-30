"""Factory classes for creating runtime clients from MCP config."""

from __future__ import annotations

import logging
import os

from context_graph.runtime.embedding_clients import (
    BedrockTitanEmbedder,
    BedrockTitanEmbedderConfig,
    EmbedderClient,
    OpenAIEmbedder,
)
from context_graph.runtime.embedding_clients.openai import OpenAIEmbedderConfig
from context_graph.runtime.llm import BedrockLLMClient, LLMClient, OpenAIClient
from context_graph.runtime.llm.config import LLMConfig as RuntimeLLMConfig
from context_graph.runtime.provider_config import (
    BedrockEmbedderProviderConfig,
    DatabaseConfig,
    EmbedderConfig,
    LLMConfig,
    Neo4jProviderConfig,
)
from context_graph.backend import Neo4jConfig, NeptuneConfig


def _validate_api_key(provider_name: str, api_key: str | None, logger: logging.Logger) -> str:
    if not api_key:
        raise ValueError(
            f"{provider_name} API key is not configured. Please set the appropriate environment variable."
        )
    logger.info("Creating %s client", provider_name)
    return api_key


class LLMClientFactory:
    """Create the LLM client used by extraction and query parsing."""

    @staticmethod
    def create(config: LLMConfig) -> LLMClient:
        logger = logging.getLogger(__name__)
        provider = config.provider.lower()

        if provider == "openai":
            if not config.providers.openai:
                raise ValueError("OpenAI provider configuration not found")

            api_key = _validate_api_key("OpenAI", config.providers.openai.api_key, logger)
            llm_config = RuntimeLLMConfig(
                api_key=api_key,
                base_url=config.providers.openai.api_url,
                model=config.model,
                small_model=getattr(config, "small_model", None) or config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
            )
            reasoning_prefixes = ("o1", "o3", "gpt-5")
            is_reasoning_model = bool(config.model) and config.model.startswith(reasoning_prefixes)
            if is_reasoning_model:
                return OpenAIClient(config=llm_config, reasoning="minimal", verbosity="low")
            return OpenAIClient(config=llm_config, reasoning=None, verbosity=None)

        if provider == "bedrock":
            logger.info("Creating Bedrock client (IAM auth)")
            llm_config = RuntimeLLMConfig(
                model=config.model,
                small_model=getattr(config, "small_model", None) or config.model,
                temperature=config.temperature if config.temperature is not None else 1.0,
                max_tokens=config.max_tokens,
            )
            return BedrockLLMClient(config=llm_config)

        raise ValueError(
            f"Unsupported LLM provider: {provider}. Supported providers are: openai, bedrock."
        )


class EmbedderFactory:
    """Create the embedder used by graph search and profile indexing."""

    @staticmethod
    def create(config: EmbedderConfig) -> EmbedderClient:
        logger = logging.getLogger(__name__)
        provider = config.provider.lower()

        if provider == "openai":
            if not config.providers.openai:
                raise ValueError("OpenAI provider configuration not found")

            api_key = _validate_api_key("OpenAI Embedder", config.providers.openai.api_key, logger)
            return OpenAIEmbedder(
                config=OpenAIEmbedderConfig(
                    api_key=api_key,
                    embedding_model=config.model,
                    base_url=config.providers.openai.api_url,
                    embedding_dim=config.dimensions,
                )
            )

        if provider in {"bedrock", "bedrock_titan"}:
            logger.info("Creating Bedrock Titan embedder (IAM auth)")
            bedrock_provider_cfg = config.providers.bedrock or BedrockEmbedderProviderConfig()
            return BedrockTitanEmbedder(
                config=BedrockTitanEmbedderConfig(
                    embedding_model=config.model or "amazon.titan-embed-text-v2:0",
                    embedding_dim=config.dimensions or 1024,
                    region_name=bedrock_provider_cfg.region_name,
                )
            )

        raise ValueError(
            f"Unsupported Embedder provider: {provider}. Supported providers are: openai, bedrock."
        )


class DatabaseDriverFactory:
    """Create database connection settings for the graph runtime."""

    @staticmethod
    def create_backend_config(config: DatabaseConfig) -> Neo4jConfig | NeptuneConfig:
        provider = config.provider.lower()

        if provider == "neo4j":
            neo4j_config = config.providers.neo4j or Neo4jProviderConfig()
            return Neo4jConfig(
                uri=os.environ.get("NEO4J_URI", neo4j_config.uri),
                user=os.environ.get("NEO4J_USER", neo4j_config.username),
                password=os.environ.get("NEO4J_PASSWORD", neo4j_config.password),
                database=os.environ.get("NEO4J_DATABASE", neo4j_config.database),
            )

        if provider == "neptune":
            host = os.environ.get(
                "NEPTUNE_HOST",
                config.providers.neptune.host if config.providers.neptune else "",
            )
            aoss_host = (
                os.environ.get(
                    "AOSS_HOST",
                    config.providers.neptune.aoss_host if config.providers.neptune else None,
                )
                or None
            )
            if not host:
                raise ValueError(
                    "Neptune host is required. Set NEPTUNE_HOST or configure "
                    "database.providers.neptune.host."
                )
            return NeptuneConfig(
                host=host,
                aoss_host=aoss_host,
            )

        raise ValueError(
            f"Unsupported Database provider: {provider}. Supported providers are: neo4j, neptune."
        )

    @staticmethod
    def create_config(config: DatabaseConfig) -> dict[str, str | None]:
        """Legacy dict view of the backend config used by older callers."""

        backend_config = DatabaseDriverFactory.create_backend_config(config)
        if isinstance(backend_config, Neo4jConfig):
            return {
                "uri": backend_config.uri,
                "user": backend_config.user,
                "password": backend_config.password,
                "database": backend_config.database,
            }
        if isinstance(backend_config, NeptuneConfig):
            return {
                "host": backend_config.host,
                "aoss_host": backend_config.aoss_host,
            }
        raise TypeError(f"Unsupported backend config: {type(backend_config)!r}")
