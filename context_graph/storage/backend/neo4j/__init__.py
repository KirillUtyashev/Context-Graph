"""Neo4j backend package exports."""

from __future__ import annotations

from importlib import import_module

__all__ = ["Neo4jBackendDriver", "Neo4jConfig"]


def __getattr__(name: str):  # pragma: no cover
    if name == "Neo4jConfig":
        return getattr(import_module(".config", __name__), name)
    if name == "Neo4jBackendDriver":
        return getattr(import_module(".driver", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
