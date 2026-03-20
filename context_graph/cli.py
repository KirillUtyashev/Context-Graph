from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from .graph_sinks import NeptuneGraphSink, Neo4jGraphSink
from .metadata import MetadataStore, PostgresMetadataStore, SQLiteMetadataStore
from .snapshot_builder import CodebaseMemorySnapshotProducer
from .temporal import TemporalGraphWriter


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Context graph snapshot and temporal planning tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser(
        "build-snapshot",
        help="Build a canonical snapshot from a local repository path",
    )
    snapshot_parser.add_argument("repo_path", help="Path to the local repository checkout")
    snapshot_parser.add_argument("--repo-id", help="Stable repository identifier")
    snapshot_parser.add_argument(
        "--revision-id",
        default="working-tree",
        help="Revision identifier to stamp onto the snapshot",
    )
    snapshot_parser.add_argument(
        "--limit",
        type=int,
        default=1000000,
        help="Reserved for exporter compatibility; currently unused",
    )

    plan_parser = subparsers.add_parser(
        "plan-apply",
        help="Build a snapshot, diff it against the latest stored snapshot, and print the Cypher plan",
    )
    plan_parser.add_argument("repo_path", help="Path to the local repository checkout")
    plan_parser.add_argument("--repo-id", help="Stable repository identifier")
    plan_parser.add_argument(
        "--revision-id",
        default="working-tree",
        help="Revision identifier to stamp onto the snapshot",
    )
    plan_parser.add_argument(
        "--limit",
        type=int,
        default=1000000,
        help="Reserved for exporter compatibility; currently unused",
    )
    plan_parser.add_argument(
        "--graph-backend",
        choices=("neo4j", "neptune"),
        default="neo4j",
        help="Graph backend syntax to plan against",
    )
    plan_parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute the planned graph writes against the configured graph backend",
    )
    plan_parser.add_argument(
        "--metadata-backend",
        choices=("sqlite", "postgres"),
        default="sqlite",
        help="Metadata backend used to persist the latest snapshot state",
    )
    plan_parser.add_argument(
        "--metadata-db",
        default=":memory:",
        help="SQLite database path used to persist the latest snapshot metadata",
    )
    plan_parser.add_argument(
        "--metadata-dsn",
        help="PostgreSQL DSN used when --metadata-backend=postgres",
    )
    plan_parser.add_argument(
        "--print-limit",
        type=int,
        default=10,
        help="Maximum number of Cypher statements to print",
    )
    plan_parser.add_argument(
        "--row-preview-limit",
        type=int,
        default=3,
        help="Maximum number of batched parameter rows to print per statement preview",
    )
    plan_parser.add_argument(
        "--neo4j-uri",
        help="Neo4j Bolt URI, e.g. bolt://localhost:7687",
    )
    plan_parser.add_argument(
        "--neo4j-user",
        help="Neo4j username",
    )
    plan_parser.add_argument(
        "--neo4j-password",
        help="Neo4j password",
    )
    plan_parser.add_argument(
        "--neo4j-database",
        help="Neo4j database name",
    )
    plan_parser.add_argument(
        "--neptune-host",
        help="Neptune host used for direct Bolt execution",
    )
    plan_parser.add_argument(
        "--neptune-port",
        type=int,
        default=8182,
        help="Neptune Bolt port",
    )
    return parser


def _producer() -> CodebaseMemorySnapshotProducer:
    root = Path(__file__).resolve().parent.parent
    return CodebaseMemorySnapshotProducer(root=root)


def _make_sink(args):
    if args.graph_backend == "neptune":
        return NeptuneGraphSink(
            host=args.neptune_host or os.getenv("CONTEXT_GRAPH_NEPTUNE_HOST"),
            port=args.neptune_port,
        )

    return Neo4jGraphSink(
        uri=args.neo4j_uri or os.getenv("NEO4J_URI"),
        username=args.neo4j_user or os.getenv("NEO4J_USERNAME") or os.getenv("NEO4J_USER"),
        password=args.neo4j_password or os.getenv("NEO4J_PASSWORD"),
        database=args.neo4j_database or os.getenv("NEO4J_DATABASE"),
    )


def _make_metadata_store(args) -> MetadataStore:
    if args.metadata_backend == "postgres":
        dsn = args.metadata_dsn or os.getenv("CONTEXT_GRAPH_METADATA_DSN")
        if not dsn:
            raise RuntimeError(
                "PostgreSQL metadata backend requires --metadata-dsn or CONTEXT_GRAPH_METADATA_DSN"
            )
        return PostgresMetadataStore.from_dsn(dsn)

    return SQLiteMetadataStore(args.metadata_db)


def _snapshot_summary(snapshot) -> dict[str, object]:
    edge_counts: dict[str, int] = {}
    for edge in snapshot.edges:
        edge_counts[edge.edge_type] = edge_counts.get(edge.edge_type, 0) + 1
    return {
        "repo_id": snapshot.repo_id,
        "revision_id": snapshot.revision_id,
        "root_path": snapshot.root_path,
        "generated_at": snapshot.generated_at,
        "node_count": len(snapshot.nodes),
        "edge_count": len(snapshot.edges),
        "edge_type_counts": edge_counts,
        "metadata": dict(snapshot.metadata),
    }


def _preview_parameters(parameters: dict[str, object], *, row_limit: int) -> dict[str, object]:
    preview: dict[str, object] = {}
    for key, value in parameters.items():
        if isinstance(value, list) and len(value) > row_limit:
            preview[key] = {
                "count": len(value),
                "preview": value[:row_limit],
                "truncated": len(value) - row_limit,
            }
        else:
            preview[key] = value
    return preview


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    producer = _producer()

    if args.command == "build-snapshot":
        snapshot = producer.build_snapshot(
            args.repo_path,
            repo_id=args.repo_id,
            revision_id=args.revision_id,
            limit=args.limit,
        )
        print(json.dumps(_snapshot_summary(snapshot), indent=2, sort_keys=True))
        return 0

    if args.command == "plan-apply":
        snapshot = producer.build_snapshot(
            args.repo_path,
            repo_id=args.repo_id,
            revision_id=args.revision_id,
            limit=args.limit,
        )
        metadata = _make_metadata_store(args)
        writer = TemporalGraphWriter(
            metadata_store=metadata,
            graph_sink=_make_sink(args),
        )
        result = writer.apply_snapshot(snapshot, execute=args.execute)
        statements = writer.graph_sink.plan_apply_diff(snapshot, result.diff)

        payload = {
            "snapshot": _snapshot_summary(snapshot),
            "diff": {
                "previous_revision_id": result.diff.previous_revision_id,
                "new_revision_id": result.diff.new_revision_id,
                "added_nodes": len(result.diff.added_nodes),
                "changed_nodes": len(result.diff.changed_nodes),
                "removed_nodes": len(result.diff.removed_nodes),
                "added_edges": len(result.diff.added_edges),
                "changed_edges": len(result.diff.changed_edges),
                "removed_edges": len(result.diff.removed_edges),
            },
            "statements_emitted": result.statements_emitted,
            "statements_executed": result.statements_executed,
            "statements_preview": [
                {
                    "query": statement.query,
                    "parameters": _preview_parameters(
                        dict(statement.parameters),
                        row_limit=args.row_preview_limit,
                    ),
                }
                for statement in statements[: args.print_limit]
            ],
        }
        try:
            print(json.dumps(payload, indent=2, sort_keys=True))
        except BrokenPipeError:
            try:
                sys.stderr.close()
            except OSError:
                pass
            return 0
        return 0

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
