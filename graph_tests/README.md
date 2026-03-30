# Graph Correctness Tests

Tests the Trail ingestion pipeline by firing Cypher queries directly against a local Neo4j instance
after ingesting [`context-graph-test-harness-new2`](https://github.com/patelhrid/context-graph-test-harness-new2).

---

## Prerequisites

- Docker Desktop running with the Trail stack started (Neo4j + Postgres)
- Python 3.12 (the `context_graph` package requires 3.12, **not** 3.10)
- A GitHub personal access token with `repo` read scope
- `pip install -r graph_tests/requirements.txt` (uses the Python 3.10 test runner — that is fine, only the ingest uses 3.12)

### Start the local stack

```bash
docker compose -f context_graph/docker-compose.local.yml up -d
```

Services started:
- **PostgreSQL** — `localhost:5433`, db/user/pass: `context_graph`
- **Neo4j** — bolt: `localhost:7687`, browser: `http://localhost:7474`, user: `neo4j`, pass: `contextgraph`

---

## Step 1 — Ingest the test harness repo

> **Important — macOS SSL fix (one-time):**
> Python 3.12 ships without macOS root certificates installed. Run this once:
> ```bash
> open /Applications/Python\ 3.12/Install\ Certificates.command
> ```
> If ingestion still fails with `CERTIFICATE_VERIFY_FAILED`, add `PYTHONHTTPSVERIFY=0` as shown below.

> **Important — cache dir must be outside OneDrive:**
> If your project lives inside `~/Library/CloudStorage/OneDrive-*`, OneDrive will interfere with git clone
> operations and silently restore deleted directories. Always use `/tmp` for both the cache and the
> metadata database.

```bash
# Clean up any stale state first
rm -rf /tmp/context-graph-cache
rm -f  /tmp/test-harness.sqlite

# Ingest (deterministic mode — no LLM required)
GITHUB_TOKEN='<your_token>' PYTHONHTTPSVERIFY=0 \
python3 -m context_graph hosted-index \
  --github-url https://github.com/patelhrid/context-graph-test-harness-new2 \
  --repo-id test-harness \
  --repo-cache-dir /tmp/context-graph-cache \
  --metadata-db /tmp/test-harness.sqlite \
  --graph-backend neo4j \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password contextgraph \
  --execute
```

### Optional — with LLM fact extraction (enables TC-09 tests)

```bash
GITHUB_TOKEN='<your_token>' OPENAI_API_KEY='<your_key>' PYTHONHTTPSVERIFY=0 \
python3 -m context_graph hosted-index \
  --github-url https://github.com/patelhrid/context-graph-test-harness-new2 \
  --repo-id test-harness \
  --repo-cache-dir /tmp/context-graph-cache \
  --metadata-db /tmp/test-harness.sqlite \
  --graph-backend neo4j \
  --neo4j-uri bolt://localhost:7687 \
  --neo4j-user neo4j \
  --neo4j-password contextgraph \
  --llm-provider openai \
  --llm-model gpt-4o \
  --execute
```

### Re-ingesting from scratch

```bash
rm -rf /tmp/context-graph-cache
rm -f  /tmp/test-harness.sqlite
# Wipe Neo4j via the browser at http://localhost:7474:
#   MATCH (n) DETACH DELETE n
# Then re-run the ingest command above.
```

---

## Step 2 — Run the tests

```bash
# Run all tests (TC-09 skipped automatically without LLM ingestion)
pytest graph_tests/ -v

# Skip TC-09 explicitly
pytest graph_tests/ -v -k "not TC09"

# Run a single category
pytest graph_tests/ -v -k "EntityResolution"
pytest graph_tests/ -v -k "VersionAware"
pytest graph_tests/ -v -k "Invalidation"
pytest graph_tests/ -v -k "Unmerged"
pytest graph_tests/ -v -k "Sanity"
```

---

## Test harness repo structure

The repo (`context-graph-test-harness-new2`) has exactly 3 commits on `main` and 2 merged PRs:

| Commit | Description |
|--------|-------------|
| Initial | `src/db.py`, `src/auth.py`, `src/tasks.py`, `src/config.py`, `tests/` |
| PR #1 merge | Adds `CONNECTION_TIMEOUT=30` to `db.py`; bumps `MAX_RETRIES` 3→5 in `tasks.py` |
| PR #2 merge | Rotates `SECRET_KEY` in `auth.py`; changes `APP_PORT` 8080→9000 in `config.py` |

PR #1 body mentions `init_db()` in `db.py` and `DB_URL` in `db.py`.  
PR #1 comment mentions `MAX_RETRIES` in `tasks.py`.  
PR #2 body says the `APP_PORT` change was due to a firewall (deliberately wrong).  
PR #2 comment corrects this: "load balancer misconfiguration, not a firewall" — this is the temporal invalidation test.

There is also an **unmerged** branch `feature/add-query-cache` with open PR #3 that adds `src/cache.py`.
This content must **never** appear in the graph after ingesting `main`.

---

## Real Neo4j schema (discovered empirically)

The pipeline uses these labels and relationships — the tests query against these directly.

### Node labels

| Label | Description |
|-------|-------------|
| `CodeNode` | Base label — all code entities carry this |
| `Function` | Subtype of CodeNode — functions and methods |
| `Variable` | Subtype of CodeNode — global variables and constants |
| `File` | Subtype of CodeNode — source files |
| `Module` | Subtype of CodeNode — Python modules |
| `Folder` | Subtype of CodeNode — directories |
| `Project` | Subtype of CodeNode — the repo root |
| `CodeNodeVersion` | A snapshot of a CodeNode at a specific commit (`revision_id` = SHA) |
| `Commit` | A git commit node (`sha`, `group_id`) |
| `Observation` | A commit message or PR-derived text record |
| `PullRequest` | A GitHub PR node (`number`, `group_id`, `metadata_json`) |

### Key properties used in tests

| Node | Properties |
|------|------------|
| `CodeNode` | `name`, `file_path`, `group_id`, `kind` |
| `CodeNodeVersion` | `revision_id` (commit SHA), `node_id`, `group_id`, `file_path` |
| `Commit` | `sha`, `group_id` |
| `PullRequest` | `number`, `group_id`, `state`, `metadata_json` (contains `merge_commit_sha` and `head_sha`) |
| `Observation` | `source_kind`, `observation_kind`, `group_id`, `source_id` |

> **Note:** The correct property is `group_id`, not `repo_id` (both are stored but `group_id` is the canonical key).  
> The correct property is `file_path`, not `file`.

### Relationships

| Relationship | From → To | Meaning |
|--------------|-----------|---------|
| `TOUCHED` | `Commit` → `CodeNode` | This commit modified this file or symbol |
| `HAS_VERSION` | `CodeNode` → `CodeNodeVersion` | This node has a snapshot at this revision |
| `INTRODUCED_VERSION` | `Commit` → `CodeNodeVersion` | This commit first introduced this version |
| `INVALIDATED_VERSION` | `Commit` → `CodeNodeVersion` | This commit superseded this version |
| `HAS_PULL_REQUEST` | `Project` → `PullRequest` | Repo has this PR |
| `ASSOCIATED_WITH_PR` | `Commit` → `PullRequest` | This commit belongs to this PR |
| `FROM_SOURCE` | `Observation` → `Commit` or `PullRequest` | This observation came from this source |
| `HAS_COMMIT` | `Project` → `Commit` | Repo has this commit |
| `DEFINES` | `Module` → `Function` or `Variable` | Module defines this symbol |

### LLM-only (require `--llm-provider`)

These do **not exist** without LLM ingestion. TC-09 tests are `@pytest.mark.skip` by default.

| Label or Rel | Meaning |
|--------------|---------|
| `Episode` | An extracted semantic unit from PR body or comment text |
| `MENTIONS` | `Episode` → `CodeNode` — entity resolution edge |
| `RELATES_TO` | Semantic fact edge with `fact`, `group_id`, `expired_at` properties |

---

## Test catalogue

### Category A — Entity Resolution

Verifies that the right `CodeNode` symbols were indexed and that PR nodes exist.

| Test | TC | What it checks | Fails if |
|------|----|----------------|----------|
| `test_TC01_init_db_function_node_exists` | TC-01 | `Function` node for `init_db` in `src/db.py` exists | Symbol not indexed |
| `test_TC01_pr1_indexed_as_pull_request` | TC-01 | `PullRequest` with `number=1` exists | PR ingestion skipped |
| `test_TC02_DB_URL_global_var_node_exists` | TC-02 | `Variable` node for `DB_URL` in `src/db.py` exists | Resolved to file only |
| `test_TC03_MAX_RETRIES_global_var_node_exists` | TC-03 | `Variable` node for `MAX_RETRIES` in `src/tasks.py` exists | Symbol not indexed |
| `test_TC04_SECRET_KEY_global_var_node_exists` | TC-04 | `Variable` node for `SECRET_KEY` in `src/auth.py` exists | Symbol not indexed |
| `test_TC04_pr2_indexed_as_pull_request` | TC-04 | `PullRequest` with `number=2` exists | PR ingestion skipped |
| `test_TC05_APP_PORT_global_var_node_exists` | TC-05 | `Variable` node for `APP_PORT` in `src/config.py` exists | Symbol not indexed |
| `test_TCERR01_no_duplicate_init_db_nodes` | TC-ERR-01 | Exactly 1 `init_db` node in `db.py` | Duplicate nodes created |

### Category B — Version-Aware Edges

Verifies that `CodeNodeVersion` snapshots exist at the right commit SHAs.
SHAs are read from `PullRequest.metadata_json` at runtime (both `merge_commit_sha` and `head_sha` are
checked because GitHub merge commits may not directly carry file diffs — the head/squash commit does).

| Test | TC | What it checks | Fails if |
|------|----|----------------|----------|
| `test_TC06_db_py_touched_by_pr1` | TC-06 | PR #1 commit has `TOUCHED` → `db.py` | File change not recorded |
| `test_TC06_db_py_has_version_at_pr1` | TC-06 | `CodeNodeVersion` for `db.py` exists at PR #1 SHA | Version not created |
| `test_TC07_tasks_py_has_version_at_pr1` | TC-07 | `CodeNodeVersion` for `tasks.py` exists at PR #1 SHA | `MAX_RETRIES` 3→5 change not versioned |
| `test_TC08_tasks_py_has_no_version_at_pr2` | TC-08 | No `CodeNodeVersion` for `tasks.py` at PR #2 SHA | Pipeline spuriously versioned an untouched file |

### Category C — Temporal Invalidation (LLM-only)

These tests require `--llm-provider openai --llm-model gpt-4o` to pass.
Without LLM ingestion they are permanently `@pytest.mark.skip` and will never produce a spurious failure.

| Test | TC | What it checks | Fails if |
|------|----|----------------|----------|
| `test_TC09_APP_PORT_has_active_fact` *(skipped)* | TC-09 | At least one active `RELATES_TO` fact for `APP_PORT` | Fact extraction failed |
| `test_TC09_APP_PORT_active_fact_is_load_balancer` *(skipped)* | TC-09 | Active fact text mentions "load balancer" or "misconfiguration" | Firewall fact kept instead of the correction |
| `test_TC09_APP_PORT_firewall_fact_is_invalidated` *(skipped)* | TC-09 | Firewall fact has `expired_at` set | PR body fact not superseded by the correcting comment |
| `test_TC09_no_simultaneous_contradictions` | TC-09 | No `CodeNodeVersion` both introduced AND invalidated by the same commit | Graph integrity error |

### Category D — Unmerged Branch (negative test)

Verifies that content from the unmerged `feature/add-query-cache` branch (PR #3) does not appear
in the graph after ingesting `main`.

| Test | TC | What it checks |
|------|----|----------------|
| `test_TC10_cache_py_file_node_does_not_exist` | TC-10 | No `CodeNode` with `file_path` containing `cache.py` |
| `test_TC10_cache_query_function_does_not_exist` | TC-10 | No `CodeNode` named `cache_query` |
| `test_TC10_get_cached_function_does_not_exist` | TC-10 | No `CodeNode` named `get_cached` |
| `test_TC10_CACHE_TTL_SECONDS_does_not_exist` | TC-10 | No `CodeNode` named `CACHE_TTL_SECONDS` |
| `test_TC10_no_pr3_pull_request_node` | TC-10 | No `PullRequest` with `number=3` |

### Sanity

Basic smoke checks that ingestion ran at all and the graph is not empty.

| Test | What it checks |
|------|----------------|
| `test_at_least_3_commits_indexed` | At least 3 `Commit` nodes (initial + PR#1 merge + PR#2 merge) |
| `test_src_db_py_file_node_exists` | At least one `CodeNode` with `file_path` containing `src/db.py` |
| `test_at_least_2_pull_requests_indexed` | At least 2 `PullRequest` nodes (PR #1 and PR #2) |
| `test_at_least_one_commit_observation` | At least 1 `Observation` node (commit message was captured) |
| `test_at_least_one_code_node_version` | At least 1 `CodeNodeVersion` (version-aware indexing ran) |
| `test_functions_and_variables_indexed` | At least 1 `Function` or `Variable` node (symbol extraction ran) |

---

## Expected results (without LLM)

```
Category A — Entity Resolution      8 tests   all PASS
Category B — Version-Aware Edges    4 tests   all PASS
Category C — Temporal Invalidation  4 tests   3 SKIP, 1 PASS
Category D — Unmerged Branch        5 tests   all PASS
Sanity                              6 tests   all PASS
```

---

## Troubleshooting

**`TimeoutError [Errno 60]` during ingestion**  
OneDrive is syncing the `.cache` directory. Always use `--repo-cache-dir /tmp/context-graph-cache`.

**`fatal: destination path ... already exists`**  
A previous failed clone is still there. Run `rm -rf /tmp/context-graph-cache` and retry.

**`CERTIFICATE_VERIFY_FAILED`**  
Run `/Applications/Python\ 3.12/Install\ Certificates.command` once, or prepend `PYTHONHTTPSVERIFY=0` to the ingest command.

**`ModuleNotFoundError` or `TimeoutError` on first import of `context_graph`**  
The package files are cloud-only placeholders in OneDrive. In Finder, right-click the `Context-Graph` folder → **Always Keep on This Device** and wait for sync to finish before retrying.

**Tests show 0 commits, 0 CodeNodes**  
Ingestion failed or was never run. Check for errors in the ingest output. Confirm Neo4j is up:
```bash
docker ps | grep neo4j
```

**`UnknownLabelWarning` or `UnknownPropertyKeyWarning` in test output**  
These Neo4j warnings are harmless — Neo4j logs them whenever a query references a label or property that has not been written yet (i.e. the graph is empty or the ingest did not produce that data). They appear alongside real `AssertionError`s when ingestion failed. The current test file has been verified against the live schema produced by a successful ingest.
