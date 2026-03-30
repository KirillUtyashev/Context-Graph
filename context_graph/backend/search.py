"""Generic search interface and shared validation helpers."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

from .types import GraphRecordRef, SearchHit, SearchMode, SearchQuery

_SAFE_FILTER_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SearchOperations(ABC):
    """Provider-neutral search entrypoints.

    Search backends are responsible for ranking and returning lightweight
    references. Higher layers can then call :meth:`fetch` to hydrate the raw
    graph records behind those references.
    """

    @abstractmethod
    async def search(self, query: SearchQuery) -> list[SearchHit]:
        """Search indexed graph records and return scored lightweight hits."""

    @abstractmethod
    async def fetch(self, refs: list[GraphRecordRef]) -> list[dict[str, Any]]:
        """Hydrate raw graph records for previously returned references."""


def validate_extra_filters(filters: dict[str, object]) -> None:
    """Reject filter keys that are unsafe to embed into backend queries."""

    for key in filters:
        if not _SAFE_FILTER_KEY.match(key):
            raise ValueError(f"invalid filter key: {key!r}")


def ensure_lexical_mode(query: SearchQuery) -> None:
    """Ensure the request is a lexical query.

    V1 intentionally keeps vector and hybrid modes in the public type system but
    does not implement them yet.
    """

    if query.mode is not SearchMode.LEXICAL:
        raise ValueError("only lexical mode may be handled by lexical helpers")
