from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from context_graph.models import CanonicalEdge, CanonicalNode, GraphSnapshot
from context_graph.server.mcp import provenance_tools
from context_graph.server.mcp import server as server_module
from context_graph.server.mcp.queue import ObservationWriteResult
from context_graph.storage.backend import GraphRecordRef, RecordKind, SearchHit


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
        if query.record_types:
            return [
                hit
                for hit in self._hits
                if hit.ref.record_type in query.record_types
            ][: query.limit]
        return self._hits[: query.limit]

    async def fetch(self, refs):
        return [
            dict(self._records[ref.record_id])
            for ref in refs
            if ref.record_id in self._records
        ]


class FakeDriver:
    def __init__(self, hits: list[SearchHit], records: dict[str, dict]) -> None:
        self.search = FakeSearchOperations(hits, records)


class FakeClaimResolutionService:
    def __init__(self) -> None:
        self.llm_client = object()
        self.calls = []

    def resolve(self, *, repo_id: str, new_claim: dict, candidates: list[dict]):
        self.calls.append((repo_id, new_claim, candidates))
        return SimpleNamespace(
            relations=[
                SimpleNamespace(
                    candidate_claim_id="claim:1",
                    resolution_type="same_as",
                    confidence="high",
                    rationale="The wording differs but the assertion is the same.",
                )
            ]
        )


class FakeArtifactSynthesisService:
    def __init__(self) -> None:
        self.llm_client = object()
        self.calls = []

    def synthesize(
        self,
        *,
        repo_id: str,
        repo_region: dict,
        code_context: dict,
        evidence: list,
        claims: list,
    ):
        self.calls.append((repo_id, repo_region, code_context, evidence, claims))
        return SimpleNamespace(
            artifacts=[
                SimpleNamespace(
                    artifact_kind="research_note",
                    title="Auth audience check",
                    summary="The indexed evidence shows an audience-validation change.",
                    key_points=["JWT audience validation was introduced."],
                )
            ]
        )


class FakeRuntime:
    def __init__(
        self,
        *,
        driver=None,
        llm_client=None,
        claim_resolution_service=None,
        research_artifact_synthesis_service=None,
        queue_service=None,
        observation_write_service=None,
    ) -> None:
        self._driver = driver
        self.llm_client = llm_client
        self.claim_resolution_service = claim_resolution_service
        self.research_artifact_synthesis_service = research_artifact_synthesis_service
        self.queue_service = queue_service
        self.observation_write_service = observation_write_service

    async def ensure_initialized(self) -> None:
        return None

    async def get_driver(self):
        return self._driver


class FakeQueueService:
    def __init__(self, result: ObservationWriteResult) -> None:
        self.result = result
        self.calls = []

    async def enqueue_observation_write(self, request, *, write_service):
        self.calls.append((request, write_service))
        return self.result


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
        self.assertEqual(response["requested_sources"], ["claims", "code"])
        self.assertEqual([hit["record_type"] for hit in response["hits"]], ["claim", "code_node"])
        self.assertIn("Semantic profile search is unavailable", response["message"])
        self.assertEqual(self.driver.search.search_queries[0].mode.value, "lexical")
        self.assertEqual(response["hits"][0]["retrieval_channels"], ["lexical"])
        self.assertIsNotNone(response["hits"][0]["lexical_score"])
        self.assertIsNone(response["hits"][0]["semantic_score"])

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
        self.assertEqual(hit["retrieval_channels"], ["lexical", "semantic_claim"])
        self.assertEqual(hit["matched_profile_key_text"], "audience requirement")
        self.assertEqual(hit["matched_profile_key_origin"], "generated_semantic")
        self.assertEqual(hit["semantic_profile_kind"], "semantic_claim_profile")
        self.assertIn("hybrid provenance retrieval", response["message"])

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

    def test_resolve_claim_queries_candidates_and_maps_relations(self) -> None:
        service = FakeClaimResolutionService()

        response = provenance_tools.resolve_claim(
            driver=self.driver,
            claim_resolution_service=service,
            repo_id="demo",
            group_id="main",
            claim_text="JWT audience validation is required.",
            claim_kind="state_claim",
            claim_subkind="desired_behavior_or_requirement",
            candidate_limit=5,
        )

        self.assertEqual(response["status"], "implemented")
        self.assertEqual(response["matches"][0]["relation"], "same_as")
        self.assertEqual(service.calls[0][0], "demo")
        self.assertEqual(self.driver.search.search_queries[0].group_ids, ("main",))

    def test_synthesize_artifact_returns_read_only_artifact(self) -> None:
        service = FakeArtifactSynthesisService()

        response = provenance_tools.synthesize_artifact(
            driver=self.driver,
            artifact_synthesis_service=service,
            snapshot=self.snapshot,
            repo_id="demo",
            group_id="main",
            subject_kind="symbol",
            subject_id="demo.app.main",
            artifact_kind="research_note",
            include_code_context=True,
        )

        self.assertEqual(response["status"], "implemented")
        self.assertEqual(response["title"], "Auth audience check")
        self.assertEqual(response["sections"][0]["title"], "Key Point 1")
        self.assertIn("TODO(context_graph.backend.artifacts.persistence)", response["message"])

    def test_scaffold_helpers_include_capability_and_todo(self) -> None:
        neighborhood = provenance_tools.get_node_neighborhood(
            service=provenance_tools.NeighborhoodQueryService(),
            repo_id="demo",
            node_id="claim:1",
            concept=None,
        )
        timeline = provenance_tools.get_timeline(
            service=provenance_tools.TimelineQueryService(),
            repo_id="demo",
            target_kind="topic",
            target_id="auth",
        )

        self.assertEqual(neighborhood["status"], "scaffold")
        self.assertIn("capability", neighborhood)
        self.assertIn("todo", neighborhood)
        self.assertEqual(timeline["status"], "scaffold")
        self.assertIn("capability", timeline)
        self.assertIn("todo", timeline)


class ProvenanceToolWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_claim_wrapper_requires_llm_provider(self) -> None:
        runtime = FakeRuntime(
            driver=object(),
            llm_client=None,
            claim_resolution_service=None,
        )
        config = SimpleNamespace(namespace=SimpleNamespace(group_id="main"))

        with patch.object(server_module, "backend_runtime", runtime), patch.object(
            server_module,
            "config",
            config,
            create=True,
        ):
            response = await server_module.resolve_claim(
                "demo",
                "JWT audience validation is required.",
            )

        self.assertEqual(
            response,
            {"error": "resolve_claim requires a configured llm provider"},
        )

    async def test_synthesize_artifact_wrapper_requires_llm_provider(self) -> None:
        runtime = FakeRuntime(
            driver=object(),
            llm_client=None,
            research_artifact_synthesis_service=None,
        )
        config = SimpleNamespace(namespace=SimpleNamespace(group_id="main"))

        with patch.object(server_module, "backend_runtime", runtime), patch.object(
            server_module,
            "config",
            config,
            create=True,
        ):
            response = await server_module.synthesize_artifact(
                "demo",
                "symbol",
                "demo.app.main",
            )

        self.assertEqual(
            response,
            {"error": "synthesize_artifact requires a configured llm provider"},
        )

    async def test_record_agent_observation_wrapper_returns_scaffold_payload(self) -> None:
        queue_result = ObservationWriteResult(
            repo_id="demo",
            group_id="main",
            accepted=True,
            persisted=False,
            observation_id=None,
            queue_position=1,
            message="record_agent_observation is scaffolded; the request was accepted but not persisted.",
        )
        runtime = FakeRuntime(
            queue_service=FakeQueueService(queue_result),
            observation_write_service=object(),
        )
        config = SimpleNamespace(namespace=SimpleNamespace(group_id="main"))

        with patch.object(server_module, "backend_runtime", runtime), patch.object(
            server_module,
            "config",
            config,
            create=True,
        ):
            response = await server_module.record_agent_observation(
                "demo",
                "Investigated auth regression symptoms.",
                task_id="task-1",
            )

        self.assertEqual(response["status"], "scaffold")
        self.assertTrue(response["accepted"])
        self.assertFalse(response["persisted"])
        self.assertEqual(response["queue_position"], 1)
        self.assertIn("todo", response)

    async def test_record_agent_observation_wrapper_passes_extended_fields_to_persist_helper(self) -> None:
        runtime = FakeRuntime(queue_service=None, observation_write_service=None)
        config = SimpleNamespace(namespace=SimpleNamespace(group_id="main"))

        async def fake_persist(request):
            self.assertEqual(request.repo_id, "demo")
            self.assertEqual(request.observation_kind, "agent_note")
            self.assertEqual(request.title, "Auth regression")
            self.assertEqual(request.source_kind, "agent")
            self.assertEqual(request.source_id, "session-7")
            self.assertEqual(request.context_kind, "agent_task")
            self.assertEqual(request.context_id, "task-9")
            self.assertEqual(request.observed_at, "2026-03-29T00:00:00Z")
            self.assertEqual(request.metadata["severity"], "high")
            return {
                "status": "implemented",
                "message": "Persisted the observation and indexed it for provenance retrieval.",
                "repo_id": "demo",
                "observation_id": "observation:agent_note:demo:1",
                "task_id": "task-9",
                "accepted": True,
                "persisted": True,
            }

        with patch.object(server_module, "backend_runtime", runtime), patch.object(
            server_module,
            "metadata_snapshot_service",
            object(),
            create=True,
        ), patch.object(
            server_module,
            "config",
            config,
            create=True,
        ), patch.object(
            server_module,
            "_persist_agent_observation_request",
            fake_persist,
        ):
            response = await server_module.record_agent_observation(
                "demo",
                "Investigated auth regression symptoms.",
                observation_kind="agent_note",
                title="Auth regression",
                source_kind="agent",
                source_id="session-7",
                context_kind="agent_task",
                context_id="task-9",
                observed_at="2026-03-29T00:00:00Z",
                task_id="task-9",
                metadata={"severity": "high"},
            )

        self.assertEqual(response["status"], "implemented")
        self.assertTrue(response["persisted"])
        self.assertEqual(response["observation_id"], "observation:agent_note:demo:1")


if __name__ == "__main__":
    unittest.main()
