"""Profile the five selected assignment functions using the stdlib ``profile`` module.

The implementation follows the Python profiling docs by using ``profile.Profile``
plus ``pstats.Stats`` summaries. Each workload prepares synthetic-but-realistic
inputs outside the profile window, then profiles only the target function call.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import io
import json
from pathlib import Path
import profile
import pstats
from pstats import SortKey
import time
from typing import Any, Callable, Mapping, Sequence

from context_graph.ingestion.commit_indexing import CommitIndexingService
from context_graph.ingestion.diffing import diff_snapshots
from context_graph.ingestion.deterministic_planner import DeterministicMutationPlanner
from context_graph.ingestion.git.github_client import GitHubRepoRef
from context_graph.ingestion.git.github_indexer import GitHubIndexer
from context_graph.ingestion.git.git_metadata import CommitInfo
from context_graph.models.graph import CanonicalEdge, CanonicalNode, GraphSnapshot, utc_now_iso
from context_graph.provenance.invalidation import CurrentEvidenceReader
from context_graph.provenance.models import ClaimEvidenceExtractionResponse
from context_graph.provenance.services import FactConflictResolutionService
from context_graph.query.code_graph import trace_call_path
from context_graph.backend import InMemoryMetadataStore, RepoIndexState

TARGET_APPLY_COMMIT = "apply_commit"
TARGET_PLAN_SNAPSHOT_DIFF = "plan_snapshot_diff"
TARGET_LOAD_CURRENT_FACTS = "load_current_facts"
TARGET_BUILD_BUNDLE = "build_bundle"
TARGET_TRACE_CALL_PATH = "trace_call_path"

ALL_TARGETS = (
    TARGET_APPLY_COMMIT,
    TARGET_PLAN_SNAPSHOT_DIFF,
    TARGET_LOAD_CURRENT_FACTS,
    TARGET_BUILD_BUNDLE,
    TARGET_TRACE_CALL_PATH,
)


@dataclass(frozen=True, slots=True)
class ScaleConfig:
    module_count: int
    functions_per_module: int
    call_edges_per_function: int
    changed_node_stride: int
    removed_module_stride: int
    extra_function_stride: int
    load_current_fact_groups: int
    load_current_records_per_fact: int
    load_current_invocations: int
    github_commit_count: int
    github_prs_per_commit: int
    github_issues_per_pr: int
    github_reviews_per_pr: int
    github_review_comments_per_pr: int
    github_issue_comments_per_issue: int
    github_bundle_invocations: int
    trace_depth: int
    trace_invocations: int


SCALES: dict[str, ScaleConfig] = {
    "small": ScaleConfig(
        module_count=8,
        functions_per_module=8,
        call_edges_per_function=2,
        changed_node_stride=5,
        removed_module_stride=4,
        extra_function_stride=3,
        load_current_fact_groups=12,
        load_current_records_per_fact=2,
        load_current_invocations=8,
        github_commit_count=3,
        github_prs_per_commit=2,
        github_issues_per_pr=1,
        github_reviews_per_pr=1,
        github_review_comments_per_pr=2,
        github_issue_comments_per_issue=2,
        github_bundle_invocations=4,
        trace_depth=3,
        trace_invocations=8,
    ),
    "medium": ScaleConfig(
        module_count=18,
        functions_per_module=14,
        call_edges_per_function=3,
        changed_node_stride=4,
        removed_module_stride=5,
        extra_function_stride=4,
        load_current_fact_groups=40,
        load_current_records_per_fact=3,
        load_current_invocations=20,
        github_commit_count=8,
        github_prs_per_commit=2,
        github_issues_per_pr=2,
        github_reviews_per_pr=2,
        github_review_comments_per_pr=3,
        github_issue_comments_per_issue=3,
        github_bundle_invocations=8,
        trace_depth=4,
        trace_invocations=20,
    ),
    "large": ScaleConfig(
        module_count=30,
        functions_per_module=22,
        call_edges_per_function=4,
        changed_node_stride=3,
        removed_module_stride=6,
        extra_function_stride=5,
        load_current_fact_groups=100,
        load_current_records_per_fact=4,
        load_current_invocations=40,
        github_commit_count=16,
        github_prs_per_commit=3,
        github_issues_per_pr=2,
        github_reviews_per_pr=3,
        github_review_comments_per_pr=4,
        github_issue_comments_per_issue=4,
        github_bundle_invocations=16,
        trace_depth=5,
        trace_invocations=40,
    ),
}


@dataclass(frozen=True, slots=True)
class PreparedWorkload:
    target: str
    function_name: str
    setup_summary: Mapping[str, Any]
    runner: Callable[[], Any]


@dataclass(frozen=True, slots=True)
class ProfileRunResult:
    target: str
    function_name: str
    output_prof: str
    output_txt: str
    output_json: str
    wall_time_seconds: float
    setup_summary: Mapping[str, Any]
    result_summary: Mapping[str, Any]


def run_profiles(
    *,
    targets: Sequence[str] = ALL_TARGETS,
    output_dir: str | Path = "profiling/results",
    scale: str = "medium",
    repeat_multiplier: int = 1,
    top_n: int = 25,
) -> tuple[ProfileRunResult, ...]:
    if scale not in SCALES:
        raise ValueError(f"unsupported scale {scale!r}; expected one of {sorted(SCALES)}")
    invalid_targets = [target for target in targets if target not in ALL_TARGETS]
    if invalid_targets:
        raise ValueError(f"unsupported targets: {invalid_targets}")
    if repeat_multiplier < 1:
        raise ValueError("repeat_multiplier must be >= 1")
    if top_n < 1:
        raise ValueError("top_n must be >= 1")

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    scale_config = SCALES[scale]

    results: list[ProfileRunResult] = []
    for target in targets:
        prepared = _prepare_workload(target=target, scale=scale_config, repeat_multiplier=repeat_multiplier)
        results.append(
            _run_single_profile(
                prepared=prepared,
                output_dir=output_root,
                top_n=top_n,
            )
        )
    return tuple(results)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Profile the five selected assignment functions with the stdlib profile module.",
    )
    parser.add_argument(
        "--target",
        choices=("all", *ALL_TARGETS),
        default="all",
        help="One target function to profile, or 'all' to run every workload.",
    )
    parser.add_argument(
        "--scale",
        choices=tuple(sorted(SCALES)),
        default="medium",
        help="Synthetic workload size.",
    )
    parser.add_argument(
        "--repeat-multiplier",
        type=int,
        default=1,
        help="Multiply the built-in invocation count for repeatable workloads.",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=25,
        help="How many rows to keep in each text summary.",
    )
    parser.add_argument(
        "--output-dir",
        default="profiling/results",
        help="Directory for .prof, .txt, and .json outputs.",
    )
    args = parser.parse_args(tuple(argv) if argv is not None else None)

    selected_targets = ALL_TARGETS if args.target == "all" else (args.target,)
    results = run_profiles(
        targets=selected_targets,
        output_dir=args.output_dir,
        scale=args.scale,
        repeat_multiplier=args.repeat_multiplier,
        top_n=args.top_n,
    )
    for result in results:
        print(
            f"{result.target}: {result.function_name} "
            f"wall={result.wall_time_seconds:.4f}s "
            f"prof={result.output_prof} "
            f"txt={result.output_txt}"
        )
    return 0


def _prepare_workload(
    *,
    target: str,
    scale: ScaleConfig,
    repeat_multiplier: int,
) -> PreparedWorkload:
    if target == TARGET_APPLY_COMMIT:
        return _prepare_apply_commit(scale=scale)
    if target == TARGET_PLAN_SNAPSHOT_DIFF:
        return _prepare_plan_snapshot_diff(scale=scale, repeat_multiplier=repeat_multiplier)
    if target == TARGET_LOAD_CURRENT_FACTS:
        return _prepare_load_current_facts(scale=scale, repeat_multiplier=repeat_multiplier)
    if target == TARGET_BUILD_BUNDLE:
        return _prepare_build_bundle(scale=scale, repeat_multiplier=repeat_multiplier)
    if target == TARGET_TRACE_CALL_PATH:
        return _prepare_trace_call_path(scale=scale, repeat_multiplier=repeat_multiplier)
    raise ValueError(f"unsupported target {target!r}")


def _run_single_profile(
    *,
    prepared: PreparedWorkload,
    output_dir: Path,
    top_n: int,
) -> ProfileRunResult:
    profiler = profile.Profile()
    started = time.perf_counter()
    result = profiler.runcall(prepared.runner)
    wall_time_seconds = time.perf_counter() - started

    prof_path = output_dir / f"{prepared.target}.prof"
    txt_path = output_dir / f"{prepared.target}.txt"
    json_path = output_dir / f"{prepared.target}.json"

    profiler.dump_stats(str(prof_path))
    txt_path.write_text(
        _render_profile_report(
            prepared=prepared,
            profiler=profiler,
            wall_time_seconds=wall_time_seconds,
            top_n=top_n,
        ),
        encoding="utf-8",
    )

    payload = {
        "target": prepared.target,
        "function_name": prepared.function_name,
        "wall_time_seconds": wall_time_seconds,
        "output_prof": str(prof_path),
        "output_txt": str(txt_path),
        "setup_summary": dict(prepared.setup_summary),
        "result_summary": _summarize_result(result),
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return ProfileRunResult(
        target=prepared.target,
        function_name=prepared.function_name,
        output_prof=str(prof_path),
        output_txt=str(txt_path),
        output_json=str(json_path),
        wall_time_seconds=wall_time_seconds,
        setup_summary=dict(prepared.setup_summary),
        result_summary=_summarize_result(result),
    )


def _render_profile_report(
    *,
    prepared: PreparedWorkload,
    profiler: profile.Profile,
    wall_time_seconds: float,
    top_n: int,
) -> str:
    stream = io.StringIO()
    stream.write(f"Target: {prepared.target}\n")
    stream.write(f"Function: {prepared.function_name}\n")
    stream.write(f"Wall time (outer measurement): {wall_time_seconds:.6f} seconds\n")
    stream.write("Setup summary:\n")
    for key, value in prepared.setup_summary.items():
        stream.write(f"  - {key}: {value}\n")
    stream.write("\nOrdered by cumulative time\n")
    cumulative = pstats.Stats(profiler, stream=stream)
    cumulative.strip_dirs().sort_stats(SortKey.CUMULATIVE).print_stats(top_n)
    stream.write("\nOrdered by internal time\n")
    internal = pstats.Stats(profiler, stream=stream)
    internal.strip_dirs().sort_stats(SortKey.TIME).print_stats(top_n)
    return stream.getvalue()


def _prepare_apply_commit(scale: ScaleConfig) -> PreparedWorkload:
    repo_id = "profile-demo"
    root_path = "/synthetic/context-graph"
    previous_snapshot, current_snapshot = _build_snapshot_pair(
        repo_id=repo_id,
        root_path=root_path,
        scale=scale,
    )
    metadata_store = InMemoryMetadataStore()
    metadata_store.bootstrap()
    metadata_store.save_latest_snapshot(previous_snapshot)
    metadata_store.save_repo_index_state(
        RepoIndexState(
            repo_id=repo_id,
            root_path=root_path,
            latest_revision_id=previous_snapshot.revision_id,
            latest_indexed_at=previous_snapshot.generated_at,
            metadata=dict(previous_snapshot.metadata),
            latest_commit_sha=previous_snapshot.revision_id,
            latest_branch="main",
            last_commit_indexed_at=previous_snapshot.generated_at,
        )
    )
    commit = CommitInfo(
        sha=current_snapshot.revision_id,
        parent_shas=(previous_snapshot.revision_id,),
        branch="main",
        author_name="Profiler",
        author_email="profiler@example.com",
        authored_at=current_snapshot.generated_at,
        committed_at=current_snapshot.generated_at,
        subject="Update profile-demo.module_0.func_0 for deterministic profiling",
        body=(
            "profile-demo.module_0.func_0 was updated to exercise the deterministic "
            "indexing, observation, and evidence planning pipeline."
        ),
        changed_files=("src/module_0.py", "src/module_1.py"),
    )
    extraction_response = ClaimEvidenceExtractionResponse.model_validate(
        {
            "claims": [
                {
                    "claim_text": "profile-demo.module_0.func_0 was updated in this commit.",
                    "claim_subkind": "fix_or_mitigation",
                    "about_hints": ["profile-demo.module_0.func_0"],
                    "confidence": "high",
                    "derivation_kind": "direct_from_observation",
                    "metadata": {},
                    "supporting_evidence": [
                        {
                            "evidence_kind": "change_summary",
                            "quote_text": "profile-demo.module_0.func_0 was updated",
                            "summary_text": (
                                "The commit message states that "
                                "profile-demo.module_0.func_0 was updated."
                            ),
                            "span_start": None,
                            "span_end": None,
                            "about_hints": ["profile-demo.module_0.func_0"],
                            "support_kind": "direct_statement",
                            "metadata": {},
                        }
                    ],
                }
            ]
        }
    )
    service = CommitIndexingService(
        metadata_store=metadata_store,
        backend_driver=None,
        snapshot_producer=_StaticSnapshotProducer(current_snapshot),
        git_metadata=_StaticGitMetadata(root_path=root_path, commit=commit),
        llm_client=None,
        claim_extraction_service=_StaticClaimExtractionService(extraction_response),
        fact_conflict_resolution_service=FactConflictResolutionService(llm_client=None),
    )

    def runner() -> Any:
        return service.apply_commit(
            root_path,
            commit_sha=commit.sha,
            repo_id=repo_id,
            execute=False,
        )

    return PreparedWorkload(
        target=TARGET_APPLY_COMMIT,
        function_name="CommitIndexingService.apply_commit",
        setup_summary={
            "previous_revision": previous_snapshot.revision_id,
            "current_revision": current_snapshot.revision_id,
            "nodes_previous": len(previous_snapshot.nodes),
            "nodes_current": len(current_snapshot.nodes),
            "edges_previous": len(previous_snapshot.edges),
            "edges_current": len(current_snapshot.edges),
            "claim_extraction_enabled": True,
        },
        runner=runner,
    )


def _prepare_plan_snapshot_diff(
    *,
    scale: ScaleConfig,
    repeat_multiplier: int,
) -> PreparedWorkload:
    previous_snapshot, current_snapshot = _build_snapshot_pair(
        repo_id="profile-demo",
        root_path="/synthetic/context-graph",
        scale=scale,
    )
    diff = diff_snapshots(previous_snapshot, current_snapshot)
    planner = DeterministicMutationPlanner()
    invocations = max(1, repeat_multiplier)

    def runner() -> Any:
        result = None
        for _ in range(invocations):
            result = planner.plan_snapshot_diff(
                previous_snapshot=previous_snapshot,
                snapshot=current_snapshot,
                diff=diff,
            )
        return result

    return PreparedWorkload(
        target=TARGET_PLAN_SNAPSHOT_DIFF,
        function_name="DeterministicMutationPlanner.plan_snapshot_diff",
        setup_summary={
            "invocations": invocations,
            "added_nodes": len(diff.added_nodes),
            "changed_nodes": len(diff.changed_nodes),
            "removed_nodes": len(diff.removed_nodes),
            "added_edges": len(diff.added_edges),
            "changed_edges": len(diff.changed_edges),
            "removed_edges": len(diff.removed_edges),
        },
        runner=runner,
    )


def _prepare_load_current_facts(
    *,
    scale: ScaleConfig,
    repeat_multiplier: int,
) -> PreparedWorkload:
    rows = _build_persisted_evidence_rows(
        fact_groups=scale.load_current_fact_groups,
        records_per_fact=scale.load_current_records_per_fact,
    )
    backend_driver = _FakeEvidenceBackendDriver(rows)
    reader = CurrentEvidenceReader(backend_driver=backend_driver)
    invocations = scale.load_current_invocations * repeat_multiplier

    def runner() -> Any:
        result = None
        for _ in range(invocations):
            result = reader.load_current_facts(
                repo_id="profile-demo",
                claim_kind="state_claim",
                about_record_ids=("symbol:profile-demo:profile-demo.module_0.func_0@rev-current",),
                excluded_fact_keys=(),
                limit=10,
            )
        return result

    return PreparedWorkload(
        target=TARGET_LOAD_CURRENT_FACTS,
        function_name="CurrentEvidenceReader.load_current_facts",
        setup_summary={
            "invocations": invocations,
            "rows": len(rows),
            "fact_groups": scale.load_current_fact_groups,
            "records_per_fact": scale.load_current_records_per_fact,
        },
        runner=runner,
    )


def _prepare_build_bundle(
    *,
    scale: ScaleConfig,
    repeat_multiplier: int,
) -> PreparedWorkload:
    repo_ref = GitHubRepoRef(
        owner="example",
        name="profile-demo",
        canonical_repo="example/profile-demo",
        clone_url="https://github.com/example/profile-demo.git",
        html_url="https://github.com/example/profile-demo",
        default_branch="main",
        source="synthetic",
    )
    commit_shas = tuple(f"commit-{index:04d}" for index in range(scale.github_commit_count))
    github_api = _FakeGitHubApi(
        commit_shas=commit_shas,
        prs_per_commit=scale.github_prs_per_commit,
        issues_per_pr=scale.github_issues_per_pr,
        reviews_per_pr=scale.github_reviews_per_pr,
        review_comments_per_pr=scale.github_review_comments_per_pr,
        issue_comments_per_issue=scale.github_issue_comments_per_issue,
    )
    indexer = GitHubIndexer(
        github_api=github_api,
        backend_driver=None,
        llm_client=None,
    )
    invocations = scale.github_bundle_invocations * repeat_multiplier

    def runner() -> Any:
        result = None
        for _ in range(invocations):
            result = indexer._build_bundle(
                repo_id="profile-demo",
                repo_ref=repo_ref,
                commit_shas=commit_shas,
            )
        return result

    return PreparedWorkload(
        target=TARGET_BUILD_BUNDLE,
        function_name="GitHubIndexer._build_bundle",
        setup_summary={
            "invocations": invocations,
            "commit_shas": len(commit_shas),
            "prs_per_commit": scale.github_prs_per_commit,
            "issues_per_pr": scale.github_issues_per_pr,
            "reviews_per_pr": scale.github_reviews_per_pr,
            "review_comments_per_pr": scale.github_review_comments_per_pr,
            "issue_comments_per_issue": scale.github_issue_comments_per_issue,
        },
        runner=runner,
    )


def _prepare_trace_call_path(
    *,
    scale: ScaleConfig,
    repeat_multiplier: int,
) -> PreparedWorkload:
    snapshot = _build_snapshot(
        repo_id="profile-demo",
        revision_id="trace-rev",
        root_path="/synthetic/context-graph",
        module_count=scale.module_count,
        functions_per_module=scale.functions_per_module,
        call_edges_per_function=scale.call_edges_per_function,
        changed_stride=None,
        removed_functions=frozenset(),
        extra_function_modules=frozenset(),
    )
    qualified_name = _select_trace_start(snapshot)
    invocations = scale.trace_invocations * repeat_multiplier

    def runner() -> Any:
        result = None
        for _ in range(invocations):
            result = trace_call_path(
                snapshot,
                qualified_name=qualified_name,
                direction="both",
                depth=scale.trace_depth,
            )
        return result

    return PreparedWorkload(
        target=TARGET_TRACE_CALL_PATH,
        function_name="trace_call_path",
        setup_summary={
            "invocations": invocations,
            "qualified_name": qualified_name,
            "nodes": len(snapshot.nodes),
            "edges": len(snapshot.edges),
            "depth": scale.trace_depth,
        },
        runner=runner,
    )


def _build_snapshot_pair(
    *,
    repo_id: str,
    root_path: str,
    scale: ScaleConfig,
) -> tuple[GraphSnapshot, GraphSnapshot]:
    previous_snapshot = _build_snapshot(
        repo_id=repo_id,
        revision_id="rev-previous",
        root_path=root_path,
        module_count=scale.module_count,
        functions_per_module=scale.functions_per_module,
        call_edges_per_function=scale.call_edges_per_function,
        changed_stride=None,
        removed_functions=frozenset(),
        extra_function_modules=frozenset(),
    )
    removed_functions = frozenset(
        (module_index, scale.functions_per_module - 1)
        for module_index in range(scale.module_count)
        if module_index % scale.removed_module_stride == 0
    )
    extra_function_modules = frozenset(
        module_index
        for module_index in range(scale.module_count)
        if module_index % scale.extra_function_stride == 1
    )
    current_snapshot = _build_snapshot(
        repo_id=repo_id,
        revision_id="rev-current",
        root_path=root_path,
        module_count=scale.module_count,
        functions_per_module=scale.functions_per_module,
        call_edges_per_function=scale.call_edges_per_function,
        changed_stride=scale.changed_node_stride,
        removed_functions=removed_functions,
        extra_function_modules=extra_function_modules,
    )
    return previous_snapshot, current_snapshot


def _build_snapshot(
    *,
    repo_id: str,
    revision_id: str,
    root_path: str,
    module_count: int,
    functions_per_module: int,
    call_edges_per_function: int,
    changed_stride: int | None,
    removed_functions: frozenset[tuple[int, int]],
    extra_function_modules: frozenset[int],
) -> GraphSnapshot:
    nodes: list[CanonicalNode] = [
        CanonicalNode(
            node_id=f"project:{repo_id}",
            repo_id=repo_id,
            kind="Project",
            name=repo_id,
            qualified_name=repo_id,
            properties={"root_path": root_path},
        )
    ]
    edges: list[CanonicalEdge] = []
    function_nodes: list[CanonicalNode] = []

    for module_index in range(module_count):
        file_name = f"module_{module_index}.py"
        file_path = f"src/{file_name}"
        file_qn = f"{repo_id}.module_{module_index}.__file__"
        file_node_id = f"file:{repo_id}:{file_qn}"
        file_properties: dict[str, Any] = {"module_index": module_index}
        if changed_stride is not None and module_index % changed_stride == 0:
            file_properties["change_marker"] = revision_id
        file_node = CanonicalNode(
            node_id=file_node_id,
            repo_id=repo_id,
            kind="File",
            name=file_name,
            qualified_name=file_qn,
            file_path=file_path,
            start_line=1,
            end_line=functions_per_module * 8 + 10,
            properties=file_properties,
        )
        nodes.append(file_node)

        extra_functions = 1 if module_index in extra_function_modules else 0
        for function_index in range(functions_per_module + extra_functions):
            if (module_index, function_index) in removed_functions:
                continue
            qualified_name = f"{repo_id}.module_{module_index}.func_{function_index}"
            node_properties: dict[str, Any] = {
                "module_index": module_index,
                "function_index": function_index,
            }
            start_line = function_index * 8 + 1
            end_line = start_line + 5
            if changed_stride is not None and (module_index + function_index) % changed_stride == 0:
                node_properties["change_marker"] = revision_id
                end_line += 2
            function_node = CanonicalNode(
                node_id=f"symbol:{repo_id}:{qualified_name}",
                repo_id=repo_id,
                kind="Function",
                name=f"func_{function_index}",
                qualified_name=qualified_name,
                file_path=file_path,
                start_line=start_line,
                end_line=end_line,
                properties=node_properties,
            )
            nodes.append(function_node)
            function_nodes.append(function_node)
            edges.append(
                CanonicalEdge(
                    edge_id=f"edge:{repo_id}:DEFINES:{file_node_id}:{function_node.node_id}",
                    repo_id=repo_id,
                    edge_type="DEFINES",
                    source_id=file_node_id,
                    target_id=function_node.node_id,
                )
            )

    if function_nodes:
        max_outbound = min(call_edges_per_function, max(0, len(function_nodes) - 1))
        for index, source in enumerate(function_nodes):
            for offset in range(1, max_outbound + 1):
                target = function_nodes[(index + offset) % len(function_nodes)]
                edges.append(
                    CanonicalEdge(
                        edge_id=f"edge:{repo_id}:CALLS:{source.node_id}:{target.node_id}",
                        repo_id=repo_id,
                        edge_type="CALLS",
                        source_id=source.node_id,
                        target_id=target.node_id,
                    )
                )

    return GraphSnapshot(
        repo_id=repo_id,
        revision_id=revision_id,
        root_path=root_path,
        generated_at=utc_now_iso(),
        nodes=tuple(nodes),
        edges=tuple(edges),
        metadata={},
    )


def _select_trace_start(snapshot: GraphSnapshot) -> str:
    degree_by_node_id: dict[str, int] = {}
    for edge in snapshot.edges:
        if edge.edge_type != "CALLS":
            continue
        degree_by_node_id[edge.source_id] = degree_by_node_id.get(edge.source_id, 0) + 1
        degree_by_node_id[edge.target_id] = degree_by_node_id.get(edge.target_id, 0) + 1
    ranked_ids = sorted(
        degree_by_node_id,
        key=lambda node_id: (degree_by_node_id[node_id], node_id),
        reverse=True,
    )
    node_map = snapshot.node_map()
    for node_id in ranked_ids:
        node = node_map.get(node_id)
        if node is not None and node.qualified_name:
            return node.qualified_name
    raise RuntimeError("synthetic snapshot did not produce a traceable qualified name")


class _StaticSnapshotProducer:
    def __init__(self, snapshot: GraphSnapshot) -> None:
        self.snapshot = snapshot

    def build_snapshot(
        self,
        repo_path: str,
        *,
        repo_id: str | None = None,
        revision_id: str,
        limit: int = 1000000,
    ) -> GraphSnapshot:
        _ = repo_path, repo_id, limit
        return GraphSnapshot(
            repo_id=self.snapshot.repo_id,
            revision_id=revision_id,
            root_path=self.snapshot.root_path,
            generated_at=self.snapshot.generated_at,
            nodes=self.snapshot.nodes,
            edges=self.snapshot.edges,
            metadata=dict(self.snapshot.metadata),
        )


class _StaticGitMetadata:
    def __init__(self, *, root_path: str, commit: CommitInfo) -> None:
        self.root_path = root_path
        self.commit = commit

    def resolve_repo_root(self, repo_path: str) -> str:
        _ = repo_path
        return self.root_path

    def get_commit_info(
        self,
        repo_path: str,
        commit_sha: str | None = None,
        branch_hint: str | None = None,
    ) -> CommitInfo:
        _ = repo_path, branch_hint
        if commit_sha is not None and commit_sha != self.commit.sha:
            raise RuntimeError(f"unknown synthetic commit {commit_sha!r}")
        return self.commit

    def is_first_parent_ancestor(
        self,
        repo_path: str,
        ancestor_sha: str,
        target_sha: str,
    ) -> bool:
        _ = repo_path
        return target_sha == self.commit.sha and ancestor_sha in self.commit.parent_shas

    @contextmanager
    def temporary_worktree(self, repo_path: str, commit_sha: str):
        _ = repo_path
        if commit_sha != self.commit.sha:
            raise RuntimeError(f"unexpected synthetic worktree request for {commit_sha!r}")
        yield self.root_path


class _StaticClaimExtractionService:
    def __init__(self, response: ClaimEvidenceExtractionResponse) -> None:
        self.response = response

    def extract(
        self,
        observation: Any,
        *,
        source_context: Mapping[str, Any] | None = None,
        code_context: Mapping[str, Any] | None = None,
    ) -> ClaimEvidenceExtractionResponse:
        _ = observation, source_context, code_context
        return self.response


class _FakeEvidenceBackendDriver:
    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.rows = [dict(row) for row in rows]

    async def execute_query(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        _ = query, kwargs
        return [dict(row) for row in self.rows]


def _build_persisted_evidence_rows(
    *,
    fact_groups: int,
    records_per_fact: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fact_index in range(fact_groups):
        claim_text = f"profile-demo.module_0.func_0 is in state variant {fact_index}."
        claim_kind = "state_claim"
        fact_key = _canonical_claim_key(claim_kind=claim_kind, claim_text=claim_text)
        observed_at = f"2026-03-{(fact_index % 27) + 1:02d}T00:00:00+00:00"
        for record_index in range(records_per_fact):
            top_level_claims = record_index % 2 == 0
            evidence_id = f"evidence:profile-demo:{fact_index}:{record_index}"
            rows.append(
                {
                    "evidence_id": evidence_id,
                    "evidence_properties": {
                        "evidence_id": evidence_id,
                        "repo_id": "profile-demo",
                        "evidence_kind": "state_detail",
                        "quote_text": f"{claim_text} citation {record_index}",
                        "summary_text": f"Summary for {claim_text}",
                        "span_start": 0,
                        "span_end": 32,
                        "support_kind": "direct_statement",
                        "fact_key": fact_key if top_level_claims else None,
                        "claim_text": claim_text if top_level_claims else None,
                        "claim_kind": claim_kind if top_level_claims else None,
                        "claim_confidence": "high" if top_level_claims else None,
                        "claim_derivation_kind": "direct_from_observation" if top_level_claims else None,
                        "observation_id": (
                            f"observation:github_pr_body:profile-demo:{fact_index}"
                            if top_level_claims
                            else None
                        ),
                        "observed_at": observed_at if top_level_claims else None,
                        "is_current": True,
                        "metadata_json": json.dumps(
                            {
                                "claim_text": claim_text,
                                "claim_kind": claim_kind,
                                "claim_confidence": "high",
                                "claim_derivation_kind": "direct_from_observation",
                            },
                            sort_keys=True,
                        ),
                    },
                    "about_record_ids": [
                        "symbol:profile-demo:profile-demo.module_0.func_0@rev-current",
                        f"file:profile-demo:profile-demo.module_{fact_index % 3}.__file__@rev-current",
                    ],
                    "observation_record_ids": [f"observation:github_pr_body:profile-demo:{fact_index}"],
                    "observation_properties": [{"observed_at": observed_at}],
                }
            )
    return rows


class _FakeGitHubApi:
    def __init__(
        self,
        *,
        commit_shas: Sequence[str],
        prs_per_commit: int,
        issues_per_pr: int,
        reviews_per_pr: int,
        review_comments_per_pr: int,
        issue_comments_per_issue: int,
    ) -> None:
        self.commit_shas = tuple(commit_shas)
        self.prs_per_commit = prs_per_commit
        self.issues_per_pr = issues_per_pr
        self.reviews_per_pr = reviews_per_pr
        self.review_comments_per_pr = review_comments_per_pr
        self.issue_comments_per_issue = issue_comments_per_issue
        self._commit_to_prs: dict[str, tuple[int, ...]] = {}
        self._pr_to_issues: dict[int, tuple[int, ...]] = {}
        self._build_indices()

    def _build_indices(self) -> None:
        next_pr = 1
        next_issue = 100
        for commit_sha in self.commit_shas:
            pr_numbers = tuple(range(next_pr, next_pr + self.prs_per_commit))
            self._commit_to_prs[commit_sha] = pr_numbers
            next_pr += self.prs_per_commit
            for pr_number in pr_numbers:
                issue_numbers = tuple(range(next_issue, next_issue + self.issues_per_pr))
                self._pr_to_issues[pr_number] = issue_numbers
                next_issue += self.issues_per_pr

    def list_commit_pull_requests(self, owner: str, repo: str, sha: str) -> list[dict[str, Any]]:
        _ = owner, repo
        return [{"number": number} for number in self._commit_to_prs.get(sha, ())]

    def get_pull_request(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        _ = owner, repo
        return {
            "id": number * 1000,
            "number": number,
            "html_url": f"https://github.com/example/profile-demo/pull/{number}",
            "title": f"Profile PR {number}",
            "state": "open",
            "user": {"login": f"author-{number % 7}"},
            "created_at": "2026-03-01T00:00:00+00:00",
            "updated_at": "2026-03-02T00:00:00+00:00",
            "body": f"This PR updates profiling path {number}.",
            "merged_at": None,
            "merge_commit_sha": None,
            "base": {"ref": "main"},
            "head": {"ref": f"feature/{number}", "sha": f"head-{number:04d}"},
        }

    def list_pull_request_reviews(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        _ = owner, repo
        return [
            {
                "id": number * 10000 + review_index,
                "html_url": f"https://github.com/example/profile-demo/pull/{number}#review-{review_index}",
                "body": f"Review {review_index} on PR {number}.",
                "state": "COMMENTED",
                "commit_id": f"commit-{number:04d}",
                "submitted_at": "2026-03-02T01:00:00+00:00",
                "updated_at": "2026-03-02T01:00:00+00:00",
                "user": {"login": f"reviewer-{review_index % 5}"},
            }
            for review_index in range(self.reviews_per_pr)
        ]

    def list_pull_request_review_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        _ = owner, repo
        return [
            {
                "id": number * 20000 + comment_index,
                "html_url": f"https://github.com/example/profile-demo/pull/{number}#discussion_r{comment_index}",
                "body": f"Review comment {comment_index} on PR {number}.",
                "path": f"src/module_{comment_index % 5}.py",
                "commit_id": f"commit-{number:04d}",
                "pull_request_review_id": number * 10000 + (comment_index % max(1, self.reviews_per_pr)),
                "created_at": "2026-03-02T02:00:00+00:00",
                "updated_at": "2026-03-02T02:00:00+00:00",
                "user": {"login": f"commenter-{comment_index % 6}"},
            }
            for comment_index in range(self.review_comments_per_pr)
        ]

    def list_issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        _ = owner, repo
        return [
            {
                "id": number * 30000 + comment_index,
                "html_url": f"https://github.com/example/profile-demo/issues/{number}#issuecomment-{comment_index}",
                "body": f"Issue comment {comment_index} on issue {number}.",
                "created_at": "2026-03-03T01:00:00+00:00",
                "updated_at": "2026-03-03T01:00:00+00:00",
                "user": {"login": f"issue-commenter-{comment_index % 4}"},
            }
            for comment_index in range(self.issue_comments_per_issue)
        ]

    def list_pull_request_linked_issue_numbers(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> tuple[int, ...]:
        _ = owner, repo
        return self._pr_to_issues[number]

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        _ = owner, repo
        return {
            "id": number * 40000,
            "number": number,
            "html_url": f"https://github.com/example/profile-demo/issues/{number}",
            "title": f"Profile issue {number}",
            "state": "open",
            "user": {"login": f"issue-author-{number % 5}"},
            "created_at": "2026-03-03T00:00:00+00:00",
            "updated_at": "2026-03-03T00:30:00+00:00",
            "closed_at": None,
            "body": f"Issue {number} discusses profiling and graph consistency.",
        }


def _summarize_result(result: Any) -> dict[str, Any]:
    if result is None:
        return {"type": "NoneType"}
    if hasattr(result, "operation_count"):
        return {"type": type(result).__name__, "operation_count": int(result.operation_count())}
    if hasattr(result, "operations"):
        summary = {"type": type(result).__name__}
        operations = getattr(result, "operations")
        if hasattr(operations, "operation_count"):
            summary["operation_count"] = int(operations.operation_count())
        if hasattr(result, "applied_commit_shas"):
            summary["applied_commit_shas"] = list(getattr(result, "applied_commit_shas"))
        if hasattr(result, "evidence_invalidated"):
            summary["evidence_invalidated"] = int(getattr(result, "evidence_invalidated"))
        return summary
    if hasattr(result, "validated_claims") and hasattr(result, "rejected_claims"):
        return {
            "type": type(result).__name__,
            "validated_claims": len(getattr(result, "validated_claims", ()) or ()),
            "rejected_claims": len(getattr(result, "rejected_claims", ()) or ()),
        }
    if hasattr(result, "entities") and hasattr(result, "observation_sources"):
        return {
            "type": type(result).__name__,
            "entities": len(getattr(result, "entities", ()) or ()),
            "observation_sources": len(getattr(result, "observation_sources", ()) or ()),
        }
    if hasattr(result, "matches"):
        return {
            "type": type(result).__name__,
            "matches": len(getattr(result, "matches", ()) or ()),
        }
    if hasattr(result, "steps"):
        return {
            "type": type(result).__name__,
            "steps": len(getattr(result, "steps", ()) or ()),
        }
    if isinstance(result, Sequence) and not isinstance(result, (str, bytes, bytearray)):
        return {"type": type(result).__name__, "length": len(result)}
    if hasattr(result, "__dict__"):
        return {"type": type(result).__name__, "repr": repr(result)}
    return {"type": type(result).__name__, "repr": repr(result)}


def _canonical_claim_key(*, claim_kind: str, claim_text: str) -> str:
    from context_graph.provenance.canonicalization import canonical_claim_key

    return canonical_claim_key(claim_kind=claim_kind, claim_text=claim_text)
