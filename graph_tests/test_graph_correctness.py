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
# Category A -- Entity Resolution
#
# These tests verify that the ingestion pipeline correctly extracted Python
# symbols from source files and wrote them as CodeNode records in Neo4j.
# Each test targets a specific symbol that must survive the full pipeline:
# file parsing -> snapshot diffing -> mutation planning -> graph write.
# ---------------------------------------------------------------------------

class TestEntityResolution:

    def test_TC01_init_db_function_node_exists(self, q, repo_id):
        # Positive test: init_db is a function defined in the initial commit of db.py.
        # Edge case: a function present from the very first commit could be missed if the
        # planner only processes diffs and skips the baseline snapshot entirely.
        assert _code_node_exists(q, repo_id, "init_db", "src/db.py"), (
            "HARD FAIL: No CodeNode named 'init_db' in src/db.py."
        )

    def test_TC01_pr1_indexed_as_pull_request(self, q, repo_id):
        # Positive test: confirms that PR ingestion ran and wrote a PullRequest node.
        # Edge case: PR metadata fetching is a separate code path from commit ingestion;
        # a failure here means provenance links from observations to PRs will be broken.
        rows = q("MATCH (p:PullRequest) WHERE p.group_id=$r AND p.number=1 RETURN count(p) AS cnt", r=repo_id)
        assert rows[0]["cnt"] == 1, "HARD FAIL: PullRequest #1 not indexed."

    def test_TC02_DB_URL_global_var_node_exists(self, q, repo_id):
        # Positive test: DB_URL is a module-level constant in db.py.
        # Edge case: module-level constants may be misclassified as file-level metadata
        # rather than discrete Variable nodes, meaning symbol-level querying would miss them.
        assert _code_node_exists(q, repo_id, "DB_URL", "src/db.py"), (
            "HARD FAIL: No CodeNode named 'DB_URL' in src/db.py."
        )

    def test_TC03_MAX_RETRIES_global_var_node_exists(self, q, repo_id):
        # Positive test: MAX_RETRIES is mutated by PR #1 (value 3 -> 5).
        # Edge case: identity-stability -- the pipeline must update the existing node
        # rather than delete-and-recreate it, or the node may vanish after re-ingestion.
        assert _code_node_exists(q, repo_id, "MAX_RETRIES", "src/tasks.py"), (
            "HARD FAIL: No CodeNode named 'MAX_RETRIES' in src/tasks.py."
        )

    def test_TC04_SECRET_KEY_global_var_node_exists(self, q, repo_id):
        # Positive test: SECRET_KEY is rotated in PR #2 (value change in auth.py).
        # Edge case: same identity-stability concern as TC-03 but across a different
        # file and a different PR, ensuring the planner handles both files consistently.
        assert _code_node_exists(q, repo_id, "SECRET_KEY", "src/auth.py"), (
            "HARD FAIL: No CodeNode named 'SECRET_KEY' in src/auth.py."
        )

    def test_TC04_pr2_indexed_as_pull_request(self, q, repo_id):
        # Positive test: paired with TC04 above to confirm PR #2 was not silently skipped.
        # Edge case: if PR ingestion fails partway through, only PR #1 may be written.
        rows = q("MATCH (p:PullRequest) WHERE p.group_id=$r AND p.number=2 RETURN count(p) AS cnt", r=repo_id)
        assert rows[0]["cnt"] == 1, "HARD FAIL: PullRequest #2 not indexed."

    def test_TC05_APP_PORT_global_var_node_exists(self, q, repo_id):
        # Positive test: APP_PORT changes value in PR #2 (8080 -> 9000) in config.py.
        # Edge case: APP_PORT is also referenced in a conflicting PR description and
        # correcting comment; the CodeNode must exist regardless of any fact-extraction
        # behaviour layered on top of it.
        assert _code_node_exists(q, repo_id, "APP_PORT", "src/config.py"), (
            "HARD FAIL: No CodeNode named 'APP_PORT' in src/config.py."
        )

    def test_TCERR01_no_duplicate_init_db_nodes(self, q, repo_id):
        # Negative test / idempotency: re-running ingestion on the same repo must not
        # append a second init_db node. Asserts count == 1 exactly, so the test catches
        # both the missing-node case (count == 0) and the duplicate case (count > 1).
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
# Category B -- Version-Aware Edges
#
# These tests verify that CodeNodeVersion snapshots are created at the correct
# commit SHAs and that untouched files do NOT receive spurious new versions.
#
# Because GitHub merge commits may not directly carry file diffs (the diff
# can land on the squash/head commit instead), every test checks BOTH the
# merge_commit_sha AND the head_sha extracted from PullRequest.metadata_json
# at runtime, rather than hardcoding any SHA.
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
        # Positive test: PR #1 modified db.py (added CONNECTION_TIMEOUT=30).
        # Edge case: the TOUCHED relationship is written by a separate code path from
        # HAS_VERSION; this test ensures the commit->file edge was recorded independently
        # of whether a full version snapshot was created.
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
        # Positive test: a CodeNodeVersion for db.py must exist at a PR #1 commit SHA.
        # Edge case: it is possible to record a TOUCHED edge without actually creating
        # the version snapshot; this test verifies the snapshotting step ran separately.
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
        # Positive test: tasks.py was also changed in PR #1 (MAX_RETRIES 3 -> 5).
        # Edge case: a per-file bug might snapshot db.py correctly but silently skip
        # tasks.py; testing both files in the same PR guards against that narrow failure.
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
            "MAX_RETRIES change (3->5) not captured."
        )

    def test_TC08_tasks_py_has_no_version_at_pr2(self, q, repo_id, pr2_shas):
        # Negative test: PR #2 did NOT modify tasks.py, so no CodeNodeVersion for
        # tasks.py should exist at any PR #2 SHA.
        # Failure mode: a pipeline bug that snapshots all files on every commit would
        # produce a spurious version here, corrupting provenance for unrelated symbols.
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
# Category C -- Graph Integrity
#
# Structural invariant that must hold regardless of ingestion configuration.
# ---------------------------------------------------------------------------

class TestTemporalInvalidation:

    def test_TC09_no_simultaneous_contradictions(self, q, repo_id):
        # Negative test / graph integrity: no CodeNodeVersion may be simultaneously
        # marked INTRODUCED_VERSION and INVALIDATED_VERSION by the same commit.
        # Failure mode: a mutation planner bug that emits both edges in a single
        # transaction would produce an incoherent provenance graph where a version
        # is recorded as both new and superseded at the same point in history.
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
            "AND invalidated by the same commit -- graph integrity error."
        )


# ---------------------------------------------------------------------------
# Category D -- Unmerged Branch (negative tests)
#
# The feature/add-query-cache branch (open PR #3) was never merged into main.
# After ingesting main, none of its symbols or PR node must appear in the graph.
# All five tests are negative: they assert count == 0.
# Failure mode in all cases: the pipeline fetches GitHub PR metadata eagerly and,
# without an explicit branch filter, could write unmerged content to the graph.
# ---------------------------------------------------------------------------

class TestUnmergedBranch:

    def test_TC10_cache_py_file_node_does_not_exist(self, q, repo_id):
        # Negative test: cache.py only exists on the unmerged feature branch.
        # A CodeNode for it must not appear after ingesting main.
        rows = q(
            "MATCH (n:CodeNode) WHERE n.group_id=$r AND n.file_path CONTAINS 'cache.py' RETURN count(n) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] == 0, "HARD FAIL: cache.py CodeNode exists -- unmerged branch leaked."

    def test_TC10_cache_query_function_does_not_exist(self, q, repo_id):
        # Negative test: cache_query is a function defined only in cache.py on the
        # unmerged branch; its presence confirms symbol-level branch leakage.
        rows = q(
            "MATCH (n:CodeNode) WHERE n.group_id=$r AND n.name='cache_query' RETURN count(n) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] == 0, "HARD FAIL: cache_query function node exists."

    def test_TC10_get_cached_function_does_not_exist(self, q, repo_id):
        # Negative test: get_cached is a second function in cache.py.
        # Testing both functions ensures leakage is caught even if only partial
        # symbol extraction ran for the unmerged file.
        rows = q(
            "MATCH (n:CodeNode) WHERE n.group_id=$r AND n.name='get_cached' RETURN count(n) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] == 0, "HARD FAIL: get_cached function node exists."

    def test_TC10_CACHE_TTL_SECONDS_does_not_exist(self, q, repo_id):
        # Negative test: CACHE_TTL_SECONDS is a module-level constant in cache.py.
        # Including a variable test alongside function tests ensures the branch filter
        # applies to all node kinds, not just Function nodes.
        rows = q(
            "MATCH (n:CodeNode) WHERE n.group_id=$r AND n.name='CACHE_TTL_SECONDS' RETURN count(n) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] == 0, "HARD FAIL: CACHE_TTL_SECONDS variable node exists."

    def test_TC10_no_pr3_pull_request_node(self, q, repo_id):
        # Negative test: PR #3 is open but unmerged. Its PullRequest node must not
        # exist in the graph. This catches the case where the pipeline indexes all
        # open PRs rather than only those merged into the ingested branch.
        rows = q(
            "MATCH (p:PullRequest) WHERE p.group_id=$r AND p.number=3 RETURN count(p) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] == 0, "HARD FAIL: PullRequest #3 (unmerged) was indexed."


# ---------------------------------------------------------------------------
# Sanity
#
# Basic smoke checks that confirm ingestion ran at all and produced a
# non-empty graph. These tests fail fast with a clear message when the
# Docker stack is down or the ingest command was never executed.
# ---------------------------------------------------------------------------

class TestSanity:

    def test_at_least_3_commits_indexed(self, q, repo_id):
        # The harness repo has exactly 3 commits on main (initial + PR#1 merge + PR#2 merge).
        rows = q("MATCH (c:Commit) WHERE c.group_id=$r RETURN count(c) AS cnt", r=repo_id)
        assert rows[0]["cnt"] >= 3, f"Only {rows[0]['cnt']} commit(s). Expected >=3."

    def test_src_db_py_file_node_exists(self, q, repo_id):
        # At least one CodeNode with a file_path containing src/db.py must exist.
        rows = q(
            "MATCH (n:CodeNode) WHERE n.group_id=$r AND n.file_path CONTAINS 'src/db.py' RETURN count(n) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] > 0, "No CodeNode for src/db.py."

    def test_at_least_2_pull_requests_indexed(self, q, repo_id):
        # PR #1 and PR #2 must both be present; fewer means PR ingestion partially failed.
        rows = q("MATCH (p:PullRequest) WHERE p.group_id=$r RETURN count(p) AS cnt", r=repo_id)
        assert rows[0]["cnt"] >= 2, f"Only {rows[0]['cnt']} PR(s). Expected >=2."

    def test_at_least_one_commit_observation(self, q, repo_id):
        # At least one Observation node must exist, confirming commit messages were captured.
        rows = q("MATCH (o:Observation) WHERE o.group_id=$r RETURN count(o) AS cnt", r=repo_id)
        assert rows[0]["cnt"] >= 1, "No Observations found."

    def test_at_least_one_code_node_version(self, q, repo_id):
        # At least one CodeNodeVersion must exist, confirming version-aware indexing ran.
        rows = q("MATCH (v:CodeNodeVersion) WHERE v.group_id=$r RETURN count(v) AS cnt", r=repo_id)
        assert rows[0]["cnt"] > 0, "No CodeNodeVersions found."

    def test_functions_and_variables_indexed(self, q, repo_id):
        # At least one Function or Variable node must exist, confirming symbol extraction ran.
        rows = q(
            "MATCH (n:CodeNode) WHERE n.group_id=$r AND (n:Function OR n:Variable) RETURN count(n) AS cnt",
            r=repo_id,
        )
        assert rows[0]["cnt"] > 0, "No Function or Variable nodes found."
