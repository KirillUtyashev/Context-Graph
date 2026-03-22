from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import sys
import types
from typing import Any


def ensure_graphiti_vendor_path() -> None:
    root = Path(__file__).resolve().parent.parent / "vendor" / "graphiti"
    path = str(root)
    if root.exists() and path not in sys.path:
        sys.path.insert(0, path)
    package_root = root / "graphiti_core"
    if package_root.exists() and "graphiti_core" not in sys.modules:
        package = types.ModuleType("graphiti_core")
        package.__path__ = [str(package_root)]  # type: ignore[attr-defined]
        package.__file__ = str(package_root / "__init__.py")
        sys.modules["graphiti_core"] = package


@dataclass(frozen=True, slots=True)
class EpisodeLLMSettings:
    provider: str
    model: str | None = None
    small_model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    aws_region: str | None = None


def load_episode_llm_settings(args: Any | None = None) -> EpisodeLLMSettings | None:
    provider = _arg_or_env(args, "llm_provider", "CONTEXT_GRAPH_LLM_PROVIDER")
    if not provider:
        return None
    provider = provider.strip().lower()
    if provider not in {"openai", "bedrock"}:
        raise RuntimeError(
            f"unsupported LLM provider {provider!r}; expected 'openai' or 'bedrock'"
        )

    settings = EpisodeLLMSettings(
        provider=provider,
        model=_arg_or_env(args, "llm_model", "CONTEXT_GRAPH_LLM_MODEL"),
        small_model=_arg_or_env(args, "llm_small_model", "CONTEXT_GRAPH_LLM_SMALL_MODEL"),
        base_url=_arg_or_env(args, "llm_base_url", "CONTEXT_GRAPH_LLM_BASE_URL"),
        api_key=_arg_or_env(args, "llm_api_key", "CONTEXT_GRAPH_LLM_API_KEY")
        or os.getenv("OPENAI_API_KEY"),
        aws_region=_arg_or_env(args, "aws_region", "AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION"),
    )

    if settings.provider == "openai" and not settings.api_key:
        raise RuntimeError(
            "OpenAI episode extraction requires OPENAI_API_KEY or --llm-api-key"
        )
    return settings


def make_episode_llm_client(settings: EpisodeLLMSettings | None) -> Any | None:
    if settings is None:
        return None

    ensure_graphiti_vendor_path()
    from graphiti_core.llm_client.config import LLMConfig

    config = LLMConfig(
        api_key=settings.api_key,
        model=settings.model,
        base_url=settings.base_url,
        small_model=settings.small_model,
    )

    if settings.provider == "openai":
        from graphiti_core.llm_client.openai_client import OpenAIClient

        return OpenAIClient(config=config)

    if settings.provider == "bedrock":
        from graphiti_core.llm_client.bedrock_client import BedrockLLMClient

        return BedrockLLMClient(config=config, region_name=settings.aws_region)

    raise RuntimeError(f"unsupported LLM provider {settings.provider!r}")


def _arg_or_env(args: Any | None, attr: str, env_name: str) -> str | None:
    if args is not None:
        value = getattr(args, attr, None)
        if value:
            return str(value)
    value = os.getenv(env_name)
    return value if value else None
