"""Transient deterministic hint enrichment for provenance grounding."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any

from .models import EvidenceCandidate, Observation

_KNOWN_FILE_EXTENSIONS = frozenset(
    {
        "c",
        "cc",
        "cfg",
        "cpp",
        "cs",
        "go",
        "h",
        "hpp",
        "ini",
        "java",
        "js",
        "json",
        "jsx",
        "kt",
        "md",
        "php",
        "py",
        "rb",
        "rs",
        "scala",
        "sh",
        "sql",
        "swift",
        "toml",
        "ts",
        "tsx",
        "txt",
        "yaml",
        "yml",
    }
)
_BACKTICK_PATTERN = re.compile(r"`([^`\n]+)`")
_PATH_PATTERN = re.compile(r"(?<![\w/.-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]+")
_DOTTED_IDENTIFIER_PATTERN = re.compile(r"(?<![\w.])(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*")
_CALL_PATTERN = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)\(\)")
_KEYWORD_SYMBOL_PATTERN = re.compile(
    r"\b(?:class|method|function|symbol|module|file)\s+([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"
)
_BASENAME_PATTERN = re.compile(r"(?<![\w/.-])[A-Za-z0-9_.-]+\.[A-Za-z0-9]+")


class HintKind(str, Enum):
    NODE_ID = "node_id"
    QUALIFIED_NAME = "qualified_name"
    FILE_PATH = "file_path"
    FILE_BASENAME = "file_basename"
    QUALIFIED_NAME_SUFFIX = "qualified_name_suffix"
    SYMBOL_NAME = "symbol_name"


class HintSource(str, Enum):
    LLM_CLAIM_HINT = "llm_claim_hint"
    LLM_EVIDENCE_HINT = "llm_evidence_hint"
    CLAIM_TEXT = "claim_text"
    EVIDENCE_QUOTE_TEXT = "evidence_quote_text"
    EVIDENCE_SUMMARY_TEXT = "evidence_summary_text"


@dataclass(frozen=True, slots=True)
class EnrichedAboutHint:
    value: str
    hint_kind: HintKind
    source: HintSource
    raw_value: str


def collect_grounding_hints(
    *,
    observation: Observation,
    claim: Any | None = None,
    evidence: EvidenceCandidate | None = None,
) -> tuple[EnrichedAboutHint, ...]:
    del observation  # grounding is evidence-centric in v1
    collected: list[EnrichedAboutHint] = []
    collected.extend(_enriched_from_values(getattr(claim, "about_hints", ()), HintSource.LLM_CLAIM_HINT))
    collected.extend(_enriched_from_values(getattr(evidence, "about_hints", ()), HintSource.LLM_EVIDENCE_HINT))
    if claim is not None:
        collected.extend(_extract_hints_from_text(getattr(claim, "claim_text", ""), HintSource.CLAIM_TEXT))
    if evidence is not None:
        collected.extend(_extract_hints_from_text(evidence.quote_text, HintSource.EVIDENCE_QUOTE_TEXT))
        collected.extend(_extract_hints_from_text(evidence.summary_text, HintSource.EVIDENCE_SUMMARY_TEXT))
    deduped: list[EnrichedAboutHint] = []
    seen: set[str] = set()
    for hint in collected:
        if hint.value in seen:
            continue
        seen.add(hint.value)
        deduped.append(hint)
    return tuple(deduped)


def infer_hint_kind(value: str) -> HintKind | None:
    normalized = normalize_hint_value(value)
    if not normalized:
        return None
    if _looks_like_node_id(normalized):
        return HintKind.NODE_ID
    if _looks_like_file_path(normalized):
        return HintKind.FILE_PATH
    if _looks_like_file_basename(normalized):
        return HintKind.FILE_BASENAME
    if _looks_like_dotted_identifier(normalized):
        if normalized.count(".") >= 2 or normalized[0].islower():
            return HintKind.QUALIFIED_NAME
        return HintKind.QUALIFIED_NAME_SUFFIX
    if _looks_like_symbol_name(normalized):
        return HintKind.SYMBOL_NAME
    return None


def normalize_hint_value(value: str) -> str:
    normalized = " ".join(str(value).split()).strip()
    if not normalized:
        return ""
    while normalized and normalized[0] in "`'\"([{<":
        normalized = normalized[1:].strip()
    while normalized and normalized[-1] in "`'\"),.:;]}>":
        normalized = normalized[:-1].strip()
    if normalized.endswith("()"):
        normalized = normalized[:-2].strip()
    while normalized and normalized[-1] in ",.:;":
        normalized = normalized[:-1].strip()
    return normalized


def _enriched_from_values(values: Any, source: HintSource) -> list[EnrichedAboutHint]:
    enriched: list[EnrichedAboutHint] = []
    for raw in values or ():
        normalized = normalize_hint_value(str(raw))
        kind = infer_hint_kind(normalized)
        if kind is None:
            continue
        enriched.append(
            EnrichedAboutHint(
                value=normalized,
                hint_kind=kind,
                source=source,
                raw_value=str(raw),
            )
        )
    return enriched


def _extract_hints_from_text(text: str, source: HintSource) -> list[EnrichedAboutHint]:
    raw_matches: list[str] = []
    if not text:
        return []
    whole_text_candidate = normalize_hint_value(text)
    if infer_hint_kind(whole_text_candidate) is not None:
        raw_matches.append(whole_text_candidate)
    raw_matches.extend(match.group(1) for match in _BACKTICK_PATTERN.finditer(text))
    raw_matches.extend(match.group(0) for match in _PATH_PATTERN.finditer(text))
    raw_matches.extend(match.group(0) for match in _DOTTED_IDENTIFIER_PATTERN.finditer(text))
    raw_matches.extend(match.group(1) for match in _CALL_PATTERN.finditer(text))
    raw_matches.extend(match.group(1) for match in _KEYWORD_SYMBOL_PATTERN.finditer(text))
    raw_matches.extend(match.group(0) for match in _BASENAME_PATTERN.finditer(text))
    return _enriched_from_values(raw_matches, source)


def _looks_like_node_id(value: str) -> bool:
    prefix, _, remainder = value.partition(":")
    return bool(remainder) and bool(prefix) and prefix.replace("_", "").isalpha()


def _looks_like_file_path(value: str) -> bool:
    if "/" not in value:
        return False
    return _has_known_extension(value.rsplit("/", 1)[-1])


def _looks_like_file_basename(value: str) -> bool:
    if "/" in value:
        return False
    return _has_known_extension(value)


def _has_known_extension(value: str) -> bool:
    if "." not in value:
        return False
    extension = value.rsplit(".", 1)[-1].lower()
    return extension in _KNOWN_FILE_EXTENSIONS


def _looks_like_dotted_identifier(value: str) -> bool:
    if "." not in value:
        return False
    segments = value.split(".")
    return all(segment and segment.replace("_", "").isalnum() and segment[0].isalpha() for segment in segments)


def _looks_like_symbol_name(value: str) -> bool:
    return bool(value) and value.replace("_", "").isalnum() and value[0].isalpha()
