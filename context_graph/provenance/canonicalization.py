"""Helpers for future canonical claim promotion."""

from __future__ import annotations

import re
from typing import Sequence

_WHITESPACE_RE = re.compile(r"\s+")


def canonicalize_claim_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value.strip()).lower()


def canonical_claim_key(
    *,
    claim_kind: str,
    claim_text: str,
    about_record_ids: Sequence[str] = (),
) -> str:
    normalized_about = ",".join(sorted(dict.fromkeys(about_record_ids)))
    if not normalized_about:
        return f"{claim_kind}:{canonicalize_claim_text(claim_text)}"
    return f"{claim_kind}:{canonicalize_claim_text(claim_text)}|about:{normalized_about}"
