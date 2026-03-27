"""Configuration objects for the Neptune backend implementation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NeptuneConfig:
    """Connection settings for a Neptune backend."""

    host: str
    aoss_host: str | None = None
    port: int = 8182
    aoss_port: int = 443
