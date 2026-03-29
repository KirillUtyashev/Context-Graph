from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import threading
from typing import Any, Sequence


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    provider: str
    model: str | None = None
    dimensions: int = 1024
    base_url: str | None = None
    api_key: str | None = None
    aws_region: str | None = None


class EmbeddingProvider:
    def __init__(self, client: Any) -> None:
        self.client = client

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        cleaned = [" ".join(text.strip().split()) for text in texts if text and text.strip()]
        if not cleaned:
            return []
        return _run_async(self.client.create_batch(list(cleaned)))


def load_embedding_settings(args: Any | None = None) -> EmbeddingSettings | None:
    provider = _arg_or_env(args, "embed_provider", "CONTEXT_GRAPH_EMBED_PROVIDER")
    if not provider:
        inferred_provider = _infer_embedding_provider(args)
        if inferred_provider is None:
            return None
        provider = inferred_provider
    provider = provider.strip().lower()
    if provider not in {"openai", "bedrock"}:
        raise RuntimeError(
            f"unsupported embedding provider {provider!r}; expected 'openai' or 'bedrock'"
        )

    dimensions_raw = _arg_or_env(args, "embed_dimensions", "CONTEXT_GRAPH_EMBED_DIMENSIONS")
    dimensions = int(dimensions_raw) if dimensions_raw else 1024

    settings = EmbeddingSettings(
        provider=provider,
        model=_arg_or_env(args, "embed_model", "CONTEXT_GRAPH_EMBED_MODEL"),
        dimensions=dimensions,
        base_url=_arg_or_env(args, "embed_base_url", "CONTEXT_GRAPH_EMBED_BASE_URL"),
        api_key=_arg_or_env(args, "embed_api_key", "CONTEXT_GRAPH_EMBED_API_KEY")
        or os.getenv("OPENAI_API_KEY"),
        aws_region=_arg_or_env(args, "aws_region", "AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION"),
    )
    if settings.provider == "openai" and not settings.api_key:
        raise RuntimeError(
            "OpenAI embeddings require CONTEXT_GRAPH_EMBED_API_KEY, OPENAI_API_KEY, or --embed-api-key"
        )
    return settings


def make_embedding_provider(settings: EmbeddingSettings | None) -> EmbeddingProvider | None:
    if settings is None:
        return None

    if settings.provider == "openai":
        from .runtime.embedding.openai import OpenAIEmbedder, OpenAIEmbedderConfig

        client = OpenAIEmbedder(
            config=OpenAIEmbedderConfig(
                api_key=settings.api_key,
                base_url=settings.base_url,
                embedding_model=settings.model or "text-embedding-3-small",
                embedding_dim=settings.dimensions,
            )
        )
        return EmbeddingProvider(client)

    if settings.provider == "bedrock":
        from .runtime.embedding.bedrock import BedrockTitanEmbedder, BedrockTitanEmbedderConfig

        client = BedrockTitanEmbedder(
            config=BedrockTitanEmbedderConfig(
                embedding_model=settings.model or "amazon.titan-embed-text-v2:0",
                embedding_dim=settings.dimensions,
                region_name=settings.aws_region,
            )
        )
        return EmbeddingProvider(client)

    raise RuntimeError(f"unsupported embedding provider {settings.provider!r}")


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive handoff
            error.append(exc)

    thread = threading.Thread(target=_runner, daemon=True, name="context-graph-embeddings")
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result.get("value")


def _arg_or_env(args: Any | None, attr: str, env_name: str) -> str | None:
    if args is not None:
        value = getattr(args, attr, None)
        if value is not None and str(value):
            return str(value)
    value = os.getenv(env_name)
    return value if value else None


def _infer_embedding_provider(args: Any | None = None) -> str | None:
    llm_provider = _arg_or_env(args, "llm_provider", "CONTEXT_GRAPH_LLM_PROVIDER")
    if llm_provider:
        normalized = llm_provider.strip().lower()
        if normalized in {"openai", "bedrock"}:
            return normalized

    if _arg_or_env(args, "embed_api_key", "CONTEXT_GRAPH_EMBED_API_KEY") or os.getenv("OPENAI_API_KEY"):
        return "openai"

    aws_region = _arg_or_env(args, "aws_region", "AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if aws_region:
        return "bedrock"

    return None
