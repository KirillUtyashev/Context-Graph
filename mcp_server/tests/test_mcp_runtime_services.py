from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config.settings import MCPServerConfig
from src.runtime import backend as backend_module
from src.runtime import metadata as metadata_module


class FakeAsyncDriver:
    def __init__(self) -> None:
        self.build_indices = AsyncMock()


class FakeSQLiteMetadataStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.bootstrap_calls = 0
        self.connection = None

    def bootstrap(self) -> None:
        self.bootstrap_calls += 1


class FakePostgresMetadataStore:
    def __init__(self, dsn: str = "") -> None:
        self.dsn = dsn
        self.bootstrap_calls = 0
        self.connection = None

    @classmethod
    def from_dsn(cls, dsn: str):
        return cls(dsn)

    def bootstrap(self) -> None:
        self.bootstrap_calls += 1


class FakeInMemoryVectorStore:
    def __init__(self) -> None:
        self.bootstrap_calls = 0
        self.connection = None

    def bootstrap(self) -> None:
        self.bootstrap_calls += 1


class FakeAuroraVectorStore:
    def __init__(self) -> None:
        self.bootstrap_calls = 0
        self.connection = None
        self.needs_resync = False
        self.mark_resynced_calls = 0

    @classmethod
    def from_metadata_store(cls, metadata_store, *, embedding_dim: int):
        store = cls()
        store.metadata_store = metadata_store
        store.embedding_dim = embedding_dim
        return store

    def bootstrap(self) -> None:
        self.bootstrap_calls += 1

    def mark_resynced(self) -> None:
        self.mark_resynced_calls += 1


class FakeProfileVectorIndexer:
    def __init__(self, *, vector_store, embedding_provider) -> None:
        self.vector_store = vector_store
        self.embedding_provider = embedding_provider
        self.sync_calls: list[tuple[str, object]] = []

    def sync_repo(self, *, repo_id: str, metadata_store) -> None:
        self.sync_calls.append((repo_id, metadata_store))


class FakeProfileSearchService:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.search_calls = []

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return kwargs.pop("result")


class FakeSemanticProvenanceSearchService:
    def __init__(self, *, profile_search_service) -> None:
        self.profile_search_service = profile_search_service
        self.search_calls = []
        self.result = None

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return self.result


class FakeHit:
    def __init__(self, score: float, key_text: str, key_origin: str, profile_id: str = "profile:1") -> None:
        self.profile_id = profile_id
        self.score = score
        self.key_text = key_text
        self.key_origin = key_origin


class FakeProfile:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


def make_profile_result():
    return SimpleNamespace(
        hits=[FakeHit(0.91, "matched text", "semantic")],
        profiles=[
            FakeProfile(
                profile_id="profile:1",
                profile_kind="semantic_claim_profile",
                profile_target_kind="Claim",
                profile_target_id="claim:1",
                relation_type=None,
                value_text="JWT audience validation is required.",
                source_node_id=None,
                target_node_id=None,
                is_active=True,
                metadata={"source": "test"},
            )
        ],
    )


@pytest.mark.asyncio
async def test_mcp_backend_runtime_initializes_driver_and_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = backend_module.MCPBackendRuntime(MCPServerConfig(), semaphore_limit=3)
    driver = FakeAsyncDriver()

    monkeypatch.setattr("context_graph.runtime.providers.factories.LLMClientFactory.create", lambda cfg: "llm")
    monkeypatch.setattr(
        "context_graph.runtime.providers.factories.EmbedderFactory.create",
        lambda cfg: "embedder",
    )
    monkeypatch.setattr(
        "context_graph.runtime.providers.factories.DatabaseDriverFactory.create_backend_config",
        lambda cfg: "backend-config",
    )
    monkeypatch.setattr("context_graph.backend.create_driver", lambda cfg: driver)
    monkeypatch.setattr(
        "context_graph.provenance.ClaimResolutionService",
        lambda *, llm_client: SimpleNamespace(llm_client=llm_client),
    )

    await runtime.ensure_initialized()
    assert runtime.driver is driver
    driver.build_indices.assert_awaited_once()
    assert runtime.llm_client == "llm"
    assert runtime.embedder_client == "embedder"
    assert runtime.claim_resolution_service.llm_client == "llm"
    assert runtime._initialized is True


@pytest.mark.asyncio
async def test_mcp_backend_runtime_handles_client_factory_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = backend_module.MCPBackendRuntime(MCPServerConfig())
    driver = FakeAsyncDriver()

    def fail_llm(_config):
        raise RuntimeError("no llm")

    def fail_embedder(_config):
        raise RuntimeError("no embedder")

    monkeypatch.setattr("context_graph.runtime.providers.factories.LLMClientFactory.create", fail_llm)
    monkeypatch.setattr("context_graph.runtime.providers.factories.EmbedderFactory.create", fail_embedder)
    monkeypatch.setattr(
        "context_graph.runtime.providers.factories.DatabaseDriverFactory.create_backend_config",
        lambda cfg: "backend-config",
    )
    monkeypatch.setattr("context_graph.backend.create_driver", lambda cfg: driver)
    monkeypatch.setattr(
        "context_graph.provenance.ClaimResolutionService",
        lambda *, llm_client: SimpleNamespace(llm_client=llm_client),
    )

    await runtime.ensure_initialized()
    assert runtime.llm_client is None
    assert runtime.embedder_client is None
    driver.build_indices.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_backend_runtime_surfaces_cached_and_fatal_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = backend_module.MCPBackendRuntime(MCPServerConfig())
    runtime._initialized = True
    await runtime.ensure_initialized()

    errored = backend_module.MCPBackendRuntime(MCPServerConfig())
    errored._init_error = "boom"
    with pytest.raises(RuntimeError, match="boom"):
        await errored.ensure_initialized()

    failing = backend_module.MCPBackendRuntime(MCPServerConfig())
    monkeypatch.setattr(
        "context_graph.runtime.providers.factories.DatabaseDriverFactory.create_backend_config",
        lambda cfg: (_ for _ in ()).throw(ValueError("bad config")),
    )
    with pytest.raises(ValueError, match="bad config"):
        await failing.ensure_initialized()
    assert failing._init_error == "bad config"


@pytest.mark.asyncio
async def test_mcp_backend_runtime_get_driver_errors_when_driver_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = backend_module.MCPBackendRuntime(MCPServerConfig())
    monkeypatch.setattr(runtime, "ensure_initialized", AsyncMock())

    with pytest.raises(RuntimeError, match="backend driver failed to initialize"):
        await runtime.get_driver()


def test_metadata_snapshot_service_builds_dsns(monkeypatch: pytest.MonkeyPatch) -> None:
    service = metadata_module.MetadataSnapshotService(MCPServerConfig())

    monkeypatch.setenv("POSTGRES_DSN", "postgresql://explicit")
    assert service._build_metadata_dsn() == "postgresql://explicit"

    monkeypatch.delenv("POSTGRES_DSN")
    monkeypatch.setenv("DATABASE_URL", "postgresql://database-url")
    assert service._build_metadata_dsn() == "postgresql://database-url"

    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("AURORA_ENDPOINT", raising=False)
    monkeypatch.setenv("METADATA_SQLITE_PATH", "/tmp/metadata.sqlite")
    assert service._build_metadata_dsn() == "sqlite:////tmp/metadata.sqlite"


def test_metadata_snapshot_service_builds_aurora_dsn_and_handles_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    monkeypatch.delenv("POSTGRES_DSN", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("METADATA_SQLITE_PATH", raising=False)
    monkeypatch.setenv("AURORA_ENDPOINT", "db.example.internal")
    monkeypatch.setenv("AURORA_DB_NAME", "context_graph")
    monkeypatch.setenv("AURORA_USERNAME", "postgres")
    monkeypatch.setenv("AURORA_PASSWORD_SECRET_ARN", "arn:secret")
    monkeypatch.setenv("AWS_REGION", "ca-central-1")

    class FakeSecretsManager:
        def get_secret_value(self, *, SecretId: str):
            assert SecretId == "arn:secret"
            return {"SecretString": '{"password": "pass word"}'}

    fake_boto3 = SimpleNamespace(client=lambda service_name, region_name: FakeSecretsManager())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    assert service._build_metadata_dsn() == "postgresql://postgres:pass+word@db.example.internal:5432/context_graph"

    fake_boto3.client = lambda service_name, region_name: (_ for _ in ()).throw(
        RuntimeError("secret failure")
    )
    assert service._build_metadata_dsn() is None

    monkeypatch.delenv("AURORA_PASSWORD_SECRET_ARN", raising=False)
    assert service._build_metadata_dsn() is None


def test_initialize_metadata_store_covers_cached_sqlite_postgres_and_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata_module, "SQLiteMetadataStore", FakeSQLiteMetadataStore)
    monkeypatch.setattr(metadata_module, "PostgresMetadataStore", FakePostgresMetadataStore)

    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    service._metadata = object()
    assert service.initialize_metadata_store() is True

    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    service._metadata_init_error = "already failed"
    assert service.initialize_metadata_store() is False

    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    monkeypatch.setattr(service, "_build_metadata_dsn", lambda: None)
    assert service.initialize_metadata_store() is False
    assert "could not be configured" in (service.metadata_init_error or "")

    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    monkeypatch.setattr(service, "_build_metadata_dsn", lambda: "sqlite:////tmp/demo.sqlite")
    assert service.initialize_metadata_store() is True
    assert isinstance(service.metadata_store, FakeSQLiteMetadataStore)
    assert service.metadata_store.path == "/tmp/demo.sqlite"

    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    monkeypatch.setattr(service, "_build_metadata_dsn", lambda: "postgresql://demo")
    assert service.initialize_metadata_store() is True
    assert isinstance(service.metadata_store, FakePostgresMetadataStore)
    assert service.metadata_store.dsn == "postgresql://demo"

    class ExplodingSQLiteStore(FakeSQLiteMetadataStore):
        def bootstrap(self) -> None:
            raise RuntimeError("bootstrap failed")

    monkeypatch.setattr(metadata_module, "SQLiteMetadataStore", ExplodingSQLiteStore)
    failing = metadata_module.MetadataSnapshotService(MCPServerConfig())
    monkeypatch.setattr(failing, "_build_metadata_dsn", lambda: "sqlite:////tmp/fail.sqlite")
    assert failing.initialize_metadata_store() is False
    assert failing.metadata_init_error == "bootstrap failed"


def test_initialize_profile_search_covers_success_and_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MCPServerConfig()
    config.embedder.dimensions = 256

    monkeypatch.setattr(metadata_module, "PostgresMetadataStore", FakePostgresMetadataStore)
    monkeypatch.setattr(metadata_module, "AuroraProfileVectorStore", FakeAuroraVectorStore)
    monkeypatch.setattr(metadata_module, "InMemoryProfileVectorStore", FakeInMemoryVectorStore)
    monkeypatch.setattr(
        "context_graph.backend.profile_vector_services.ProfileSearchService",
        FakeProfileSearchService,
    )
    monkeypatch.setattr(
        "context_graph.backend.profile_vector_services.ProfileVectorIndexer",
        FakeProfileVectorIndexer,
    )
    monkeypatch.setattr(
        "context_graph.backend.profile_vector_services.SemanticProvenanceSearchService",
        FakeSemanticProvenanceSearchService,
    )
    monkeypatch.setattr("context_graph.query.parser.QueryParser", lambda *, llm_client: ("parser", llm_client))
    monkeypatch.setattr("context_graph.runtime.embeddings.EmbeddingProvider", lambda raw: ("provider", raw))
    monkeypatch.setattr("context_graph.runtime.providers.factories.LLMClientFactory.create", lambda cfg: "llm")
    monkeypatch.setattr("context_graph.runtime.providers.factories.EmbedderFactory.create", lambda cfg: "embedder")

    service = metadata_module.MetadataSnapshotService(config)
    service._metadata = FakePostgresMetadataStore("postgresql://demo")
    monkeypatch.setattr(service, "initialize_metadata_store", lambda: True)
    assert service.initialize_profile_search() is True
    assert isinstance(service._profile_vector_store, FakeAuroraVectorStore)
    assert service.profile_indexer is not None
    assert service._base_profile_search_service is not None
    assert service._profile_search_service is not None

    cached = metadata_module.MetadataSnapshotService(config)
    cached._profile_search_service = object()
    assert cached.initialize_profile_search() is True

    errored = metadata_module.MetadataSnapshotService(config)
    errored._profile_search_init_error = "broken"
    assert errored.initialize_profile_search() is False

    missing = metadata_module.MetadataSnapshotService(config)
    missing._metadata_init_error = "metadata missing"
    monkeypatch.setattr(missing, "initialize_metadata_store", lambda: False)
    assert missing.initialize_profile_search() is False
    assert missing._profile_search_init_error == "metadata missing"

    no_handle = metadata_module.MetadataSnapshotService(config)
    monkeypatch.setattr(no_handle, "initialize_metadata_store", lambda: True)
    assert no_handle.initialize_profile_search() is False
    assert no_handle._profile_search_init_error == "metadata store initialized without a handle"

    monkeypatch.setattr(
        "context_graph.runtime.providers.factories.LLMClientFactory.create",
        lambda cfg: (_ for _ in ()).throw(RuntimeError("llm unavailable")),
    )
    failing = metadata_module.MetadataSnapshotService(config)
    failing._metadata = FakePostgresMetadataStore("postgresql://demo")
    monkeypatch.setattr(failing, "initialize_metadata_store", lambda: True)
    assert failing.initialize_profile_search() is False
    assert failing._profile_search_init_error == "llm unavailable"


def test_search_profiles_sync_covers_resync_branches_and_error_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata_module, "InMemoryProfileVectorStore", FakeInMemoryVectorStore)

    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    monkeypatch.setattr(service, "initialize_profile_search", lambda: False)
    service._profile_search_init_error = "profile search unavailable"
    assert service.search_profiles_sync(query="jwt", repo_id="demo") == []

    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    monkeypatch.setattr(service, "initialize_profile_search", lambda: True)
    assert service.search_profiles_sync(query="jwt", repo_id="demo") == []

    in_memory_store = FakeInMemoryVectorStore()
    in_memory_indexer = FakeProfileVectorIndexer(vector_store=in_memory_store, embedding_provider="embed")
    semantic_service = FakeSemanticProvenanceSearchService(profile_search_service=None)
    semantic_service.result = make_profile_result()
    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    monkeypatch.setattr(service, "initialize_profile_search", lambda: True)
    service._metadata = SimpleNamespace(connection=None)
    service._profile_vector_store = in_memory_store
    service._profile_indexer = in_memory_indexer
    service._profile_search_service = semantic_service
    results = service.search_profiles_sync(query="jwt", repo_id="demo")
    assert results[0]["profile_id"] == "profile:1"
    assert in_memory_store.bootstrap_calls == 1
    assert in_memory_indexer.sync_calls == [("demo", service._metadata)]

    aurora_store = FakeAuroraVectorStore()
    aurora_store.needs_resync = True
    base_service = FakeProfileSearchService()
    base_service.search = lambda **kwargs: make_profile_result()
    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    monkeypatch.setattr(service, "initialize_profile_search", lambda: True)
    service._metadata = SimpleNamespace(connection=None)
    service._profile_vector_store = aurora_store
    service._profile_indexer = FakeProfileVectorIndexer(vector_store=aurora_store, embedding_provider="embed")
    service._base_profile_search_service = base_service
    results = service.search_profiles_sync(
        query="jwt",
        repo_id="demo",
        profile_kinds=("semantic_claim_profile",),
    )
    assert results[0]["matched_key_text"] == "matched text"
    assert aurora_store.mark_resynced_calls == 1

    class ExplodingIndexer(FakeProfileVectorIndexer):
        def sync_repo(self, *, repo_id: str, metadata_store) -> None:
            raise RuntimeError("sync failed")

    exploding_store = FakeAuroraVectorStore()
    exploding_store.needs_resync = True
    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    monkeypatch.setattr(service, "initialize_profile_search", lambda: True)
    service._metadata = SimpleNamespace(connection=None)
    service._profile_vector_store = exploding_store
    service._profile_indexer = ExplodingIndexer(vector_store=exploding_store, embedding_provider="embed")
    service._profile_search_service = SimpleNamespace(search=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("search failed")))
    rollback_calls = []
    monkeypatch.setattr(service, "_rollback_profile_search_connections", lambda: rollback_calls.append("rollback"))
    assert service.search_profiles_sync(query="jwt", repo_id="demo") == []
    assert rollback_calls == ["rollback"]


@pytest.mark.parametrize("profile_kinds", [None, ("semantic_claim_profile",)])
def test_search_profiles_sync_matches_profiles_by_id(monkeypatch, profile_kinds) -> None:
    first = make_profile_result().profiles[0]
    second = FakeProfile(
        **{
            **first.__dict__,
            "profile_id": "profile:2",
            "profile_target_id": "claim:2",
            "value_text": "A different claim.",
        }
    )
    result = SimpleNamespace(
        hits=(
            FakeHit(0.99, "stale key", "raw", profile_id="missing"),
            FakeHit(0.91, "first key", "raw", profile_id=first.profile_id),
            FakeHit(0.81, "second key", "raw", profile_id=second.profile_id),
        ),
        profiles=(second, first),
    )
    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    monkeypatch.setattr(service, "initialize_profile_search", lambda: True)
    service._metadata = SimpleNamespace(connection=None)
    search_service = SimpleNamespace(search=lambda **kwargs: result)
    service._profile_search_service = search_service
    service._base_profile_search_service = search_service

    rows = service.search_profiles_sync(query="jwt", repo_id="demo", profile_kinds=profile_kinds)

    assert [
        (row["profile_id"], row["profile_target_id"], row["value_text"], row["score"], row["matched_key_text"])
        for row in rows
    ] == [
        (first.profile_id, first.profile_target_id, first.value_text, 0.91, "first key"),
        (second.profile_id, second.profile_target_id, second.value_text, 0.81, "second key"),
    ]


def test_metadata_snapshot_service_connection_helpers_cover_abort_detection() -> None:
    connection = SimpleNamespace(info=SimpleNamespace(transaction_status=SimpleNamespace(name="INERROR")))
    assert metadata_module.connection_transaction_in_error(connection) is True

    connection = SimpleNamespace(info=SimpleNamespace(transaction_status="psycopg.INERROR"))
    assert metadata_module.connection_transaction_in_error(connection) is True

    connection = SimpleNamespace(info=None, get_transaction_status=lambda: 3)
    assert metadata_module.connection_transaction_in_error(connection) is True

    connection = SimpleNamespace(info=None, get_transaction_status=lambda: (_ for _ in ()).throw(RuntimeError()))
    assert metadata_module.connection_transaction_in_error(connection) is False

    shared_connection = SimpleNamespace(rollback=lambda: None)
    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    service._metadata = SimpleNamespace(connection=shared_connection)
    service._profile_vector_store = SimpleNamespace(connection=shared_connection)
    service._rollback_profile_search_connections()

    rollbacks: list[str] = []

    class RollbackConnection:
        def __init__(self, in_error: bool) -> None:
            self.info = SimpleNamespace(transaction_status=SimpleNamespace(name="INERROR" if in_error else "IDLE"))

        def rollback(self) -> None:
            rollbacks.append("rollback")

    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    service._metadata = SimpleNamespace(connection=RollbackConnection(True))
    service._profile_vector_store = SimpleNamespace(connection=RollbackConnection(False))
    service._rollback_aborted_profile_search_connections()
    assert rollbacks == ["rollback"]


def test_metadata_snapshot_service_covers_remaining_small_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    for env_name in (
        "POSTGRES_DSN",
        "DATABASE_URL",
        "METADATA_SQLITE_PATH",
        "AURORA_ENDPOINT",
        "AURORA_PASSWORD_SECRET_ARN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(env_name, raising=False)
    assert service._build_metadata_dsn() is None

    config = MCPServerConfig()
    config.embedder.dimensions = 256
    monkeypatch.setattr(metadata_module, "PostgresMetadataStore", FakePostgresMetadataStore)
    monkeypatch.setattr(metadata_module, "AuroraProfileVectorStore", FakeAuroraVectorStore)
    monkeypatch.setattr(metadata_module, "InMemoryProfileVectorStore", FakeInMemoryVectorStore)
    monkeypatch.setattr(
        "context_graph.backend.profile_vector_services.ProfileSearchService",
        FakeProfileSearchService,
    )
    monkeypatch.setattr(
        "context_graph.backend.profile_vector_services.ProfileVectorIndexer",
        FakeProfileVectorIndexer,
    )
    monkeypatch.setattr(
        "context_graph.backend.profile_vector_services.SemanticProvenanceSearchService",
        FakeSemanticProvenanceSearchService,
    )
    monkeypatch.setattr("context_graph.query.parser.QueryParser", lambda *, llm_client: ("parser", llm_client))
    monkeypatch.setattr("context_graph.runtime.embeddings.EmbeddingProvider", lambda raw: ("provider", raw))
    monkeypatch.setattr("context_graph.runtime.providers.factories.LLMClientFactory.create", lambda cfg: "llm")
    monkeypatch.setattr("context_graph.runtime.providers.factories.EmbedderFactory.create", lambda cfg: "embedder")

    in_memory = metadata_module.MetadataSnapshotService(config)
    in_memory._metadata = FakeSQLiteMetadataStore("/tmp/demo.sqlite")
    monkeypatch.setattr(in_memory, "initialize_metadata_store", lambda: True)
    assert in_memory.initialize_profile_search() is True
    assert isinstance(in_memory._profile_vector_store, FakeInMemoryVectorStore)

    no_semantic = metadata_module.MetadataSnapshotService(MCPServerConfig())
    monkeypatch.setattr(no_semantic, "initialize_profile_search", lambda: True)
    no_semantic._metadata = SimpleNamespace(connection=None)
    assert no_semantic.search_profiles_sync(query="jwt", repo_id="demo") == []
    assert no_semantic.search_profiles_sync(
        query="jwt",
        repo_id="demo",
        profile_kinds=("semantic_claim_profile",),
    ) == []

    rollback_service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    rollback_service._metadata = SimpleNamespace(connection=None)
    rollback_service._profile_vector_store = SimpleNamespace(
        connection=SimpleNamespace(
            rollback=lambda: (_ for _ in ()).throw(RuntimeError("rollback failed"))
        )
    )
    rollback_service._rollback_profile_search_connections()

    rollback_calls: list[str] = []

    class DuplicateAbortedConnection:
        def __init__(self) -> None:
            self.info = SimpleNamespace(transaction_status=SimpleNamespace(name="INERROR"))

        def rollback(self) -> None:
            rollback_calls.append("rollback")
            raise RuntimeError("rollback failed")

    duplicate_connection = DuplicateAbortedConnection()
    aborted_service = metadata_module.MetadataSnapshotService(MCPServerConfig())
    aborted_service._metadata = SimpleNamespace(connection=duplicate_connection)
    aborted_service._profile_vector_store = SimpleNamespace(connection=duplicate_connection)
    aborted_service._rollback_aborted_profile_search_connections()
    assert rollback_calls == ["rollback"]
