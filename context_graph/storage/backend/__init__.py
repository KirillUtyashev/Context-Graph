"""Public entrypoints for the additive backend package."""

from .driver import BackendDriver, BackendSession
from .errors import (
    BackendConfigurationError,
    BackendFeatureNotImplementedError,
    UnsupportedCapabilityError,
)
from .factory import create_driver
from .metadata import (
    AuroraMetadataStore,
    InMemoryMetadataConfig,
    InMemoryMetadataStore,
    MetadataStore,
    PostgresMetadataConfig,
    PostgresMetadataStore,
    RepoIndexState,
    RepoSourceState,
    SQLiteMetadataConfig,
    SQLiteMetadataStore,
    create_metadata_store,
)
from .maintenance import (
    MaintenanceOperations,
    RepoResetResult,
    ScopeResetRequest,
    ScopeResetResult,
)
from .mutations import (
    EdgeRecord,
    merge_mutation_plans,
    MutationApplyResult,
    MutationOperations,
    MutationPlan,
    NodeRecord,
    SearchDocumentRecord,
)
from .neo4j.config import Neo4jConfig
from .neptune.config import NeptuneConfig
from .profile_vectors import (
    AuroraProfileVectorStore,
    InMemoryProfileVectorConfig,
    InMemoryProfileVectorStore,
    PgVectorProfileVectorConfig,
    PgVectorProfileVectorStore,
    ProfileKeyRecord,
    ProfileVectorHit,
    ProfileVectorStore,
    create_profile_vector_store,
    create_profile_vector_store_from_metadata,
)
from .query_executor import QueryExecutor, Transaction
from .search import SearchOperations
from .sync_bridge import BackendSyncBridge
from .traversal import TraversalOperations
from .types import (
    BackendCapabilities,
    BackendProvider,
    GraphRecordRef,
    RecordKind,
    SearchHit,
    SearchMode,
    SearchQuery,
    TraversalHit,
    TraversalQuery,
)

__all__ = [
    "AuroraMetadataStore",
    "AuroraProfileVectorStore",
    "BackendCapabilities",
    "BackendConfigurationError",
    "BackendDriver",
    "BackendFeatureNotImplementedError",
    "BackendProvider",
    "BackendSession",
    "GraphRecordRef",
    "InMemoryMetadataConfig",
    "InMemoryMetadataStore",
    "InMemoryProfileVectorConfig",
    "InMemoryProfileVectorStore",
    "MetadataStore",
    "MaintenanceOperations",
    "MutationApplyResult",
    "MutationOperations",
    "MutationPlan",
    "merge_mutation_plans",
    "Neo4jConfig",
    "NeptuneConfig",
    "NodeRecord",
    "PgVectorProfileVectorConfig",
    "PgVectorProfileVectorStore",
    "PostgresMetadataConfig",
    "PostgresMetadataStore",
    "ProfileKeyRecord",
    "ProfileVectorHit",
    "ProfileVectorStore",
    "QueryExecutor",
    "RecordKind",
    "RepoIndexState",
    "RepoResetResult",
    "RepoSourceState",
    "ScopeResetRequest",
    "ScopeResetResult",
    "SearchDocumentRecord",
    "SearchHit",
    "SearchMode",
    "SearchOperations",
    "SearchQuery",
    "BackendSyncBridge",
    "SQLiteMetadataConfig",
    "SQLiteMetadataStore",
    "Transaction",
    "TraversalHit",
    "TraversalOperations",
    "TraversalQuery",
    "UnsupportedCapabilityError",
    "EdgeRecord",
    "create_driver",
    "create_metadata_store",
    "create_profile_vector_store",
    "create_profile_vector_store_from_metadata",
]
