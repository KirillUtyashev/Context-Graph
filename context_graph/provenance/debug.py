"""Lightweight provenance debug tracing — logs structured events at DEBUG level."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def emit_provenance_debug(event: str, payload: dict[str, Any]) -> None:
    """Emit a structured debug event for provenance pipeline tracing."""
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("provenance_debug event=%s %s", event, json.dumps(payload, default=str))
