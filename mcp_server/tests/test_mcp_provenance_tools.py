from __future__ import annotations

from pathlib import Path
import asyncio
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.backend import (
    BackendProvider,
    GraphRecordRef,
    RecordKind,
    SearchHit,
    TraversalHit,
)
import src.provenance as provenance_tools
from src.state import ServerState
from src.tools import observations as observation_tools
from src.tools import provenance as provenance_tool_module


def make_code_graph_snapshot(root_path: str) -> GraphSnapshot:
    repo_id = "demo"
    project = CanonicalNode(
        "project:demo",
        repo_id,
        "Project",
        "demo",
        qualified_name="demo",
    )
    app_file = CanonicalNode(
        "file:demo:app",
        repo_id,
        "File",
        "app.py",
        qualified_name="demo.app.__file__",
        file_path="app.py",
    )
    main = CanonicalNode(
        "symbol:demo:demo.app.main",
        repo_id,
        "Function",
        "main",
        qualified_name="demo.app.main",
        file_path="app.py",
        start_line=1,
        end_line=2,
    )
    helper = CanonicalNode(
        "symbol:demo:demo.app.helper",
        repo_id,
        "Function",
        "helper",
        qualified_name="demo.app.helper",
        file_path="app.py",
        start_line=4,
        end_line=5,
    )
    edges = (
        CanonicalEdge("edge:project-app", repo_id, "CONTAINS_FILE", project.node_id, app_file.node_id),
        CanonicalEdge("edge:defines-main", repo_id, "DEFINES", app_file.node_id, main.node_id),
        CanonicalEdge("edge:defines-helper", repo_id, "DEFINES", app_file.node_id, helper.node_id),
        CanonicalEdge("edge:calls-main-helper", repo_id, "CALLS", main.node_id, helper.node_id),
    )
    return GraphSnapshot(
        repo_id=repo_id,
        revision_id="rev-123",
        root_path=root_path,
        generated_at="2026-03-27T00:00:00+00:00",
        nodes=(project, app_file, main, helper),
        edges=edges,
        metadata={},
    )


class FakeSearchOperations:
    def __init__(self, hits: list[SearchHit], records: dict[str, dict]) -> None:
        self._hits = hits
        self._records = records
        self.search_queries = []

    async def search(self, query):
        self.search_queries.append(query)
        results = list(self._hits)
        if query.record_kinds:
            allowed_kinds = set(query.record_kinds)
            results = [hit for hit in results if hit.ref.record_kind in allowed_kinds]
        if query.record_types:
            allowed_types = set(query.record_types)
            results = [hit for hit in results if hit.ref.record_type in allowed_types]
        if query.filters:
            filtered: list[SearchHit] = []
            for hit in results:
                record = self._records.get(hit.ref.record_id, {})
                properties = dict(record.get("properties") or {})
                include = True
                for key, value in query.filters.items():
                    candidate = properties.get(key, record.get(key))
                    if candidate != value:
                        include = False
                        break
                if include:
                    filtered.append(hit)
            results = filtered
        return results[: query.limit]

    async def fetch(self, refs):
        return [
            dict(self._records[ref.record_id])
            for ref in refs
            if ref.record_id in self._records
        ]


class FakeTraversalOperations:
    def __init__(self, hits: list[TraversalHit] | None = None) -> None:
        self._hits = list(hits or [])
        self.expand_queries = []

    async def expand(self, query):
        self.expand_queries.append(query)
        return list(self._hits)


class FakeDriver:
    def __init__(
        self,
        hits: list[SearchHit],
        records: dict[str, dict],
        *,
        provider: BackendProvider = BackendProvider.NEO4J,
        execute_query_results: list[dict] | None = None,
        traversal_hits: list[TraversalHit] | None = None,
    ) -> None:
        self.provider = provider
        self.search = FakeSearchOperations(hits, records)
        self.traversal = FakeTraversalOperations(traversal_hits)
        self.execute_query_calls = []
        self._execute_query_results = list(execute_query_results or [])

    async def execute_query(self, query: str, **kwargs):
        self.execute_query_calls.append((query, kwargs))
        return list(self._execute_query_results)


class FakeRuntime:
    def __init__(
        self,
        *,
        driver=None,
        llm_client=None,
        claim_resolution_service=None,
    ) -> None:
        self._driver = driver
        self.llm_client = llm_client
        self.claim_resolution_service = claim_resolution_service

    async def ensure_initialized(self) -> None:
        return None

    async def get_driver(self):
        return self._driver


class ProvenanceToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        (root / "app.py").write_text(
            "def main():\n"
            "    return helper()\n\n"
            "def helper():\n"
            "    return 1\n",
            encoding="utf-8",
        )
        self.snapshot = make_code_graph_snapshot(self.temp_dir.name)
        self.hits = [
            SearchHit(
                ref=GraphRecordRef(
                    record_kind=RecordKind.NODE,
                    record_id="claim:1",
                    record_type="claim",
                    repo_id="demo",
                    group_id="main",
                ),
                score=0.93,
            ),
            SearchHit(
                ref=GraphRecordRef(
                    record_kind=RecordKind.NODE,
                    record_id="symbol:demo:demo.app.main",
                    record_type="code_node",
                    repo_id="demo",
                    group_id="main",
                ),
                score=0.71,
            ),
            SearchHit(
                ref=GraphRecordRef(
                    record_kind=RecordKind.NODE,
                    record_id="evidence:1",
                    record_type="evidence_record",
                    repo_id="demo",
                    group_id="main",
                ),
                score=0.82,
            ),
        ]
        self.records = {
            "claim:1": {
                "record_id": "claim:1",
                "record_type": "claim",
                "properties": {
                    "claim_id": "claim:1",
                    "claim_text": "JWT audience validation is required.",
                    "claim_kind": "state_claim",
                    "claim_subkind": "desired_behavior_or_requirement",
                    "latest_observed_at": "2026-03-27T00:00:00Z",
                },
            },
            "symbol:demo:demo.app.main": {
                "record_id": "symbol:demo:demo.app.main",
                "record_type": "code_node",
                "properties": {
                    "title": "demo.app.main",
                    "qualified_name": "demo.app.main",
                    "summary_text": "Main application entry point.",
                    "file_path": "app.py",
                },
            },
            "evidence:1": {
                "record_id": "evidence:1",
                "record_type": "evidence_record",
                "properties": {
                    "observation_id": "observation:1",
                    "quote_text": "Added JWT audience validation.",
                    "summary_text": "Audience validation was added.",
                    "evidence_kind": "commit_excerpt",
                    "support_kind": "states_change",
                },
            },
        }
        self.driver = FakeDriver(self.hits, self.records)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_search_context_uses_lexical_backend_hydration(self) -> None:
        response = provenance_tools.search_context(
            driver=self.driver,
            repo_id="demo",
            query="audience validation",
            group_id="main",
            sources=["claims", "code"],
            limit=10,
        )

        self.assertEqual(response["status"], "implemented")
        self.assertEqual([hit["record_type"] for hit in response["hits"]], ["claim", "code_node"])
        self.assertIn("Semantic node and relation profile search is unavailable", response["message"])
        self.assertEqual(self.driver.search.search_queries[0].mode.value, "lexical")
        self.assertNotIn("score", response["hits"][0])
        self.assertNotIn("retrieval_channels", response["hits"][0])
        self.assertEqual(response["hits"][1]["qualified_name"], "demo.app.main")

    def test_search_context_fuses_lexical_and_semantic_claim_hits(self) -> None:
        def fake_semantic_search(**kwargs):
            self.assertEqual(kwargs["profile_kinds"], ("semantic_claim_profile",))
            return [
                {
                    "profile_id": "profile:demo:semantic_claim_profile:claim:1",
                    "profile_kind": "semantic_claim_profile",
                    "profile_target_kind": "Claim",
                    "profile_target_id": "claim:1",
                    "relation_type": None,
                    "value_text": "JWT audience validation is required.",
                    "source_node_id": None,
                    "target_node_id": None,
                    "is_active": True,
                    "score": 0.88,
                    "matched_key_text": "audience requirement",
                    "matched_key_origin": "generated_semantic",
                    "metadata": {
                        "claim_id": "claim:1",
                        "claim_text": "JWT audience validation is required.",
                        "claim_kind": "state_claim",
                        "claim_subkind": "desired_behavior_or_requirement",
                    },
                }
            ]

        response = provenance_tools.search_context(
            driver=self.driver,
            semantic_search=fake_semantic_search,
            repo_id="demo",
            query="audience validation",
            group_id="main",
            sources=["claims"],
            limit=10,
        )

        self.assertEqual(len(response["hits"]), 1)
        hit = response["hits"][0]
        self.assertEqual(hit["record_id"], "claim:1")
        self.assertEqual(hit["hit_kind"], "node")
        self.assertIn("separate node and relation search pipelines", response["message"])

    def test_search_context_keeps_claim_hits_as_claims_when_observation_is_loaded(self) -> None:
        driver = FakeDriver(
            [
                SearchHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="claim:claim-max-retries",
                        record_type="claim",
                        repo_id="demo",
                        group_id="main",
                    ),
                    score=0.95,
                ),
                SearchHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="evidence:max-retries",
                        record_type="evidence_record",
                        repo_id="demo",
                        group_id="main",
                    ),
                    score=0.82,
                ),
            ],
            {
                "claim:claim-max-retries": {
                    "record_id": "claim:claim-max-retries",
                    "record_type": "claim",
                    "properties": {
                        "claim_id": "claim:claim-max-retries",
                        "claim_text": "The failure was originally due to keeping MAX_RETRIES at 3.",
                        "claim_kind": "state_claim",
                        "claim_subkind": "root_cause_or_problem",
                        "observation_id": "observation:max-retries",
                        "latest_observed_at": "2026-03-29T00:00:00Z",
                    },
                },
                "evidence:max-retries": {
                    "record_id": "evidence:max-retries",
                    "record_type": "evidence_record",
                    "properties": {
                        "observation_id": "observation:max-retries",
                        "quote_text": "MAX_RETRIES remained at 3 and caused early task failure.",
                        "summary_text": "MAX_RETRIES caused early failure.",
                    },
                },
                "observation:max-retries": {
                    "record_id": "observation:max-retries",
                    "record_type": "observation",
                    "properties": {
                        "title": "Retry follow-up",
                        "text": "Rotated SECRET_KEY and changed APP_PORT before discussing MAX_RETRIES.",
                        "observed_at": "2026-03-29T00:00:00Z",
                    },
                },
            },
        )

        response = provenance_tools.search_context(
            driver=driver,
            repo_id="demo",
            query="Why MAX_RETRIES has value 5?",
            group_id="main",
            sources=["claims", "observations"],
            limit=10,
        )

        self.assertEqual(
            [hit["record_type"] for hit in response["hits"]],
            ["claim", "observation"],
        )
        self.assertEqual(response["hits"][0]["record_id"], "claim:claim-max-retries")

    def test_search_context_dedupes_multiple_evidence_hits_for_same_observation(self) -> None:
        driver = FakeDriver(
            [
                SearchHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="evidence:1",
                        record_type="evidence_record",
                        repo_id="demo",
                        group_id="main",
                    ),
                    score=0.91,
                ),
                SearchHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="evidence:2",
                        record_type="evidence_record",
                        repo_id="demo",
                        group_id="main",
                    ),
                    score=0.87,
                ),
            ],
            {
                "evidence:1": {
                    "record_id": "evidence:1",
                    "record_type": "evidence_record",
                    "properties": {
                        "observation_id": "observation:1",
                        "quote_text": "MAX_RETRIES remained at 3 and caused failure.",
                    },
                },
                "evidence:2": {
                    "record_id": "evidence:2",
                    "record_type": "evidence_record",
                    "properties": {
                        "observation_id": "observation:1",
                        "quote_text": "MAX_RETRIES was later increased to 5.",
                    },
                },
                "observation:1": {
                    "record_id": "observation:1",
                    "record_type": "observation",
                    "properties": {
                        "title": "Retries",
                        "text": "Long observation text about several config changes.",
                        "observed_at": "2026-03-29T00:00:00Z",
                    },
                },
            },
        )

        response = provenance_tools.search_context(
            driver=driver,
            repo_id="demo",
            query="MAX_RETRIES",
            group_id="main",
            sources=["observations"],
            limit=10,
        )

        self.assertEqual(len(response["hits"]), 1)
        self.assertEqual(response["hits"][0]["record_type"], "observation")
        self.assertNotIn("score", response["hits"][0])

    def test_search_context_ranks_relevant_claim_ahead_of_surfaced_observation(self) -> None:
        driver = FakeDriver(
            [
                SearchHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="evidence:max-retries",
                        record_type="evidence_record",
                        repo_id="demo",
                        group_id="main",
                    ),
                    score=0.94,
                ),
                SearchHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="claim:max-retries",
                        record_type="claim",
                        repo_id="demo",
                        group_id="main",
                    ),
                    score=0.82,
                ),
            ],
            {
                "evidence:max-retries": {
                    "record_id": "evidence:max-retries",
                    "record_type": "evidence_record",
                    "properties": {
                        "observation_id": "observation:max-retries",
                        "quote_text": "MAX_RETRIES remained at 3 and caused early task failure.",
                    },
                },
                "claim:max-retries": {
                    "record_id": "claim:max-retries",
                    "record_type": "claim",
                    "properties": {
                        "claim_id": "claim:max-retries",
                        "claim_text": "The failure was originally due to keeping MAX_RETRIES at 3, which caused early task failure.",
                        "claim_kind": "state_claim",
                        "claim_subkind": "root_cause_or_problem",
                        "observation_id": "observation:max-retries",
                        "latest_observed_at": "2026-03-29T00:00:00Z",
                    },
                },
                "observation:max-retries": {
                    "record_id": "observation:max-retries",
                    "record_type": "observation",
                    "properties": {
                        "title": "Config changes",
                        "text": "Rotated SECRET_KEY in auth.py, reduced TOKEN_TTL_HOURS, and changed APP_PORT before mentioning MAX_RETRIES near the end.",
                        "observed_at": "2026-03-29T00:00:00Z",
                    },
                },
            },
        )

        def fake_semantic_search(**kwargs):
            if kwargs["profile_kinds"] == ("semantic_claim_profile",):
                return [
                    {
                        "profile_id": "profile:demo:semantic_claim_profile:claim:max-retries",
                        "profile_kind": "semantic_claim_profile",
                        "profile_target_kind": "Claim",
                        "profile_target_id": "claim:max-retries",
                        "relation_type": None,
                        "value_text": "The failure was originally due to keeping MAX_RETRIES at 3, which caused early task failure.",
                        "source_node_id": None,
                        "target_node_id": None,
                        "is_active": True,
                        "score": 0.91,
                        "matched_key_text": "retry failure root cause",
                        "matched_key_origin": "generated_semantic",
                        "metadata": {
                            "claim_id": "claim:max-retries",
                            "claim_text": "The failure was originally due to keeping MAX_RETRIES at 3, which caused early task failure.",
                            "claim_kind": "state_claim",
                            "claim_subkind": "root_cause_or_problem",
                        },
                    }
                ]
            return []

        response = provenance_tools.search_context(
            driver=driver,
            semantic_search=fake_semantic_search,
            repo_id="demo",
            query="Why MAX_RETRIES has value 5?",
            group_id="main",
            sources=["claims", "observations"],
            limit=10,
        )

        self.assertEqual(
            [hit["record_type"] for hit in response["hits"][:2]],
            ["claim", "observation"],
        )

    def test_search_context_prefers_matched_evidence_excerpt_for_surfaced_observation_summary(self) -> None:
        driver = FakeDriver(
            [
                SearchHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="evidence:max-retries",
                        record_type="evidence_record",
                        repo_id="demo",
                        group_id="main",
                    ),
                    score=0.94,
                ),
            ],
            {
                "evidence:max-retries": {
                    "record_id": "evidence:max-retries",
                    "record_type": "evidence_record",
                    "properties": {
                        "observation_id": "observation:max-retries",
                        "quote_text": "MAX_RETRIES remained at 3 and caused early task failure.",
                        "summary_text": "Retry configuration caused failure.",
                    },
                },
                "observation:max-retries": {
                    "record_id": "observation:max-retries",
                    "record_type": "observation",
                    "properties": {
                        "title": "Config changes",
                        "text": "Rotated SECRET_KEY in auth.py and changed APP_PORT long before mentioning the retry issue.",
                        "observed_at": "2026-03-29T00:00:00Z",
                    },
                },
            },
        )

        response = provenance_tools.search_context(
            driver=driver,
            repo_id="demo",
            query="MAX_RETRIES failure",
            group_id="main",
            sources=["observations"],
            limit=10,
        )

        self.assertEqual(len(response["hits"]), 1)
        self.assertEqual(
            response["hits"][0]["summary"],
            "MAX_RETRIES remained at 3 and caused early task failure.",
        )

    def test_search_context_includes_relation_hits_by_default(self) -> None:
        def fake_semantic_search(**kwargs):
            if kwargs["profile_kinds"] == ("semantic_claim_profile",):
                return [
                    {
                        "profile_id": "profile:demo:semantic_claim_profile:claim:1",
                        "profile_kind": "semantic_claim_profile",
                        "profile_target_kind": "Claim",
                        "profile_target_id": "claim:1",
                        "relation_type": None,
                        "value_text": "JWT audience validation is required.",
                        "source_node_id": None,
                        "target_node_id": None,
                        "is_active": True,
                        "score": 0.88,
                        "matched_key_text": "audience requirement",
                        "matched_key_origin": "generated_semantic",
                        "metadata": {
                            "claim_id": "claim:1",
                            "claim_text": "JWT audience validation is required.",
                            "claim_kind": "state_claim",
                            "claim_subkind": "desired_behavior_or_requirement",
                            "is_current": True,
                        },
                    }
                ]
            if kwargs["profile_kinds"] == ("semantic_relation_profile",):
                self.assertTrue(kwargs["active_only"])
                return [
                    {
                        "profile_id": "profile:demo:semantic_relation_profile:claim_relation:claim:2:claim:1:contradicts",
                        "profile_kind": "semantic_relation_profile",
                        "profile_target_kind": "Relation",
                        "profile_target_id": "claim_relation:claim:2:claim:1:contradicts",
                        "relation_type": "contradicts",
                        "value_text": "The rollout note contradicts the required audience validation.",
                        "source_node_id": "claim:2",
                        "target_node_id": "claim:1",
                        "is_active": True,
                        "score": 0.79,
                        "matched_key_text": "contradicts the required audience validation",
                        "matched_key_origin": "raw_text",
                        "metadata": {
                            "source_record_id": "claim:2",
                            "source_record_type": "claim",
                            "target_record_id": "claim:1",
                            "target_record_type": "claim",
                            "observed_at": "2026-03-29T00:00:00Z",
                        },
                    }
                ]
            return []

        response = provenance_tools.search_context(
            driver=self.driver,
            semantic_search=fake_semantic_search,
            repo_id="demo",
            query="audience validation contradiction",
            group_id="main",
            sources=["claims", "relations"],
            limit=10,
        )

        self.assertEqual([hit["hit_kind"] for hit in response["hits"]], ["node", "relation"])
        relation_hit = response["hits"][1]
        self.assertEqual(relation_hit["relation_type"], "contradicts")
        self.assertEqual(relation_hit["source_record_id"], "claim:2")
        self.assertEqual(relation_hit["target_record_id"], "claim:1")

    def test_search_context_can_return_lexical_relation_hits(self) -> None:
        relation_record_id = "claim_relation:claim:2:claim:1:contradicts"
        driver = FakeDriver(
            [
                SearchHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.EDGE,
                        record_id=relation_record_id,
                        record_type="claim_relation",
                        repo_id="demo",
                        group_id="main",
                    ),
                    score=0.84,
                )
            ],
            {
                relation_record_id: {
                    "record_id": relation_record_id,
                    "record_type": "claim_relation",
                    "relationship_type": "CONTRADICTS",
                    "source_record_id": "claim:2",
                    "target_record_id": "claim:1",
                    "properties": {
                        "resolution_type": "contradicts",
                        "resolution_rationale": "The rollout note contradicts the required audience validation.",
                        "resolution_confidence": "high",
                        "observed_at": "2026-03-29T00:00:00Z",
                        "is_active": True,
                    },
                }
            },
        )

        response = provenance_tools.search_context(
            driver=driver,
            repo_id="demo",
            query="audience validation contradiction",
            group_id="main",
            sources=["relations"],
            limit=10,
        )

        self.assertEqual(len(response["hits"]), 1)
        relation_hit = response["hits"][0]
        self.assertEqual(relation_hit["hit_kind"], "relation")
        self.assertEqual(relation_hit["record_id"], relation_record_id)
        self.assertEqual(relation_hit["relation_type"], "contradicts")
        self.assertEqual(relation_hit["source_record_id"], "claim:2")
        self.assertEqual(relation_hit["target_record_id"], "claim:1")

    def test_search_context_include_historical_surfaces_inactive_relation_hits(self) -> None:
        def fake_semantic_search(**kwargs):
            if kwargs["profile_kinds"] == ("semantic_relation_profile",):
                if kwargs["active_only"]:
                    return []
                return [
                    {
                        "profile_id": "profile:demo:semantic_relation_profile:claim_relation:claim:2:claim:1:contradicts",
                        "profile_kind": "semantic_relation_profile",
                        "profile_target_kind": "Relation",
                        "profile_target_id": "claim_relation:claim:2:claim:1:contradicts",
                        "relation_type": "contradicts",
                        "value_text": "The rollout note contradicts the required audience validation.",
                        "source_node_id": "claim:2",
                        "target_node_id": "claim:1",
                        "is_active": False,
                        "score": 0.79,
                        "matched_key_text": "contradicts the required audience validation",
                        "matched_key_origin": "raw_text",
                        "metadata": {
                            "source_record_id": "claim:2",
                            "source_record_type": "claim",
                            "target_record_id": "claim:1",
                            "target_record_type": "claim",
                            "observed_at": "2026-03-29T00:00:00Z",
                        },
                    }
                ]
            return []

        hidden_response = provenance_tools.search_context(
            driver=self.driver,
            semantic_search=fake_semantic_search,
            repo_id="demo",
            query="audience validation contradiction",
            group_id="main",
            sources=["relations"],
            limit=10,
        )
        self.assertEqual(hidden_response["hits"], [])

        historical_response = provenance_tools.search_context(
            driver=self.driver,
            semantic_search=fake_semantic_search,
            repo_id="demo",
            query="audience validation contradiction",
            group_id="main",
            sources=["relations"],
            limit=10,
            include_historical=True,
        )
        self.assertEqual(len(historical_response["hits"]), 1)
        self.assertFalse(historical_response["hits"][0]["is_active"])
        self.assertIsNone(historical_response["hits"][0]["inactive_at"])

    def test_search_context_include_historical_surfaces_inactive_claim_profile_hits(self) -> None:
        driver = FakeDriver([], {})

        def fake_semantic_search(**kwargs):
            if kwargs["profile_kinds"] == ("semantic_claim_profile",):
                if kwargs["active_only"]:
                    return []
                return [
                    {
                        "profile_id": "profile:demo:semantic_claim_profile:claim:inactive",
                        "profile_kind": "semantic_claim_profile",
                        "profile_target_kind": "Claim",
                        "profile_target_id": "claim:inactive",
                        "relation_type": None,
                        "value_text": "JWT audience validation was later rolled back.",
                        "source_node_id": None,
                        "target_node_id": None,
                        "is_active": False,
                        "score": 0.77,
                        "matched_key_text": "audience validation rollback",
                        "matched_key_origin": "generated_semantic",
                        "metadata": {
                            "claim_id": "claim:inactive",
                            "claim_text": "JWT audience validation was later rolled back.",
                            "claim_kind": "change_claim",
                            "claim_subkind": "fix_or_mitigation",
                            "is_current": False,
                        },
                    }
                ]
            return []

        current_only = provenance_tools.search_context(
            driver=driver,
            semantic_search=fake_semantic_search,
            repo_id="demo",
            query="audience rollback",
            group_id="main",
            sources=["claims"],
            limit=10,
        )
        self.assertEqual(current_only["hits"], [])

        historical = provenance_tools.search_context(
            driver=driver,
            semantic_search=fake_semantic_search,
            repo_id="demo",
            query="audience rollback",
            group_id="main",
            sources=["claims"],
            limit=10,
            include_historical=True,
        )
        self.assertEqual(len(historical["hits"]), 1)
        self.assertEqual(historical["hits"][0]["hit_kind"], "node")
        self.assertFalse(historical["hits"][0]["is_active"])
        self.assertIsNone(historical["hits"][0]["inactive_at"])

    def test_get_code_context_builds_deterministic_slice(self) -> None:
        response = provenance_tools.get_code_context(
            snapshot=self.snapshot,
            repo_id="demo",
            target="demo.app.main",
            target_kind="function",
            include_neighbors=True,
            include_references=True,
            max_related_symbols=5,
        )

        self.assertEqual(response["status"], "implemented")
        self.assertEqual(response["primary_symbol"]["qualified_name"], "demo.app.main")
        self.assertIn(
            "demo.app.helper",
            [symbol["qualified_name"] for symbol in response["related_symbols"]],
        )
        self.assertTrue(response["snippets"])

    def test_get_node_neighborhood_uses_snapshot_for_code_nodes(self) -> None:
        neighborhood = provenance_tools.get_node_neighborhood(
            service=provenance_tools.NeighborhoodQueryService(
                driver=self.driver,
                snapshot=self.snapshot,
            ),
            repo_id="demo",
            node_id="symbol:demo:demo.app.main",
            hops=1,
            edge_types=None,
        )

        self.assertEqual(neighborhood["status"], "implemented")
        self.assertEqual(neighborhood["center_record_id"], "symbol:demo:demo.app.main")
        self.assertEqual(
            [node["record_id"] for node in neighborhood["nodes"]],
            ["symbol:demo:demo.app.main", "file:demo:app", "symbol:demo:demo.app.helper"],
        )
        self.assertEqual(neighborhood["nodes"][0]["qualified_name"], "demo.app.main")
        self.assertEqual(
            [edge["edge_type"] for edge in neighborhood["edges"]],
            ["CALLS", "DEFINES"],
        )

    def test_get_node_neighborhood_uses_neo4j_backend_for_provenance_nodes(self) -> None:
        relation_record_id = "claim_relation:claim:2:claim:1:contradicts"
        driver = FakeDriver(
            [],
            {
                "claim:1": {
                    "record_id": "claim:1",
                    "record_type": "claim",
                    "properties": {
                        "claim_id": "claim:1",
                        "claim_text": "JWT audience validation is required.",
                        "latest_observed_at": "2026-03-27T00:00:00Z",
                    },
                    "repo_id": "demo",
                    "group_id": "main",
                },
                "observation:1": {
                    "record_id": "observation:1",
                    "record_type": "observation",
                    "repo_id": "demo",
                    "group_id": "main",
                    "properties": {
                        "title": "Auth note",
                        "text": "Added JWT audience validation.",
                        "observed_at": "2026-03-28T00:00:00Z",
                    },
                },
                relation_record_id: {
                    "record_id": relation_record_id,
                    "record_type": "claim_relation",
                    "repo_id": "demo",
                    "group_id": "main",
                    "source_record_id": "claim:2",
                    "target_record_id": "claim:1",
                    "relationship_type": "CONTRADICTS",
                    "properties": {
                        "resolution_type": "contradicts",
                        "resolution_rationale": "The rollout note contradicts the required audience validation.",
                        "observed_at": "2026-03-29T00:00:00Z",
                        "is_active": True,
                    },
                },
            },
            traversal_hits=[
                TraversalHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="observation:1",
                        record_type="observation",
                        repo_id="demo",
                        group_id="main",
                    ),
                    distance=1,
                    path_metadata={"record_kind": "node", "record_type": "observation"},
                ),
                TraversalHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.EDGE,
                        record_id=relation_record_id,
                        record_type="claim_relation",
                        repo_id="demo",
                        group_id="main",
                    ),
                    distance=1,
                    path_metadata={
                        "record_kind": "edge",
                        "record_type": "claim_relation",
                        "relationship_type": "CONTRADICTS",
                        "source_record_id": "claim:2",
                        "target_record_id": "claim:1",
                    },
                ),
            ],
        )

        neighborhood = provenance_tools.get_node_neighborhood(
            service=provenance_tools.NeighborhoodQueryService(driver=driver),
            repo_id="demo",
            node_id="claim:1",
            hops=2,
            edge_types=["claim_relation"],
        )

        self.assertEqual(neighborhood["status"], "implemented")
        self.assertEqual(neighborhood["nodes"][0]["record_id"], "claim:1")
        self.assertEqual(neighborhood["edges"][0]["edge_type"], "contradicts")
        self.assertEqual(driver.traversal.expand_queries[0].origin_refs[0].record_id, "claim:1")
        self.assertEqual(driver.traversal.expand_queries[0].max_depth, 2)
        self.assertEqual(driver.traversal.expand_queries[0].edge_types, ("claim_relation",))

    def test_get_node_neighborhood_returns_implemented_for_neptune_provenance_nodes(self) -> None:
        relation_record_id = "claim_relation:claim:2:claim:1:contradicts"
        driver = FakeDriver(
            [],
            {
                "claim:1": {
                    "record_id": "claim:1",
                    "record_type": "claim",
                    "repo_id": "demo",
                    "group_id": "main",
                    "properties": {"claim_text": "JWT audience validation is required."},
                },
                "claim:2": {
                    "record_id": "claim:2",
                    "record_type": "claim",
                    "repo_id": "demo",
                    "group_id": "main",
                    "properties": {"claim_text": "JWT audience validation was removed."},
                },
                relation_record_id: {
                    "record_id": relation_record_id,
                    "record_type": "claim_relation",
                    "repo_id": "demo",
                    "group_id": "main",
                    "source_record_id": "claim:2",
                    "target_record_id": "claim:1",
                    "relationship_type": "CONTRADICTS",
                    "properties": {
                        "resolution_type": "contradicts",
                        "resolution_rationale": "The rollout note contradicts the required audience validation.",
                        "observed_at": "2026-03-29T00:00:00Z",
                        "is_active": True,
                    },
                },
            },
            provider=BackendProvider.NEPTUNE,
            traversal_hits=[
                TraversalHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="claim:2",
                        record_type="claim",
                        repo_id="demo",
                        group_id="main",
                    ),
                    distance=1,
                    path_metadata={"record_kind": "node", "record_type": "claim"},
                ),
                TraversalHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.EDGE,
                        record_id=relation_record_id,
                        record_type="claim_relation",
                        repo_id="demo",
                        group_id="main",
                    ),
                    distance=1,
                    path_metadata={
                        "record_kind": "edge",
                        "record_type": "claim_relation",
                        "relationship_type": "CONTRADICTS",
                        "source_record_id": "claim:2",
                        "target_record_id": "claim:1",
                    },
                ),
            ],
        )

        neighborhood = provenance_tools.get_node_neighborhood(
            service=provenance_tools.NeighborhoodQueryService(driver=driver),
            repo_id="demo",
            node_id="claim:1",
            hops=2,
            edge_types=None,
        )

        self.assertEqual(neighborhood["status"], "implemented")
        self.assertEqual(neighborhood["center_record_id"], "claim:1")
        self.assertEqual([node["record_id"] for node in neighborhood["nodes"]], ["claim:1", "claim:2"])

    def test_get_timeline_builds_historical_events_from_exact_target(self) -> None:
        relation_record_id = "claim_relation:claim:2:claim:1:contradicts"
        driver = FakeDriver(
            [],
            {
                "claim:1": {
                    "record_id": "claim:1",
                    "record_type": "claim",
                    "repo_id": "demo",
                    "group_id": "main",
                    "properties": {
                        "claim_id": "claim:1",
                        "claim_text": "JWT audience validation is required.",
                        "latest_observed_at": "2026-03-27T00:00:00Z",
                    },
                },
                "observation:1": {
                    "record_id": "observation:1",
                    "record_type": "observation",
                    "repo_id": "demo",
                    "group_id": "main",
                    "properties": {
                        "title": "Auth note",
                        "text": "Added JWT audience validation.",
                        "observed_at": "2026-03-28T00:00:00Z",
                    },
                },
                "claim:2": {
                    "record_id": "claim:2",
                    "record_type": "claim",
                    "repo_id": "demo",
                    "group_id": "main",
                    "properties": {
                        "claim_id": "claim:2",
                        "claim_text": "JWT audience validation was removed.",
                        "latest_observed_at": "2026-03-30T00:00:00Z",
                        "is_current": False,
                        "valid_to": "2026-03-31T00:00:00Z",
                    },
                },
                relation_record_id: {
                    "record_id": relation_record_id,
                    "record_type": "claim_relation",
                    "repo_id": "demo",
                    "group_id": "main",
                    "source_record_id": "claim:2",
                    "target_record_id": "claim:1",
                    "relationship_type": "CONTRADICTS",
                    "properties": {
                        "resolution_type": "contradicts",
                        "resolution_rationale": "The rollout note contradicts the required audience validation.",
                        "observed_at": "2026-03-29T00:00:00Z",
                        "is_active": False,
                        "deactivated_at": "2026-03-31T00:00:00Z",
                    },
                },
            },
            traversal_hits=[
                TraversalHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="observation:1",
                        record_type="observation",
                        repo_id="demo",
                        group_id="main",
                    ),
                    distance=1,
                    path_metadata={"record_kind": "node", "record_type": "observation"},
                ),
                TraversalHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="claim:2",
                        record_type="claim",
                        repo_id="demo",
                        group_id="main",
                    ),
                    distance=1,
                    path_metadata={"record_kind": "node", "record_type": "claim"},
                ),
                TraversalHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.EDGE,
                        record_id=relation_record_id,
                        record_type="claim_relation",
                        repo_id="demo",
                        group_id="main",
                    ),
                    distance=1,
                    path_metadata={
                        "record_kind": "edge",
                        "record_type": "claim_relation",
                        "relationship_type": "CONTRADICTS",
                        "source_record_id": "claim:2",
                        "target_record_id": "claim:1",
                    },
                ),
            ],
        )

        timeline = provenance_tools.get_timeline(
            service=provenance_tools.TimelineQueryService(driver=driver),
            repo_id="demo",
            target_id="claim:1",
            limit=10,
        )

        self.assertEqual(timeline["status"], "implemented")
        self.assertTrue(timeline["events"])
        event_types = [event["event_type"] for event in timeline["events"]]
        self.assertIn("relation:contradicts", event_types)
        self.assertNotIn("observation", event_types)
        self.assertEqual(timeline["events"][0]["observed_at"], "2026-03-27T00:00:00Z")

    def test_get_timeline_returns_implemented_for_neptune(self) -> None:
        relation_record_id = "claim_relation:claim:2:claim:1:contradicts"
        driver = FakeDriver(
            [],
            {
                "claim:1": {
                    "record_id": "claim:1",
                    "record_type": "claim",
                    "repo_id": "demo",
                    "group_id": "main",
                    "properties": {
                        "claim_id": "claim:1",
                        "claim_text": "JWT audience validation is required.",
                        "latest_observed_at": "2026-03-27T00:00:00Z",
                    },
                },
                "claim:2": {
                    "record_id": "claim:2",
                    "record_type": "claim",
                    "repo_id": "demo",
                    "group_id": "main",
                    "properties": {
                        "claim_id": "claim:2",
                        "claim_text": "JWT audience validation was removed.",
                        "latest_observed_at": "2026-03-30T00:00:00Z",
                        "is_current": False,
                        "valid_to": "2026-03-31T00:00:00Z",
                    },
                },
                relation_record_id: {
                    "record_id": relation_record_id,
                    "record_type": "claim_relation",
                    "repo_id": "demo",
                    "group_id": "main",
                    "source_record_id": "claim:2",
                    "target_record_id": "claim:1",
                    "relationship_type": "CONTRADICTS",
                    "properties": {
                        "resolution_type": "contradicts",
                        "resolution_rationale": "The rollout note contradicts the required audience validation.",
                        "observed_at": "2026-03-29T00:00:00Z",
                        "is_active": False,
                        "deactivated_at": "2026-03-31T00:00:00Z",
                    },
                },
            },
            provider=BackendProvider.NEPTUNE,
            traversal_hits=[
                TraversalHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.NODE,
                        record_id="claim:2",
                        record_type="claim",
                        repo_id="demo",
                        group_id="main",
                    ),
                    distance=1,
                    path_metadata={"record_kind": "node", "record_type": "claim"},
                ),
                TraversalHit(
                    ref=GraphRecordRef(
                        record_kind=RecordKind.EDGE,
                        record_id=relation_record_id,
                        record_type="claim_relation",
                        repo_id="demo",
                        group_id="main",
                    ),
                    distance=1,
                    path_metadata={
                        "record_kind": "edge",
                        "record_type": "claim_relation",
                        "relationship_type": "CONTRADICTS",
                        "source_record_id": "claim:2",
                        "target_record_id": "claim:1",
                    },
                ),
            ],
        )

        timeline = provenance_tools.get_timeline(
            service=provenance_tools.TimelineQueryService(driver=driver),
            repo_id="demo",
            target_id="claim:1",
            limit=10,
        )

        self.assertEqual(timeline["status"], "implemented")
        self.assertTrue(timeline["events"])
        self.assertIn("relation:contradicts", [event["event_type"] for event in timeline["events"]])


class ProvenanceToolWrapperTests(unittest.IsolatedAsyncioTestCase):
    def test_removed_public_tools_are_not_exposed(self) -> None:
        self.assertFalse(hasattr(provenance_tool_module, "resolve_claim"))
        self.assertFalse(hasattr(observation_tools, "synthesize_artifact"))

    async def test_get_node_neighborhood_wrapper_requires_exact_node_id(self) -> None:
        response = await provenance_tool_module.get_node_neighborhood(
            ServerState(),
            node_id="",
            repo_id="demo",
        )

        self.assertEqual(
            response,
            {
                "error": (
                    "node_id is required and must be an exact id such as project:owner-repo, "
                    "file:owner-repo:path/to/file.py, or symbol:owner-repo:pkg.module.func"
                )
            },
        )

    async def test_get_timeline_wrapper_requires_positive_limit(self) -> None:
        response = await provenance_tool_module.get_timeline(
            ServerState(),
            target_id="claim:1",
            repo_id=None,
            limit=0,
        )

        self.assertEqual(response, {"error": "limit must be a positive integer"})

    async def test_record_agent_observation_wrapper_fills_internal_defaults_for_persist_helper(self) -> None:
        runtime = FakeRuntime()
        config = SimpleNamespace(namespace=SimpleNamespace(group_id="main"))
        state = ServerState(
            config=config,
            backend_runtime=runtime,
            metadata_snapshot_service=object(),
        )

        async def fake_persist(persist_state, request):
            self.assertIs(persist_state, state)
            self.assertEqual(request.repo_id, "demo")
            self.assertEqual(request.observation_kind, "agent_note")
            self.assertEqual(request.title, "Auth regression")
            self.assertEqual(request.source_kind, "agent")
            self.assertIsNone(request.source_id)
            self.assertIsNone(request.context_kind)
            self.assertEqual(request.context_id, "task-9")
            self.assertRegex(request.observed_at or "", r"^\d{4}-\d{2}-\d{2}T")
            self.assertEqual(request.metadata["severity"], "high")
            return {
                "status": "implemented",
                "message": "Persisted the observation and indexed it for provenance retrieval.",
                "repo_id": "demo",
                "observation_id": "observation:agent_note:demo:1",
                "task_id": "task-9",
                "claim_ids": ["claim:demo:1", "claim:demo:2"],
                "accepted": True,
                "persisted": True,
            }

        with patch.object(
            observation_tools,
            "_persist_agent_observation_request",
            fake_persist,
        ):
            response = await observation_tools.record_agent_observation(
                state,
                "demo",
                "Investigated auth regression symptoms.",
                observation_kind="agent_note",
                title="Auth regression",
                task_id="task-9",
                metadata={"severity": "high"},
            )

        self.assertEqual(response["status"], "implemented")
        self.assertTrue(response["persisted"])
        self.assertEqual(response["observation_id"], "observation:agent_note:demo:1")
        self.assertEqual(response["claim_ids"], ["claim:demo:1", "claim:demo:2"])


class ProvenanceToolAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_context_async_runs_sync_semantic_search_off_loop(self) -> None:
        driver = FakeDriver(
            hits=[],
            records={},
        )

        def semantic_search(**kwargs):
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return [
                {
                    "profile_id": "profile:claim:1",
                    "profile_kind": "semantic_claim_profile",
                    "profile_target_kind": "Claim",
                    "profile_target_id": "claim:1",
                    "value_text": "JWT audience validation is required.",
                    "score": 0.92,
                    "matched_key_text": "audience requirement",
                    "matched_key_origin": "generated_semantic",
                    "metadata": {
                        "claim_id": "claim:1",
                        "claim_text": "JWT audience validation is required.",
                    },
                }
            ]

        response = await provenance_tools.search_context_async(
            driver=driver,
            semantic_search=semantic_search,
            repo_id="demo",
            query="audience validation",
            group_id="main",
            sources=["claims"],
            limit=10,
        )

        self.assertEqual(response["status"], "implemented")
        self.assertEqual(len(response["hits"]), 1)
        self.assertEqual(response["hits"][0]["record_type"], "claim")
        self.assertEqual(response["hits"][0]["hit_kind"], "node")
        self.assertTrue(response["hits"][0]["is_active"])


if __name__ == "__main__":
    unittest.main()
