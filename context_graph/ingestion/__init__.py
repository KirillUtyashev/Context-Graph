"""Git-aware codebase ingestion, snapshot building, and diffing."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "CodebaseMemorySnapshotProducer",
    "CommitIndexResult",
    "DeterministicHostedIndexResult",
    "HostedIndexRequest",
    "IngestionDriver",
    "RepoCacheManager",
    "TemporalGraphWriter",
    "build_snapshot_from_graph_export",
    "diff_snapshots",
]

_EXPORTS = {
    "CodebaseMemorySnapshotProducer": (".snapshot_builder", "CodebaseMemorySnapshotProducer"),
    "CommitIndexResult": (".models", "CommitIndexResult"),
    "DeterministicHostedIndexResult": (".models", "DeterministicHostedIndexResult"),
    "HostedIndexRequest": (".models", "HostedIndexRequest"),
    "IngestionDriver": (".driver", "IngestionDriver"),
    "RepoCacheManager": (".repo_cache", "RepoCacheManager"),
    "TemporalGraphWriter": (".temporal", "TemporalGraphWriter"),
    "build_snapshot_from_graph_export": (".snapshot_builder", "build_snapshot_from_graph_export"),
    "diff_snapshots": (".diffing", "diff_snapshots"),
}


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = _EXPORTS[name]
    module = import_module(module_name, __name__)
    return getattr(module, attr_name)
