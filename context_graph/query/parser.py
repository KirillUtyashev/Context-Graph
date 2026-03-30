from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re
import sys
import threading
from typing import Any, Sequence

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    BaseModel = None
    Field = None


_BACKTICK_RE = re.compile(r"`([^`]+)`")
_QUOTED_RE = re.compile(r'"([^"]{3,})"')
_ISSUE_RE = re.compile(r"#(\d+)")
_COMMIT_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
_ROUTE_RE = re.compile(r"(?<!\w)(/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=-]+)")
_PATH_RE = re.compile(r"\b(?:[\w.-]+/)+[\w./-]+\b")
_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.:]*\b")
_FILE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".sh",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".tf",
}
_INTENT_KEYWORDS = {
    "why": "why",
    "impact": "impact",
    "rollback": "rollback",
    "constraint": "constraint",
    "constraints": "constraint",
    "migrate": "migration",
    "migration": "migration",
    "history": "history",
}
_ALLOWED_INTENTS = {"why", "impact", "rollback", "constraint", "migration", "history"}


@dataclass(slots=True)
class PromptMessage:
    role: str
    content: str

    def model_dump(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class QueryAnchor:
    kind: str
    value: str


@dataclass(frozen=True, slots=True)
class QueryParseResult:
    raw_query: str
    anchors: tuple[QueryAnchor, ...]
    high_level_keywords: tuple[str, ...]
    intent_tags: tuple[str, ...]


if BaseModel is not None:

    class ParsedHighLevelKeywords(BaseModel):
        high_level_keywords: list[str] = Field(default_factory=list)

else:

    class ParsedHighLevelKeywords:
        @classmethod
        def model_json_schema(cls) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "high_level_keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["high_level_keywords"],
            }


class QueryParser:
    def __init__(self, *, llm_client: Any | None = None) -> None:
        self.llm_client = llm_client

    def parse(self, query: str, *, llm_enabled: bool = True) -> QueryParseResult:
        anchors = _extract_deterministic_anchors(query)
        intent_tags = list(_deterministic_intent_tags(query))
        keywords: list[str] = []

        if llm_enabled and self.llm_client is not None:
            llm_keywords = self._extract_high_level_keywords(query)
            keywords.extend(llm_keywords)

        keywords = _dedupe_texts(keywords)
        intent_tags = [tag for tag in _dedupe_texts(intent_tags) if tag in _ALLOWED_INTENTS]

        print(
            "[context_graph.debug] query_parser.parse "
            f"query={query!r} "
            f"anchors={[{'kind': anchor.kind, 'value': anchor.value} for anchor in anchors]!r} "
            f"high_level_keywords={keywords!r} "
            f"intent_tags={intent_tags!r}",
            file=sys.stderr,
            flush=True,
        )

        return QueryParseResult(
            raw_query=query,
            anchors=anchors,
            high_level_keywords=tuple(keywords),
            intent_tags=tuple(intent_tags),
        )

    def _extract_high_level_keywords(self, query: str) -> list[str]:
        response = _run_async(
            self.llm_client.generate_response(
                _query_prompt(query),
                response_model=ParsedHighLevelKeywords,
                max_tokens=2048,
                group_id="query-parser",
                prompt_name="context_graph.query_parser.parse",
            )
        )
        payload = _normalize_query_response(response)
        return [
            keyword
            for keyword in payload.get("high_level_keywords", [])
            if isinstance(keyword, str) and keyword.strip()
        ]


def _extract_deterministic_anchors(query: str) -> tuple[QueryAnchor, ...]:
    anchors: list[QueryAnchor] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str) -> None:
        normalized = value.strip()
        if not normalized:
            return
        key = (kind, normalized)
        if key in seen:
            return
        seen.add(key)
        anchors.append(QueryAnchor(kind=kind, value=normalized))

    for value in _BACKTICK_RE.findall(query):
        add("quoted", value)
    for value in _QUOTED_RE.findall(query):
        add("quoted", value)
    for number in _ISSUE_RE.findall(query):
        add("artifact_number", f"#{number}")
    for sha in _COMMIT_RE.findall(query):
        add("commit_sha", sha)
    for route in _ROUTE_RE.findall(query):
        add("route", route)
    for path in _PATH_RE.findall(query):
        add("file_path", path)

    for token in _IDENTIFIER_RE.findall(query):
        if token.startswith("#"):
            continue
        if "/" in token and _looks_like_file_path(token):
            add("file_path", token)
            continue
        if "." in token or "_" in token or _looks_like_symbol(token):
            add("symbol", token)

    return tuple(anchors)


def _deterministic_intent_tags(query: str) -> tuple[str, ...]:
    lowered = query.lower()
    matches = [
        intent
        for keyword, intent in _INTENT_KEYWORDS.items()
        if re.search(rf"\b{re.escape(keyword)}\b", lowered)
    ]
    return tuple(_dedupe_texts(matches))


def _looks_like_symbol(token: str) -> bool:
    if token.isupper():
        return False
    return any(
        [
            "." in token,
            "_" in token,
            re.search(r"[a-z][A-Z]", token) is not None,
            re.search(r"[A-Z][a-z]+[A-Z][A-Za-z0-9]*", token) is not None,
        ]
    )


def _looks_like_file_path(token: str) -> bool:
    for suffix in _FILE_EXTENSIONS:
        if token.lower().endswith(suffix):
            return True
    return "/" in token


def _query_prompt(query: str) -> list[PromptMessage]:
    return [
        PromptMessage(
            role="system",
            content=(
                "---Role---\n"
                "You are a helpful assistant tasked with identifying high-level keywords in the user's query.\n\n"
                "---Goal---\n"
                "Given the query, list only high-level keywords.\n"
                "High-level keywords focus on overarching concepts or themes.\n"
                "Do not return low-level keywords, exact code anchors, file paths, commit SHAs, issue numbers, or other concrete entities unless they are truly high-level themes."
            ),
        ),
        PromptMessage(
            role="user",
            content=(
                "Output the keywords in JSON format.\n"
                "The JSON must have exactly one key: \"high_level_keywords\".\n"
                "\"high_level_keywords\" should contain overarching concepts or themes only.\n\n"
                "Examples:\n"
                "Query: How does international trade influence global economic stability?\n"
                "Output: {\"high_level_keywords\": [\"International trade\", \"Global economic stability\", \"Economic impact\"]}\n\n"
                "Query: What are the environmental consequences of deforestation on biodiversity?\n"
                "Output: {\"high_level_keywords\": [\"Environmental consequences\", \"Deforestation\", \"Biodiversity loss\"]}\n\n"
                "Query: What is the role of education in reducing poverty?\n"
                "Output: {\"high_level_keywords\": [\"Education\", \"Poverty reduction\", \"Socioeconomic development\"]}\n\n"
                f"Real Data:\nQuery: {query}\nOutput:"
            ),
        ),
    ]


def _normalize_query_response(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return dict(response)
    if hasattr(response, "model_dump"):
        return dict(response.model_dump())
    return {"high_level_keywords": []}


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: list[BaseException] = []

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive handoff
            error.append(exc)

    thread = threading.Thread(target=_runner, daemon=True, name="context-graph-query-parser")
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result.get("value")


def _dedupe_texts(values: Sequence[str]) -> list[str]:
    deduped: dict[str, str] = {}
    for value in values:
        normalized = " ".join(value.strip().split())
        if not normalized:
            continue
        deduped.setdefault(normalized.casefold(), normalized)
    return list(deduped.values())


__all__ = [
    "PromptMessage",
    "QueryAnchor",
    "QueryParseResult",
    "QueryParser",
]
