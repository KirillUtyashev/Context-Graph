from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from context_graph.ingestion.driver import IngestionDriver
from context_graph.ingestion.models import HostedIndexRequest
from context_graph.metadata import InMemoryMetadataStore
from context_graph.storage.backend.metadata import (
    InMemoryMetadataConfig,
    PostgresMetadataConfig,
    SQLiteMetadataConfig,
)
from context_graph.storage.backend.metadata.factory import create_metadata_store
from context_graph.storage.backend.profile_vectors import (
    InMemoryProfileVectorConfig,
    PgVectorProfileVectorConfig,
)
from context_graph.storage.backend.profile_vectors.factory import (
    create_profile_vector_store,
    create_profile_vector_store_from_metadata,
)


def test_metadata_store_factory_dispatches_all_supported_configs() -> None:
    in_memory = create_metadata_store(InMemoryMetadataConfig())
    assert isinstance(in_memory, InMemoryMetadataStore)

    sqlite_store = create_metadata_store(SQLiteMetadataConfig(path=":memory:"))
    assert sqlite_store.connection is not None

    with patch(
        "context_graph.storage.backend.metadata.factory.PostgresMetadataStore.from_dsn",
        return_value="pg-store",
    ) as from_dsn:
        store = create_metadata_store(PostgresMetadataConfig(dsn="postgresql://example"))

    assert store == "pg-store"
    from_dsn.assert_called_once_with("postgresql://example")

    with pytest.raises(TypeError, match="unsupported metadata config"):
        create_metadata_store(object())  # type: ignore[arg-type]


def test_profile_vector_factory_dispatches_connection_and_metadata_paths() -> None:
    in_memory = create_profile_vector_store(InMemoryProfileVectorConfig())
    assert in_memory is not None

    with patch(
        "context_graph.storage.backend.profile_vectors.factory.PgVectorProfileVectorStore",
        return_value="direct-store",
    ) as pg_store:
        store = create_profile_vector_store(
            PgVectorProfileVectorConfig(embedding_dim=256),
            connection=object(),
        )
    assert store == "direct-store"
    pg_store.assert_called_once()

    with patch(
        "context_graph.storage.backend.profile_vectors.factory.create_profile_vector_store_from_metadata",
        return_value="metadata-store",
    ) as from_metadata:
        store = create_profile_vector_store(
            PgVectorProfileVectorConfig(embedding_dim=384),
            metadata_store=SimpleNamespace(),
        )
    assert store == "metadata-store"
    from_metadata.assert_called_once()

    with pytest.raises(RuntimeError, match="requires either a metadata_store or direct connection"):
        create_profile_vector_store(PgVectorProfileVectorConfig(embedding_dim=128))

    with pytest.raises(TypeError, match="unsupported profile vector config"):
        create_profile_vector_store(object())  # type: ignore[arg-type]


def test_profile_vector_factory_from_metadata_requires_postgres_store() -> None:
    with pytest.raises(RuntimeError, match="requires a PostgreSQL-compatible metadata store"):
        create_profile_vector_store_from_metadata(InMemoryMetadataStore())

    fake_postgres = SimpleNamespace(connection="pg-conn")
    with patch(
        "context_graph.storage.backend.profile_vectors.factory.PostgresMetadataStore",
        new=type("FakePostgresMetadataStore", (), {}),
    ), patch(
        "context_graph.storage.backend.profile_vectors.factory.PgVectorProfileVectorStore",
        return_value="pgvector-store",
    ) as pg_store:
        fake_postgres = (
            __import__("context_graph.storage.backend.profile_vectors.factory", fromlist=["PostgresMetadataStore"])
            .PostgresMetadataStore()
        )
        fake_postgres.connection = "pg-conn"
        result = create_profile_vector_store_from_metadata(fake_postgres, embedding_dim=512)

    assert result == "pgvector-store"
    pg_store.assert_called_once_with("pg-conn", embedding_dim=512)


def test_ingestion_driver_builds_default_dependencies_and_delegates_calls() -> None:
    metadata_store = InMemoryMetadataStore()
    driver = IngestionDriver(metadata_store=metadata_store, backend_driver=None)

    assert driver.metadata_store is metadata_store
    assert driver.backend_driver is None
    assert driver.snapshot_producer is not None
    assert driver.git_metadata is not None
    assert driver.repo_cache is not None
    assert driver.planner is not None
    assert driver.repo_resolver is not None

    latest_result = object()
    commit_result = object()
    hosted_result = object()

    fake_commit_service = SimpleNamespace(
        apply_latest_commit=lambda *args, **kwargs: latest_result,
        apply_commit=lambda *args, **kwargs: commit_result,
    )
    fake_hosted_service = SimpleNamespace(index=lambda request: hosted_result)

    driver._make_commit_indexing_service = lambda: fake_commit_service  # type: ignore[method-assign]
    driver._make_hosted_indexing_service = lambda: fake_hosted_service  # type: ignore[method-assign]

    assert driver.apply_latest_commit("/tmp/repo", repo_id="demo", branch_hint="main", execute=True) is latest_result
    assert (
        driver.apply_commit(
            "/tmp/repo",
            commit_sha="abc123",
            repo_id="demo",
            branch_hint="main",
            execute=True,
        )
        is commit_result
    )
    assert driver.index_hosted(HostedIndexRequest(github_repo="openai/context-graph")) is hosted_result


def test_ingestion_driver_factory_helpers_wire_services_with_shared_dependencies() -> None:
    metadata_store = InMemoryMetadataStore()
    snapshot_producer = object()
    git_metadata = object()
    github_api = object()
    repo_cache = object()
    llm_client = object()
    backend_driver = object()

    with patch("context_graph.ingestion.driver.CommitIndexingService", return_value="commit-service") as commit_cls:
        driver = IngestionDriver(
            metadata_store=metadata_store,
            backend_driver=backend_driver,
            snapshot_producer=snapshot_producer,  # type: ignore[arg-type]
            git_metadata=git_metadata,  # type: ignore[arg-type]
            github_api=github_api,  # type: ignore[arg-type]
            repo_cache=repo_cache,  # type: ignore[arg-type]
            llm_client=llm_client,
        )
        service = driver._make_commit_indexing_service()

    assert service == "commit-service"
    commit_cls.assert_called_once_with(
        metadata_store=metadata_store,
        backend_driver=backend_driver,
        snapshot_producer=snapshot_producer,
        git_metadata=git_metadata,
        planner=driver.planner,
        llm_client=llm_client,
    )

    with patch("context_graph.ingestion.driver.HostedIndexingService", return_value="hosted-service") as hosted_cls:
        service = driver._make_hosted_indexing_service()

    assert service == "hosted-service"
    hosted_cls.assert_called_once_with(
        metadata_store=metadata_store,
        backend_driver=backend_driver,
        snapshot_producer=snapshot_producer,
        git_metadata=git_metadata,
        github_api=github_api,
        repo_cache=repo_cache,
        repo_resolver=driver.repo_resolver,
        planner=driver.planner,
        llm_client=llm_client,
    )
