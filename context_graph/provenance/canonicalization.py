"""Helpers for future canonical claim promotion."""

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")


def canonicalize_claim_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value.strip()).lower()


def canonical_claim_key(*, claim_kind: str, claim_text: str) -> str:
    return f"{claim_kind}:{canonicalize_claim_text(claim_text)}"
