"""
Shared fixtures for graph correctness tests.

Before running:
  1. Start local services:
       docker compose -f context_graph/docker-compose.local.yml up -d
  2. Ingest the test harness repo:
       python3 -m context_graph hosted-index \
         --github-url https://github.com/patelhrid/context-graph-test-harness-new2 \
         --repo-id test-harness \
         --repo-cache-dir .cache/context-graph \
         --metadata-db /tmp/test-harness.sqlite \
         --graph-backend neo4j \
         --neo4j-uri bolt://localhost:7687 \
         --neo4j-user neo4j \
         --neo4j-password contextgraph \
         --llm-provider openai \
         --llm-model gpt-4o \
         --execute
  3. Run:
       pytest graph_tests/ -v
"""

import pytest
from neo4j import GraphDatabase

# ── Connection config ─────────────────────────────────────────────────────────
NEO4J_URI      = "bolt://localhost:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "contextgraph"

# Must match the --repo-id you used when ingesting
REPO_ID = "test-harness"

# The GitHub repo being tested
GITHUB_REPO = "patelhrid/context-graph-test-harness-new2"


@pytest.fixture(scope="session")
def driver():
    """Single Neo4j driver reused across the whole test session."""
    drv = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    yield drv
    drv.close()


@pytest.fixture(scope="session")
def q(driver):
    """Convenience: run a Cypher query and return all records as dicts."""
    def _run(cypher: str, **params) -> list[dict]:
        with driver.session() as session:
            result = session.run(cypher, **params)
            return [dict(r) for r in result]
    return _run


@pytest.fixture(scope="session")
def repo_id():
    return REPO_ID
