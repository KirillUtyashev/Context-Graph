"""Configuration schemas with pydantic-settings and YAML support."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from context_graph.runtime.provider_config import (
    AnthropicProviderConfig,
    AzureOpenAIProviderConfig,
    BedrockEmbedderProviderConfig,
    DatabaseConfig,
    DatabaseProvidersConfig,
    EmbedderConfig,
    EmbedderProvidersConfig,
    FalkorDBProviderConfig,
    GeminiProviderConfig,
    GroqProviderConfig,
    LLMConfig,
    LLMProvidersConfig,
    Neo4jProviderConfig,
    NeptuneProviderConfig,
    OpenAIProviderConfig,
    VoyageProviderConfig,
)


def default_config_path() -> Path:
    """Return the default on-disk config path for the standalone MCP app."""

    return Path(__file__).resolve().parents[2] / "config" / "config.yaml"


class YamlSettingsSource(PydanticBaseSettingsSource):
    """Custom settings source for loading from YAML files."""

    def __init__(self, settings_cls: type[BaseSettings], config_path: Path | None = None):
        super().__init__(settings_cls)
        self.config_path = config_path or default_config_path()

    def _expand_env_vars(self, value: Any) -> Any:
        """Recursively expand environment variables in configuration values."""

        if isinstance(value, str):
            import re

            def replacer(match):
                var_name = match.group(1)
                default_value = match.group(3) if match.group(3) is not None else ""
                return os.environ.get(var_name, default_value)

            pattern = r"\$\{([^:}]+)(:([^}]*))?\}"
            full_match = re.fullmatch(pattern, value)
            if full_match:
                result = replacer(full_match)
                if isinstance(result, str):
                    lower_result = result.lower().strip()
                    if lower_result in ("true", "1", "yes", "on"):
                        return True
                    if lower_result in ("false", "0", "no", "off"):
                        return False
                    if lower_result == "":
                        return None
                return result
            return re.sub(pattern, replacer, value)

        if isinstance(value, dict):
            return {k: self._expand_env_vars(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._expand_env_vars(item) for item in value]
        return value

    def get_field_value(self, field_name: str, field_info: Any) -> Any:
        """Get field value from YAML config."""

        return None

    def __call__(self) -> dict[str, Any]:
        """Load and parse YAML configuration."""

        if not self.config_path.exists():
            return {}

        with self.config_path.open(encoding="utf-8") as handle:
            raw_config = yaml.safe_load(handle) or {}

        return self._expand_env_vars(raw_config)


class ServerConfig(BaseModel):
    """Server transport configuration."""

    transport: str = Field(
        default="http",
        description="Transport type: http (default, recommended), stdio, or sse (deprecated)",
    )
    host: str = Field(default="0.0.0.0", description="Server host")
    port: int = Field(default=8000, description="Server port")


class EntityTypeConfig(BaseModel):
    """Configured entity type for graph extraction."""

    name: str
    description: str


class NamespaceConfig(BaseModel):
    """Namespace configuration for MCP-backed graph data."""

    group_id: str = Field(default="main", description="Group ID")
    episode_id_prefix: str | None = Field(default="", description="Episode ID prefix")
    user_id: str = Field(default="mcp_user", description="User ID")
    entity_types: list[EntityTypeConfig] = Field(default_factory=list)

    def model_post_init(self, __context) -> None:
        if self.episode_id_prefix is None:
            self.episode_id_prefix = ""


class MCPServerConfig(BaseSettings):
    """MCP server configuration with YAML and environment support."""

    server: ServerConfig = Field(default_factory=ServerConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    namespace: NamespaceConfig = Field(default_factory=NamespaceConfig)

    model_config = SettingsConfigDict(
        env_prefix="",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        config_path = Path(os.environ.get("CONFIG_PATH", str(default_config_path())))
        yaml_settings = YamlSettingsSource(settings_cls, config_path)
        return (init_settings, env_settings, yaml_settings, dotenv_settings)

    def apply_cli_overrides(self, args) -> None:
        """Apply CLI argument overrides to configuration."""

        if hasattr(args, "transport") and args.transport:
            self.server.transport = args.transport

        if hasattr(args, "llm_provider") and args.llm_provider:
            self.llm.provider = args.llm_provider
        if hasattr(args, "model") and args.model:
            self.llm.model = args.model
        if hasattr(args, "temperature") and args.temperature is not None:
            self.llm.temperature = args.temperature

        if hasattr(args, "embedder_provider") and args.embedder_provider:
            self.embedder.provider = args.embedder_provider
        if hasattr(args, "embedder_model") and args.embedder_model:
            self.embedder.model = args.embedder_model

        if hasattr(args, "database_provider") and args.database_provider:
            self.database.provider = args.database_provider

        if hasattr(args, "group_id") and args.group_id:
            self.namespace.group_id = args.group_id
        if hasattr(args, "user_id") and args.user_id:
            self.namespace.user_id = args.user_id


__all__ = [
    "AnthropicProviderConfig",
    "AzureOpenAIProviderConfig",
    "BedrockEmbedderProviderConfig",
    "DatabaseConfig",
    "DatabaseProvidersConfig",
    "EmbedderConfig",
    "EmbedderProvidersConfig",
    "EntityTypeConfig",
    "FalkorDBProviderConfig",
    "GeminiProviderConfig",
    "GroqProviderConfig",
    "LLMConfig",
    "LLMProvidersConfig",
    "MCPServerConfig",
    "NamespaceConfig",
    "Neo4jProviderConfig",
    "NeptuneProviderConfig",
    "OpenAIProviderConfig",
    "PydanticBaseSettingsSource",
    "ServerConfig",
    "VoyageProviderConfig",
    "YamlSettingsSource",
]
