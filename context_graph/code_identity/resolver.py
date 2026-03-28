from __future__ import annotations

from collections import defaultdict
from os.path import basename
from pathlib import PurePosixPath
import re
from typing import Iterable, Sequence

from context_graph.models import CanonicalNode, GraphSnapshot

from .canonicalizer import CodeIdentityCanonicalizer, DEFAULT_CODE_IDENTITY_CANONICALIZER

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


class SnapshotCodeResolver:
    """Resolve deterministic code identities against one canonical snapshot."""

    def __init__(
        self,
        snapshot: GraphSnapshot,
        *,
        extra_nodes: Sequence[CanonicalNode] = (),
        canonicalizer: CodeIdentityCanonicalizer | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.canonicalizer = canonicalizer or DEFAULT_CODE_IDENTITY_CANONICALIZER

        available_nodes: dict[str, CanonicalNode] = {
            node.node_id: node
            for node in snapshot.nodes
            if node.kind != "Project"
        }
        for node in extra_nodes:
            if node.kind != "Project":
                available_nodes.setdefault(node.node_id, node)

        self.nodes = tuple(sorted(available_nodes.values(), key=lambda node: node.node_id))
        self._by_node_id = {node.node_id: node for node in self.nodes}
        self._by_qualified_name = {
            node.qualified_name: node
            for node in self.nodes
            if node.qualified_name
        }

        self._file_nodes = tuple(
            node for node in self.nodes if node.kind == "File" and node.file_path
        )
        self._folder_nodes = tuple(node for node in self.nodes if node.kind == "Folder")
        self._semantic_nodes = tuple(
            node for node in self.nodes if node.kind not in {"File", "Folder"}
        )

        self._files_by_path: dict[str, list[CanonicalNode]] = defaultdict(list)
        for node in self._file_nodes:
            normalized_path = self.canonicalizer.normalize_rel_path(node.file_path or "")
            if normalized_path:
                self._files_by_path[normalized_path].append(node)

        self._folders_by_path: dict[str, list[CanonicalNode]] = defaultdict(list)
        for node in self._folder_nodes:
            for folder_path in self._folder_path_candidates(node):
                self._folders_by_path[folder_path].append(node)

        self._files_by_basename: dict[str, list[CanonicalNode]] = defaultdict(list)
        for node in self._file_nodes:
            if node.file_path:
                self._files_by_basename[basename(node.file_path)].append(node)

        self._symbols_by_name: dict[str, list[CanonicalNode]] = defaultdict(list)
        for node in self._semantic_nodes:
            self._symbols_by_name[node.name].append(node)

    def resolve_node_id(self, node_id: str) -> CanonicalNode | None:
        return self._by_node_id.get(node_id)

    def resolve_qualified_name(self, qualified_name: str) -> CanonicalNode | None:
        return self._by_qualified_name.get(qualified_name)

    def resolve_file_path(self, file_path: str) -> CanonicalNode | None:
        normalized_path = self.canonicalizer.normalize_rel_path(file_path)
        return self._resolve_unique(self._files_by_path.get(normalized_path, ()))

    def resolve_folder_path(self, folder_path: str) -> CanonicalNode | None:
        normalized_path = self.canonicalizer.normalize_rel_path(folder_path)
        return self._resolve_unique(self._folders_by_path.get(normalized_path, ()))

    def resolve_file_basename(self, file_name: str) -> CanonicalNode | None:
        return self._resolve_unique(self._files_by_basename.get(file_name, ()))

    def resolve_qualified_name_suffix(self, suffix: str) -> CanonicalNode | None:
        matches = [
            node
            for node in self._semantic_nodes
            if node.qualified_name
            and (
                node.qualified_name == suffix
                or node.qualified_name.endswith(f".{suffix}")
            )
        ]
        return self._resolve_unique(matches)

    def resolve_symbol_name(self, symbol_name: str) -> CanonicalNode | None:
        return self._resolve_unique(self._symbols_by_name.get(symbol_name, ()))

    def resolve_about_hint(
        self,
        hint: str | tuple[str, str | None],
    ) -> CanonicalNode | None:
        if isinstance(hint, tuple):
            raw_value, hint_kind = hint
        else:
            raw_value, hint_kind = hint, None
        normalized_hint = _normalize_hint_value(raw_value)
        if not normalized_hint:
            return None
        resolved_kind = hint_kind or _infer_hint_kind(normalized_hint)

        if resolved_kind == "node_id":
            return self.resolve_node_id(normalized_hint)
        if resolved_kind == "file_path":
            return self.resolve_file_path(normalized_hint)
        if resolved_kind == "file_basename":
            return self.resolve_file_basename(normalized_hint)
        if resolved_kind == "qualified_name":
            return self.resolve_qualified_name(normalized_hint)
        if resolved_kind == "qualified_name_suffix":
            return self.resolve_qualified_name_suffix(normalized_hint)
        if resolved_kind == "symbol_name":
            return self.resolve_symbol_name(normalized_hint)
        return self.resolve_qualified_name(normalized_hint) or self.resolve_node_id(normalized_hint)

    def resolve_about_hints(
        self,
        hints: Iterable[str | tuple[str, str | None]],
    ) -> tuple[CanonicalNode, ...]:
        resolved: list[CanonicalNode] = []
        seen: set[str] = set()
        for hint in hints:
            node = self.resolve_about_hint(hint)
            if node is None or node.node_id in seen:
                continue
            seen.add(node.node_id)
            resolved.append(node)
        return tuple(resolved)

    def resolve_about_record_id(self, hint: str | tuple[str, str | None]) -> str | None:
        node = self.resolve_about_hint(hint)
        if node is None:
            return None
        return f"{node.node_id}@{self.snapshot.revision_id}"

    def resolve_about_record_ids(
        self,
        hints: Iterable[str | tuple[str, str | None]],
    ) -> tuple[str, ...]:
        return tuple(
            f"{node.node_id}@{self.snapshot.revision_id}"
            for node in self.resolve_about_hints(hints)
        )

    def resolve_changed_files(self, changed_files: Sequence[str]) -> tuple[CanonicalNode, ...]:
        touched_nodes: dict[str, CanonicalNode] = {}
        for raw_path in changed_files:
            normalized_path = self.canonicalizer.normalize_rel_path(raw_path)
            if not normalized_path:
                continue

            file_node = self.resolve_file_path(normalized_path)
            if file_node is not None:
                touched_nodes.setdefault(file_node.node_id, file_node)

            for ancestor_path in _ancestor_folder_paths(normalized_path, self.canonicalizer):
                folder_node = self.resolve_folder_path(ancestor_path)
                if folder_node is not None:
                    touched_nodes.setdefault(folder_node.node_id, folder_node)

        return tuple(sorted(touched_nodes.values(), key=lambda node: node.node_id))

    def _folder_path_candidates(self, node: CanonicalNode) -> tuple[str, ...]:
        candidates: list[str] = []
        normalized_file_path = self.canonicalizer.normalize_rel_path(node.file_path or "")
        if normalized_file_path:
            candidates.append(normalized_file_path)

        if node.qualified_name:
            repo_prefix = f"{node.repo_id}."
            if node.qualified_name.startswith(repo_prefix):
                qualified_suffix = node.qualified_name[len(repo_prefix) :]
                normalized_qualified_path = self.canonicalizer.normalize_rel_path(
                    qualified_suffix.replace(".", "/")
                )
                if normalized_qualified_path:
                    candidates.append(normalized_qualified_path)

        return tuple(dict.fromkeys(candidates))

    @staticmethod
    def _resolve_unique(matches: Sequence[CanonicalNode]) -> CanonicalNode | None:
        return matches[0] if len(matches) == 1 else None


def _ancestor_folder_paths(
    file_path: str,
    canonicalizer: CodeIdentityCanonicalizer,
) -> tuple[str, ...]:
    normalized_path = canonicalizer.normalize_rel_path(file_path)
    if not normalized_path:
        return ()

    paths: list[str] = []
    for ancestor in PurePosixPath(normalized_path).parents:
        ancestor_path = canonicalizer.normalize_rel_path(str(ancestor))
        if ancestor_path:
            paths.append(ancestor_path)
    return tuple(paths)


def _normalize_hint_value(value: str) -> str:
    normalized = " ".join(str(value).split()).strip()
    if not normalized:
        return ""
    while normalized and normalized[0] in "`'\"([{<":
        normalized = normalized[1:].strip()
    while normalized and normalized[-1] in "`'\"),.:;]}>":
        normalized = normalized[:-1].strip()
    if normalized.endswith("()"):  # pragma: no cover
        normalized = normalized[:-2].strip()
    while normalized and normalized[-1] in ",.:;":  # pragma: no cover
        normalized = normalized[:-1].strip()
    return normalized


def _infer_hint_kind(value: str) -> str | None:
    normalized = _normalize_hint_value(value)
    if not normalized:
        return None
    if _looks_like_node_id(normalized):
        return "node_id"
    if _looks_like_file_path(normalized):
        return "file_path"
    if _looks_like_file_basename(normalized):
        return "file_basename"
    if _looks_like_dotted_identifier(normalized):
        if normalized.count(".") >= 2 or normalized[0].islower():
            return "qualified_name"
        return "qualified_name_suffix"
    if _looks_like_symbol_name(normalized):
        return "symbol_name"
    return None


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
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value))
