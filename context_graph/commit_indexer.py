from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .episode_processor import EpisodeProcessor, EpisodeWorkItem
from .episodes import make_entity_node, make_episode
from .git_metadata import CommitInfo, GitMetadata
from .graph_read import GraphReadRepository
from .graph_sinks import BaseCypherGraphSink, CypherStatement
from .metadata import MetadataStore, RepoIndexState
from .models import GraphSnapshot, SnapshotApplyResult, SnapshotDiff
from .snapshot_builder import CodebaseMemorySnapshotProducer
from .temporal import TemporalGraphWriter


@dataclass(frozen=True, slots=True)
class CommitIndexResult:
    repo_id: str
    repo_path: str
    commit: CommitInfo
    previous_commit_sha: str | None
    branch: str | None
    skipped: bool
    skip_reason: str | None
    snapshot: GraphSnapshot | None
    apply_result: SnapshotApplyResult | None
    statements: tuple[CypherStatement, ...]
    applied_commit_shas: tuple[str, ...]


class CommitIndexer:
    def __init__(
        self,
        *,
        metadata_store: MetadataStore,
        graph_sink: BaseCypherGraphSink,
        snapshot_producer: CodebaseMemorySnapshotProducer | None = None,
        git_metadata: GitMetadata | None = None,
        episode_processor: EpisodeProcessor | None = None,
    ) -> None:
        self.metadata_store = metadata_store
        self.graph_sink = graph_sink
        self.snapshot_producer = snapshot_producer or CodebaseMemorySnapshotProducer()
        self.git_metadata = git_metadata or GitMetadata()
        self.episode_processor = episode_processor or EpisodeProcessor(
            graph_sink=graph_sink,
            read_repository=GraphReadRepository(metadata_store=metadata_store, graph_sink=graph_sink),
        )
        self.writer = TemporalGraphWriter(metadata_store=metadata_store, graph_sink=graph_sink)

    def apply_latest_commit(
        self,
        repo_path: str,
        *,
        repo_id: str | None = None,
        branch_hint: str | None = None,
        limit: int = 1000000,
        execute: bool = False,
    ) -> CommitIndexResult:
        resolved_repo_path = self.git_metadata.resolve_repo_root(repo_path)
        resolved_repo_id = repo_id or _default_repo_id(resolved_repo_path)
        current_state = self.metadata_store.load_repo_index_state(resolved_repo_id)
        previous_commit_sha = current_state.latest_commit_sha if current_state else None
        if not self.git_metadata.has_new_commit(
            resolved_repo_path,
            previous_commit_sha,
        ):
            head_commit = self.git_metadata.get_commit_info(
                resolved_repo_path,
                branch_hint=branch_hint,
            )
            return CommitIndexResult(
                repo_id=resolved_repo_id,
                repo_path=resolved_repo_path,
                commit=head_commit,
                previous_commit_sha=previous_commit_sha,
                branch=head_commit.branch,
                skipped=True,
                skip_reason="head commit already indexed",
                snapshot=None,
                apply_result=None,
                statements=(),
                applied_commit_shas=(),
            )

        head_commit = self.git_metadata.get_commit_info(
            resolved_repo_path,
            branch_hint=branch_hint,
        )
        self._ensure_supported_lineage(
            resolved_repo_path,
            previous_commit_sha=previous_commit_sha,
            previous_branch=current_state.latest_branch if current_state else None,
            target_commit=head_commit,
        )
        head_sha = head_commit.sha
        if previous_commit_sha is None:
            return self.apply_commit(
                resolved_repo_path,
                commit_sha=head_sha,
                repo_id=resolved_repo_id,
                branch_hint=branch_hint,
                limit=limit,
                execute=execute,
            )

        commits = self.git_metadata.list_commits_between(
            resolved_repo_path,
            previous_commit_sha,
            head_sha,
        )
        if not commits:
            return self.apply_commit(
                resolved_repo_path,
                commit_sha=head_sha,
                repo_id=resolved_repo_id,
                branch_hint=branch_hint,
                limit=limit,
                execute=execute,
            )

        results = [
            self.apply_commit(
                resolved_repo_path,
                commit_sha=commit_sha,
                repo_id=resolved_repo_id,
                branch_hint=branch_hint,
                limit=limit,
                execute=execute,
            )
            for commit_sha in commits
        ]
        last = results[-1]
        all_statements = tuple(
            statement
            for result in results
            for statement in result.statements
        )
        total_emitted = sum(result.apply_result.statements_emitted for result in results if result.apply_result)
        total_executed = sum(result.apply_result.statements_executed for result in results if result.apply_result)
        aggregate_apply = None
        if last.apply_result is not None:
            aggregate_apply = SnapshotApplyResult(
                snapshot=last.apply_result.snapshot,
                diff=last.apply_result.diff,
                statements_emitted=total_emitted,
                statements_executed=total_executed,
            )
        return CommitIndexResult(
            repo_id=resolved_repo_id,
            repo_path=resolved_repo_path,
            commit=last.commit,
            previous_commit_sha=previous_commit_sha,
            branch=last.branch,
            skipped=False,
            skip_reason=None,
            snapshot=last.snapshot,
            apply_result=aggregate_apply,
            statements=all_statements,
            applied_commit_shas=tuple(
                sha
                for result in results
                for sha in result.applied_commit_shas
            ),
        )

    def apply_commit(
        self,
        repo_path: str,
        *,
        commit_sha: str,
        repo_id: str | None = None,
        branch_hint: str | None = None,
        limit: int = 1000000,
        execute: bool = False,
    ) -> CommitIndexResult:
        resolved_repo_path = self.git_metadata.resolve_repo_root(repo_path)
        resolved_repo_id = repo_id or _default_repo_id(resolved_repo_path)
        current_state = self.metadata_store.load_repo_index_state(resolved_repo_id)
        commit = self.git_metadata.get_commit_info(
            resolved_repo_path,
            commit_sha,
            branch_hint=branch_hint,
        )
        previous_commit_sha = current_state.latest_commit_sha if current_state else None
        if previous_commit_sha == commit.sha:
            return CommitIndexResult(
                repo_id=resolved_repo_id,
                repo_path=resolved_repo_path,
                commit=commit,
                previous_commit_sha=previous_commit_sha,
                branch=commit.branch,
                skipped=True,
                skip_reason="commit already indexed",
                snapshot=None,
                apply_result=None,
                statements=(),
                applied_commit_shas=(),
            )

        self._ensure_supported_lineage(
            resolved_repo_path,
            previous_commit_sha=previous_commit_sha,
            previous_branch=current_state.latest_branch if current_state else None,
            target_commit=commit,
        )

        with self.git_metadata.temporary_worktree(resolved_repo_path, commit.sha) as worktree_path:
            snapshot = self.snapshot_producer.build_snapshot(
                worktree_path,
                repo_id=resolved_repo_id,
                revision_id=commit.sha,
                limit=limit,
            )

        normalized_snapshot = _normalize_snapshot(
            snapshot,
            repo_path=resolved_repo_path,
            commit=commit,
        )
        diff, structural_statements = self.writer.plan_snapshot(normalized_snapshot)
        commit_statements = self.graph_sink.plan_attach_commit(
            normalized_snapshot,
            diff,
            commit=commit,
        )
        episode_statements = self._plan_commit_message_episode(
            normalized_snapshot,
            diff,
            commit=commit,
        )
        all_statements = [*structural_statements, *commit_statements, *episode_statements]
        apply_result = self.writer.apply_planned_snapshot(
            normalized_snapshot,
            diff,
            all_statements,
            execute=execute,
        )
        statements = tuple(all_statements)
        self.metadata_store.save_repo_index_state(
            RepoIndexState(
                repo_id=normalized_snapshot.repo_id,
                root_path=normalized_snapshot.root_path,
                latest_revision_id=normalized_snapshot.revision_id,
                latest_indexed_at=normalized_snapshot.generated_at,
                metadata=dict(normalized_snapshot.metadata),
                latest_commit_sha=commit.sha,
                latest_branch=commit.branch,
                last_commit_indexed_at=normalized_snapshot.generated_at,
            )
        )
        return CommitIndexResult(
            repo_id=resolved_repo_id,
            repo_path=resolved_repo_path,
            commit=commit,
            previous_commit_sha=previous_commit_sha,
            branch=commit.branch,
            skipped=False,
            skip_reason=None,
            snapshot=normalized_snapshot,
            apply_result=apply_result,
            statements=statements,
            applied_commit_shas=(commit.sha,),
        )

    def _plan_commit_message_episode(
        self,
        snapshot: GraphSnapshot,
        diff: SnapshotDiff,
        *,
        commit: CommitInfo,
    ) -> tuple[CypherStatement, ...]:
        if self.episode_processor is None:
            return ()
        text = _commit_message_text(commit)
        if not text:
            return ()
        extra_nodes = [
            make_entity_node(
                node_id=f"commit:{snapshot.repo_id}:{commit.sha}",
                repo_id=snapshot.repo_id,
                kind="Commit",
                name=commit.subject or commit.sha[:12],
                aliases=(commit.sha,),
            )
        ]
        extra_nodes.extend(_touched_node_refs(diff))
        batch = self.episode_processor.apply_batch(
            [
                EpisodeWorkItem(
                    episode=make_episode(
                        episode_id=f"git-commit-message:{commit.sha}",
                        repo_id=snapshot.repo_id,
                        source_type="git_commit_message",
                        source_id=commit.sha,
                        author=commit.author_name,
                        created_at=commit.committed_at,
                        updated_at=commit.committed_at,
                        text=text,
                        metadata={
                            "thread_id": f"commit:{commit.sha}",
                            "author_email": commit.author_email,
                            "branch": commit.branch,
                        },
                        parent_node_id=f"commit:{snapshot.repo_id}:{commit.sha}",
                    ),
                    extra_nodes=tuple(extra_nodes),
                )
            ],
            execute=False,
        )
        return batch.statements

    def _ensure_supported_lineage(
        self,
        repo_path: str,
        *,
        previous_commit_sha: str | None,
        previous_branch: str | None,
        target_commit: CommitInfo,
    ) -> None:
        if previous_commit_sha is None or previous_commit_sha == target_commit.sha:
            return

        if (
            previous_branch
            and target_commit.branch
            and previous_branch != target_commit.branch
        ):
            raise RuntimeError(
                "commit indexing does not support switching branches yet: "
                f"last indexed branch was {previous_branch!r}, "
                f"but the current checkout is on {target_commit.branch!r}"
            )

        if not self.git_metadata.is_ancestor(repo_path, previous_commit_sha, target_commit.sha):
            raise RuntimeError(
                "commit indexing only supports descendant commits from the last indexed revision: "
                f"last indexed commit {previous_commit_sha} is not an ancestor of target commit {target_commit.sha}"
            )


def _default_repo_id(repo_path: str) -> str:
    raw_name = Path(repo_path).resolve().name or "repo"
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw_name).strip("-_.").lower()
    return slug or "repo"


def _commit_message_text(commit: CommitInfo) -> str:
    parts = [part.strip() for part in (commit.subject, commit.body) if part and part.strip()]
    return "\n\n".join(parts)


def _context_ref_from_node(node):
    aliases = []
    if node.file_path:
        aliases.append(node.file_path.rsplit("/", 1)[-1])
    if node.qualified_name:
        aliases.extend(part for part in node.qualified_name.split(".") if part)
    return make_entity_node(
        node_id=node.node_id,
        repo_id=node.repo_id,
        kind=node.kind,
        name=node.name,
        qualified_name=node.qualified_name,
        file_path=node.file_path,
        aliases=tuple(dict.fromkeys(alias for alias in aliases if alias)),
        metadata=node.properties,
    )


def _touched_node_refs(diff) -> list[EntityNode]:
    refs: dict[str, EntityNode] = {}
    for node in diff.added_nodes.values():
        if node.kind != "Project":
            refs[node.node_id] = _context_ref_from_node(node)
    for change in diff.changed_nodes.values():
        node = change.new
        if node.kind != "Project":
            refs[node.node_id] = _context_ref_from_node(node)
    for node in diff.removed_nodes.values():
        if node.kind != "Project":
            refs[node.node_id] = _context_ref_from_node(node)
    return sorted(refs.values(), key=lambda item: item.uuid)


def _normalize_snapshot(snapshot: GraphSnapshot, *, repo_path: str, commit: CommitInfo) -> GraphSnapshot:
    metadata = dict(snapshot.metadata)
    metadata.update(
        {
            "commit_sha": commit.sha,
            "commit_branch": commit.branch,
            "commit_parent_shas": list(commit.parent_shas),
            "commit_changed_files": list(commit.changed_files),
            "source_revision_kind": "git-commit",
        }
    )
    return GraphSnapshot(
        repo_id=snapshot.repo_id,
        revision_id=commit.sha,
        root_path=repo_path,
        generated_at=snapshot.generated_at,
        nodes=snapshot.nodes,
        edges=snapshot.edges,
        metadata=metadata,
    )
