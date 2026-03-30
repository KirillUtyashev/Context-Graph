"""Shared helpers for resolving CLI arguments and environment variables."""

from __future__ import annotations

import os
from typing import Any


def _arg_or_env(args: Any | None, attr: str, env_name: str) -> str | None:
    """Return *attr* from *args* if set, else fall back to the env var *env_name*.

    Uses the stricter ``embeddings.py`` semantics: an argument value is accepted
    only when it is not ``None`` **and** its string representation is truthy.
    """
    if args is not None:
        value = getattr(args, attr, None)
        if value is not None and str(value):
            return str(value)
    value = os.getenv(env_name)
    return value if value else None
