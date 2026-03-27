"""Factory helpers for constructing concrete backend drivers."""

from __future__ import annotations

from .driver import BackendDriver
from .neo4j.config import Neo4jConfig
from .neptune.config import NeptuneConfig


def create_driver(config: Neo4jConfig | NeptuneConfig) -> BackendDriver:
    """Create the appropriate backend driver for the supplied config object."""

    if isinstance(config, Neo4jConfig):
        from .neo4j.driver import Neo4jBackendDriver

        return Neo4jBackendDriver(config)
    if isinstance(config, NeptuneConfig):
        from .neptune.driver import NeptuneBackendDriver

        return NeptuneBackendDriver(config)
    raise TypeError(f"unsupported backend config: {type(config)!r}")
