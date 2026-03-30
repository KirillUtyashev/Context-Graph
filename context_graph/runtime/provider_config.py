"""Shared provider/database config models used by runtime clients and app layers."""

from pydantic import BaseModel, Field


class OpenAIProviderConfig(BaseModel):
    """OpenAI provider configuration."""

    api_key: str | None = None
    api_url: str = "https://api.openai.com/v1"
    organization_id: str | None = None


class AzureOpenAIProviderConfig(BaseModel):
    """Azure OpenAI provider configuration."""

    api_key: str | None = None
    api_url: str | None = None
    api_version: str = "2024-10-21"
    deployment_name: str | None = None
    use_azure_ad: bool = False


class AnthropicProviderConfig(BaseModel):
    """Anthropic provider configuration."""

    api_key: str | None = None
    api_url: str = "https://api.anthropic.com"
    max_retries: int = 3


class GeminiProviderConfig(BaseModel):
    """Gemini provider configuration."""

    api_key: str | None = None
    project_id: str | None = None
    location: str = "us-central1"


class GroqProviderConfig(BaseModel):
    """Groq provider configuration."""

    api_key: str | None = None
    api_url: str = "https://api.groq.com/openai/v1"


class VoyageProviderConfig(BaseModel):
    """Voyage AI provider configuration."""

    api_key: str | None = None
    api_url: str = "https://api.voyageai.com/v1"
    model: str = "voyage-3"


class BedrockEmbedderProviderConfig(BaseModel):
    """AWS Bedrock embedder provider configuration."""

    region_name: str | None = None
    input_type: str = "search_document"


class LLMProvidersConfig(BaseModel):
    """LLM providers configuration."""

    openai: OpenAIProviderConfig | None = None
    azure_openai: AzureOpenAIProviderConfig | None = None
    anthropic: AnthropicProviderConfig | None = None
    gemini: GeminiProviderConfig | None = None
    groq: GroqProviderConfig | None = None


class LLMConfig(BaseModel):
    """LLM configuration."""

    provider: str = Field(default="openai", description="LLM provider")
    model: str = Field(default="gpt-4o-mini", description="Model name")
    temperature: float | None = Field(
        default=None,
        description="Temperature (optional, defaults to None for reasoning models)",
    )
    max_tokens: int = Field(default=4096, description="Max tokens")
    providers: LLMProvidersConfig = Field(default_factory=LLMProvidersConfig)


class EmbedderProvidersConfig(BaseModel):
    """Embedder providers configuration."""

    openai: OpenAIProviderConfig | None = None
    azure_openai: AzureOpenAIProviderConfig | None = None
    gemini: GeminiProviderConfig | None = None
    voyage: VoyageProviderConfig | None = None
    bedrock: BedrockEmbedderProviderConfig | None = None


class EmbedderConfig(BaseModel):
    """Embedder configuration."""

    provider: str = Field(default="openai", description="Embedder provider")
    model: str = Field(default="text-embedding_clients-3-small", description="Model name")
    dimensions: int = Field(default=1536, description="Embedding dimensions")
    providers: EmbedderProvidersConfig = Field(default_factory=EmbedderProvidersConfig)


class Neo4jProviderConfig(BaseModel):
    """Neo4j provider configuration."""

    uri: str = "bolt://localhost:7687"
    username: str = "neo4j"
    password: str | None = None
    database: str = "neo4j"
    use_parallel_runtime: bool = False


class FalkorDBProviderConfig(BaseModel):
    """FalkorDB provider configuration."""

    uri: str = "redis://localhost:6379"
    password: str | None = None
    database: str = "default_db"


class NeptuneProviderConfig(BaseModel):
    """Amazon Neptune provider configuration."""

    host: str = Field(
        description="Neptune endpoint prefixed with neptune-db:// or neptune-graph://"
    )
    aoss_host: str | None = Field(
        default=None,
        description="Amazon OpenSearch Serverless host. Optional — omit to disable BM25 search.",
    )


class DatabaseProvidersConfig(BaseModel):
    """Database providers configuration."""

    neo4j: Neo4jProviderConfig | None = None
    falkordb: FalkorDBProviderConfig | None = None
    neptune: NeptuneProviderConfig | None = None


class DatabaseConfig(BaseModel):
    """Database configuration."""

    provider: str = Field(default="neo4j", description="Database provider")
    providers: DatabaseProvidersConfig = Field(default_factory=DatabaseProvidersConfig)
