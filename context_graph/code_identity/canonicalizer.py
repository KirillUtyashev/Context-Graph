from __future__ import annotations


class CodeIdentityCanonicalizer:
    """Own deterministic code-node and code-edge identity rules."""

    def normalize_rel_path(self, value: str) -> str:
        rel_path = value.replace("\\", "/")
        while rel_path.startswith("./"):
            rel_path = rel_path[2:]
        rel_path = rel_path.rstrip("/")
        if rel_path == ".":
            return ""
        return rel_path

    def stable_node_id(
        self,
        repo_id: str,
        *,
        kind: str,
        qualified_name: str | None,
        file_path: str | None,
        name: str,
    ) -> str:
        if kind == "Project":
            return f"project:{repo_id}"

        if qualified_name:
            return f"{kind.lower()}:{repo_id}:{qualified_name}"

        rel_path = self.normalize_rel_path(file_path or "")
        if rel_path:
            return f"{kind.lower()}:{repo_id}:{rel_path}:{name}"
        return f"{kind.lower()}:{repo_id}:{name}"

    def stable_edge_id(
        self,
        repo_id: str,
        edge_type: str,
        source_id: str,
        target_id: str,
    ) -> str:
        return f"edge:{repo_id}:{edge_type}:{source_id}:{target_id}"


DEFAULT_CODE_IDENTITY_CANONICALIZER = CodeIdentityCanonicalizer()
