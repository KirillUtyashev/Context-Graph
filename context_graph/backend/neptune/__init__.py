"""Neptune backend package exports."""

from __future__ import annotations

from importlib import import_module

__all__ = ["NeptuneBackendDriver", "NeptuneConfig"]


def __getattr__(name: str):
    if name == "NeptuneConfig":
        return getattr(import_module(".config", __name__), name)
    if name == "NeptuneBackendDriver":
        return getattr(import_module(".driver", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
