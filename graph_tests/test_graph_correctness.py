"""
Trail / Context-Graph correctness tests — schema-accurate version.

Real schema (discovered from live Neo4j):
  Nodes:   CodeNode (+ sublabels Function, Variable, File, Module, Folder, Project)
           CodeNodeVersion  (revision_id = commit SHA, node_id = parent CodeNode's node_id)
           Commit           (sha, group_id, repo_id)
           Observation      (source_kind, observation_kind, group_id, source_id)
           PullRequest      (number, group_id, repo_id, state, metadata_json)

  Rels:    TOUCHED             Commit  -> CodeNode
           HAS_VERSION         CodeNode -> CodeNodeVersion
           INTRODUCED_VERSION  Commit  -> CodeNodeVersion
           INVALIDATED_VERSION Commit  -> CodeNodeVersion
           HAS_PULL_REQUEST    -> PullRequest
           FROM_SOURCE         Observation -> Commit | PullRequest

  LLM-only (require --llm-provider, skipped here):
           Episode, MENTIONS, RELATES_TO
"""

import json
import pytest
from neo4j import GraphDatabase

NEO4J_URI      = "bolt://localhost:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "contextgraph"
REPO_ID        = "test-harness"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def driver():
    d = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    yield d
    d.close()


@pytest.fixture(scope="session")
def q(driver):
    def _run(cypher, **params):
        with driver.session() as s:
            return [dict(r) for r in s.run(cypher, **params)]
    return _run


@pytest.fixture(scope="session")
def repo_id():
    return REPO_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _code_node_exists(q, repo_id, name, file_path_fragment):
    rows = q(
        """
        MATCH (n:CodeNode)
        WHERE n.group_id   = $repo_id
          AND n.name       = $name
          AND n.file_path  CONTAINS $fp
        RETURN count(n) AS cnt
        """,
        repo_id=repo_id, name=name, fp=file_path_fragment,
    )
    return rows[0]["cnt"] > 0


def _pr_merge_sha(q, repo_id, pr_number):
    rows = q(
        "MATCH (p:PullRequest) WHERE p.group_id=$repo_id AND p.number=$n RETURN p.metadata_json AS m",
        repo_id=repo_id, n=pr_number,
    )
    if not rows:
        return None
    return json.loads(rows[0]["m"]).get("merge_commit_sha")


def _pr_head_sha(q, repo_id, pr_number):
    rows = q(
        "MATCH (p:PullRequest) WHERE p.group_id=$repo_id AND p.number=$n RETURN p.metadata_json AS m",
        repo_id=repo_id, n=pr_number,
    )
    if not rows:
        return None
    return json.loads(rows[0]["m"]).get("head_sha")


# ---------------------------------------------------------------------------
# Category A — Entity Resolution
# ---------------------------------------------------------------------------

class TestEntityResolution:

    def test_TC01_init_db_function_node_exists(self, q, repo_id):
        assert _code_node_exists(q, repo_id, "init_db", "src/db.py"), (
            "HARD FAIL: No CodeNode named 'init_db' in src/db.py."
        )

    def test_TC01_pr1_indexed_as_pull_request(self, q, repo_id):
        rows = q("MATCH (p:PullRequest) WHERE p.group_id=$r AND p.number=1 RETURN count(p) AS cnt", r=repo_id)
        assert rows[0]["cnt"] == 1, "HARD FAIL: PullRequest #1 not indexed."

    def test_TC02_DB_URL_global_var_node_exists(self, q, repo_id):
        assert _code_node_exists(q, repo_id, "DB_URL", "src/db.py"), (
            "HARD FAIL: No CodeNode named 'DB_URL' in src/db.py."
        )

    def test_TC03_MAX_RETRIES_global_var_node_exists(self, q, repo_id):
        assert _code_node_exists(q, repo_id, "MAX_RETRIES", "src/tasks.py"), (
            "HARD FAIL: No CodeNode named 'MAX_RETRIES' in src/tasks.py."
        )

    def test_TC04_SECRET_KEY_global_var_node_exists(self, q, repo_id):
        assert _code_node_exists(q, repo_id, "SECRET_KEY", "src/auth.py"), (
            "HARD FAIL: No CodeNode named 'SECRET_KEY' in src/auth.py."
        )

    def test_TC04_pr2_indexed_as_pull_request(self, q, repo_id):
        rows = q("MATCH (p:PullRequest) WHERE p.group_id=$r AND p.number=2 RETURN count(p) AS cnt", r=repo_id)
        assert rows[0]["cnt"] == 1, "HARD FAIL: PullRequest #2 not indexed."

    def test_TC05_APP_PORT_global_var_node_exists(self, q, repo_id):
        assert _code_node_exists(q, repo_id, "APP_PORT", "src/config.py"), (
            "HARD FAIL: No CodeNode named 'APP_PORT' in src/config.py."
        )

    def test_TCERR01_no_duplicate_init_db_nodes(self, q, repo_id):
        rows = q(
            """
            MATCH (n:CodeNode)
            WHERE n.group_id = $r AND n.name = 'init_db' AND n.file_path CONTAINS 'db.py'
            RETURN count(n) AS cnt
            """,
            r=repo_id,
        )
        cnt = rows[0]["cnt"]
        assert cnt == 1, (
            f"HARD FAIL: Expected 1 node for init_db in db.py, found {cnt}."
        )


# ---------------------------------------------------------------------------
# Category B — Version-Aware Edges
# ---------------------------------------------------------------------------

class TestVersionAwareEdges:

    @pytest.fixture(scope="class")
    def pr1_shas(self, q, repo_id):
        return {
            "merge": _pr_merge_sha(q, repo_id, 1),
            "head":  _pr_head_sha(q, repo_id, 1),
        }

    @pytest.fixture(scope="class")
    def pr2_shas(self, q, repo_id):
        return {
            "merge": _pr_merge_sha(q, repo_id, 2),
            "head":  _pr_head_sha(q, repo_id, 2),
        }

    def test_TC06_db_py_touched_by_pr1(self, q, repo_id, pr1_shas):
        """PR #1 modified db.py — at least one of its commits must TOUCH db.py."""
        shas = [s for s in pr1_shas.values() if s]
        for sha in shas:
            rows = q(
                """
                MATCH (c:Commit)-[:TOUCHED]->(n:CodeNode)
                WHERE c.group_id=$r AND c.sha=$sha AND n.file_path CONTAINS 'db.py'
                RETURN count(c) AS cnt
                """,
                r=repo_id, sha=sha,
            )
            if rows[0]["cnt"] > 0:
                return
        pytest.fail(
            f"HARD FAIL: Neither PR #1 merge ({pr1_shas['merge'][:8] if pr1_shas['merge'] else 'N/A'}) "
            f"nor head ({pr1_shas['head'][:8] if pr1_shas['head'] else 'N/A'}) "
            "has a TOUCHED edge to db.py."
        )

    def test_TC06_db_py_has_version_at_pr1(self, q, repo_id, pr1_shas):
        """A CodeNodeVersion for db.py must exist at a PR #1 commit."""
        shas = [s for s in pr1_shas.values() if s]
        rows = q(
            """
            MATCH (n:CodeNode)-[:HAS_VERSION]->(v:CodeNodeVersion)
            WHERE n.group_id=$r AND n.file_path CONTAINS 'db.py'
              AND v.revision_id IN $shas
            RETURN count(v) AS cnt
            """,
            r=repo_id, shas=shas,
        )
        assert rows[0]["cnt"] > 0, (
            "HARD FAIL: No CodeNodeVersion for db.py at PR #1 commits. "
            f"Checked SHAs: {[s[:8] for s in shas]}"
        )

    def test_TC07_tasks_py_has_version_at_pr1(self, q, repo_id, pr1_shas):
        """tasks.py was changed in PR #1 (MAX_RETRIES 3→5) — version must exist."""
        shas = [s for s in pr1_shas.values() if s]
        rows = q(
            """
            MATCH (n:CodeNode)-[:HAS_VERSION]->(v:CodeNodeVersion)
            WHERE n.group_id=$r AND n.file_path CONTAINS 'tasks.py'
              AND v.revision_id IN $shas
            RETURN count(v) AS cnt
            """,
            r=repo_id, shas=shas,
        )
        assert rows[0]["cnt"] > 0, (
            "HARD FAIL: No CodeNodeVersion for tasks.py at PR #1 commits. "
            "MAX_RETRIES change (3→5) not captured."
        )

    def test_TC08_tasks_py_has_no_version_at_pr2(self, q, repo_id, pr2_shas):
        """PR #2 did NOT modify tasks.py — no CodeNodeVersion for tasks.py at PR #2."""
        shas = [s for s in pr2_shas.values() if s]
        rows = q(
            """
            MATCH (n:CodeNode)-[:HAS_VERSION]->(v:CodeNodeVersion)
            WHERE n.group_id=$r AND n.file_path CONTAINS 'tasks.py'
              AND v.revision_id IN $shas
            RETURN count(v) AS cnt
            """,
            r=repo_id, shas=shas,
        )
        assert rows[0]["cnt"] == 0, (
            f"HARD FAIL: Found CodeNodeVersion for tasks.py at PR #2 commits "
            f"{[s[:8] for s in shas]}, but PR #2 did not modify tasks.py."
        )


# ---------------------------------------------------------------------------
# Category C — Temporal Invalidation (LLM-only)
# ---------------------------------------------------------------------------

class TestTemporalInvalidation:

    @pytest.mark.skip(reason="Requires --llm-provider (RELATES_TO facts only exist with LLM)")
    def test_TC09_APP_PORT_has_active_fact(self, q, repo_id):
        pass

    @pytest.mark.skip(reason="Requires --llm-provider (RELATES_TO facts only exist with LLM)")
    def test_TC09_APP_PORT_active_fact_is_load_balancer(self, q, repo_id):
        pass

    @pytest.mark.skip(reason="Requires --llm-provider (RELATES_TO facts only exist with LLM)")
    def test_TC09_APP_PORT_firewall_fact_is_invalidated(self, q, repo_id):
        pass

    def test_TC09_no_simultaneous_contradictions(self, q, repo_id):
        """
        Structural integrity: no CodeNodeVersion can be simultaneously
        INTRODUCED_VERSION and INVALIDATED_VERSION by the same commit.
        """
        rows = q(
            """
            MATCH (c:Commit)-[:INTRODUCED_VERSION]->(v:CodeNodeVersion)
            MATCH (c)-[:INVALIDATED_VERSION]->(v)
            WHERE c.group_id = $r
            RETURN count(v) AS cnt
            """,
            r=repo_id,
        )
        assert rows[0]["cnt"] == 0, (
            f"{rows[0]['cnt']} CodeNodeVersion(s) are simultaneously introduced "
            "AND invalidated by the same commit — graph integrity error."
        )


# ---------------------------------------------------------------------------
# Category D — Negative (unmerged branch must not appear)
# ---------------------------------------------------------------------------

class TestUnmergedBranch:

    def test_TC10_cache_py_file_node_does_not_exist(self, q, repo_id):
        rows = q(
            "MATCH (n:CodeNode) WHERE n.group_id=$r AND n.file_path CONTAINS 'cache.py' RETURN count(n) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] == 0, "HARD FAIL: cache.py CodeNode exists — unmerged branch leaked."

    def test_TC10_cache_query_function_does_not_exist(self, q, repo_id):
        rows = q(
            "MATCH (n:CodeNode) WHERE n.group_id=$r AND n.name='cache_query' RETURN count(n) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] == 0, "HARD FAIL: cache_query function node exists."

    def test_TC10_get_cached_function_does_not_exist(self, q, repo_id):
        rows = q(
            "MATCH (n:CodeNode) WHERE n.group_id=$r AND n.name='get_cached' RETURN count(n) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] == 0, "HARD FAIL: get_cached function node exists."

    def test_TC10_CACHE_TTL_SECONDS_does_not_exist(self, q, repo_id):
        rows = q(
            "MATCH (n:CodeNode) WHERE n.group_id=$r AND n.name='CACHE_TTL_SECONDS' RETURN count(n) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] == 0, "HARD FAIL: CACHE_TTL_SECONDS variable node exists."

    def test_TC10_no_pr3_pull_request_node(self, q, repo_id):
        rows = q(
            "MATCH (p:PullRequest) WHERE p.group_id=$r AND p.number=3 RETURN count(p) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] == 0, "HARD FAIL: PullRequest #3 (unmerged) was indexed."


# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------

class TestSanity:

    def test_at_least_3_commits_indexed(self, q, repo_id):
        rows = q("MATCH (c:Commit) WHERE c.group_id=$r RETURN count(c) AS cnt", r=repo_id)
        assert rows[0]["cnt"] >= 3, f"Only {rows[0]['cnt']} commit(s). Expected ≥3."

    def test_src_db_py_file_node_exists(self, q, repo_id):
        rows = q(
            "MATCH (n:CodeNode) WHERE n.group_id=$r AND n.file_path CONTAINS 'src/db.py' RETURN count(n) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] > 0, "No CodeNode for src/db.py."

    def test_at_least_2_pull_requests_indexed(self, q, repo_id):
        rows = q("MATCH (p:PullRequest) WHERE p.group_id=$r RETURN count(p) AS cnt", r=repo_id)
        assert rows[0]["cnt"] >= 2, f"Only {rows[0]['cnt']} PR(s). Expected ≥2."

    def test_at_least_one_commit_observation(self, q, repo_id):
        rows = q("MATCH (o:Observation) WHERE o.group_id=$r RETURN count(o) AS cnt", r=repo_id)
        assert rows[0]["cnt"] >= 1, "No Observations found."

    def test_at_least_one_code_node_version(self, q, repo_id):
        rows = q("MATCH (v:CodeNodeVersion) WHERE v.group_id=$r RETURN count(v) AS cnt", r=repo_id)
        assert rows[0]["cnt"] > 0, "No CodeNodeVersions found."

    def test_functions_and_variables_indexed(self, q, repo_id):
        rows = q(
            "MATCH (n:CodeNode) WHERE n.group_id=$r AND (n:Function OR n:Variable) RETURN count(n) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] > 0, "No Function or Variable nodes found."
