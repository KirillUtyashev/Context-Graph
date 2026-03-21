# GRAPHITI_PORT.md

Reference document for the graphiti-core vendored fork integration.
Describes every change made to wire graphiti into Context-Graph with
Amazon Neptune as the graph backend and AWS Bedrock (Claude LLM + Titan embeddings) as the AI provider.

---

## Architecture

```
Cursor IDE
  └─ MCP client → HTTP :8000 → EC2 (mcp-server.service, Python 3.11)
                                  └─ graphiti-core (vendored, editable at /opt/mcp_server/current/graphiti_core_src)
                                       ├─ BedrockLLMClient      (boto3 Converse API, IAM role)
                                       ├─ BedrockTitanEmbedder  (boto3 InvokeModel, amazon.titan-embed-text-v2:0, 1024 dims)
                                       ├─ NoOpCrossEncoder      (identity reranker — no OpenAI key needed)
                                       └─ NeptuneDriver         (HTTPS port 8182, no AOSS)
                                            └─ Neptune Serverless (VPC-only)
```

---

## Deployment Status — ✅ FULLY OPERATIONAL

| Component | Status |
|-----------|--------|
| graphiti-core vendored fork | ✅ `vendor/graphiti/` |
| BedrockLLMClient | ✅ `graphiti_core/llm_client/bedrock_client.py` |
| BedrockTitanEmbedder | ✅ `graphiti_core/embedder/bedrock.py` |
| NoOpCrossEncoder | ✅ `graphiti_core/cross_encoder/noop_reranker.py` |
| MCP server config | ✅ `mcp_server/config/config-neptune-bedrock.yaml` |
| EC2 instance | ✅ `i-032921bb1ebea509b` (`context-graph-dev-mcp`) |
| Python 3.11 venv | ✅ `/opt/mcp_server/venv/bin/python` → Python 3.11.14 |
| Neptune connection | ✅ `build_indices_and_constraints()` succeeded |
| MCP server HTTP | ✅ `http://3.96.188.247:8000/mcp/` |
| `get_status` | ✅ returns `"status": "ok"` |
| `add_memory` (full pipeline) | ✅ episode processed in ~57s (LLM entity extraction + Titan embeddings + Neptune write) |
| `search_memory_facts` | ✅ returns semantic search results from Neptune |
| `search_nodes` | ⚠️ returns empty — node hybrid search requires AOSS for fulltext recall (see notes) |

---

## Infrastructure

| Resource | Value |
|----------|-------|
| EC2 instance | `i-032921bb1ebea509b` (`context-graph-dev-mcp`) |
| EC2 public IP | `3.96.188.247` |
| Neptune endpoint | `context-graph-dev-neptune.cluster-cd2euaamm1l1.ca-central-1.neptune.amazonaws.com` |
| Artifacts S3 bucket | `context-graph-dev-472730591037-mcp-artifacts` |
| MCP URL | `http://3.96.188.247:8000/mcp/` |
| AWS region | `ca-central-1` |
| EC2 IAM role | `context-graph-dev-mcp-ec2-role` |

---

## New Files

| File | Purpose |
|------|---------|
| `vendor/graphiti/graphiti_core/llm_client/bedrock_client.py` | `BedrockLLMClient` — boto3 Converse API, IAM auth |
| `vendor/graphiti/graphiti_core/embedder/bedrock.py` | `BedrockEmbedder` (Cohere) + `BedrockTitanEmbedder` (Titan) — boto3 InvokeModel, IAM auth |
| `vendor/graphiti/graphiti_core/cross_encoder/noop_reranker.py` | `NoOpCrossEncoder` — identity reranker, no external API needed |
| `vendor/graphiti/mcp_server/config/config-neptune-bedrock.yaml` | MCP YAML config (neptune + bedrock + bedrock_titan embedder) |
| `vendor/graphiti/mcp_server/requirements.txt` | Pinned flat deps for EC2 pip install |
| `pyproject.toml` | Root project manifest; `graphiti-core[neptune]` editable dep |
| `scripts/deploy-mcp.sh` | Local deployment script: build artifact → S3 → SSM |
| `scripts/test-neptune.sh` | Smoke-test Neptune + MCP health via SSM |

## Modified Files

| File | What changed |
|------|-------------|
| `vendor/graphiti/mcp_server/pyproject.toml` | Local editable dep on `../` instead of PyPI |
| `vendor/graphiti/graphiti_core/driver/neptune_driver.py` | Multiple fixes — see **Neptune Driver Fixes** below |
| `vendor/graphiti/graphiti_core/embedder/__init__.py` | Added `BedrockEmbedder`, `BedrockTitanEmbedder` and their config exports |
| `vendor/graphiti/graphiti_core/llm_client/__init__.py` | Added `BedrockLLMClient` export |
| `vendor/graphiti/graphiti_core/search/search_utils.py` | Fixed 4 `AOSS KeyError: 'hits'` crashes — guarded `res.get('hits',...)` when AOSS not configured |
| `vendor/graphiti/mcp_server/src/config/schema.py` | Added `NeptuneProviderConfig`, `BedrockEmbedderProviderConfig` |
| `vendor/graphiti/mcp_server/src/services/factories.py` | Added `bedrock` LLM, `neptune` DB, `bedrock_cohere` embedder, `bedrock_titan` embedder factory cases |
| `vendor/graphiti/mcp_server/src/graphiti_mcp_server.py` | `neptune` driver init, `NoOpCrossEncoder`, `get_status` health check uses `execute_query` (not `async for`) |
| `infra/modules/mcp_ec2/main.tf` | Removed `bedrock_invoke_restrict_models` deny policy (was blocking Cohere/Titan); added Marketplace IAM permissions |
| `infra/stacks/dev/variables.tf` | `bedrock_allowed_model_ids` includes `us.anthropic.claude-sonnet-4-6`, `amazon.titan-embed-text-v2:0` |
| `.github/workflows/deploy-mcp-server.yml` | Fixed paths trigger; artifact bundles graphiti-core source |

---

## Key Design Notes

### Editable install + artifact bundling

The deploy artifact (`tar.gz`) contains `mcp_server/` contents **plus** the graphiti-core source at `graphiti_core_src/`. On EC2 the deploy script does:

```bash
pip install -e /opt/mcp_server/current/graphiti_core_src[neptune]
pip install -r /opt/mcp_server/current/requirements.txt
```

**AL2023 fixes applied:**
- `python3.11` used for venv (AL2023 ships 3.9 by default; graphiti-core needs ≥3.10)
- Deploy script detects and deletes any existing 3.9 venv before recreating with 3.11
- `AWS_DEFAULT_REGION` added to systemd unit (botocore reads `AWS_DEFAULT_REGION`, not `AWS_REGION`)
- argparse `--config` default changed to `None` so `CONFIG_PATH` env var set in systemd unit isn't overwritten at startup
- `NoOpCrossEncoder` avoids `OpenAIRerankerClient` which requires `OPENAI_API_KEY`

### AOSS is optional

`NeptuneDriver(host=..., aoss_host=None)` — BM25/keyword search disabled; semantic similarity + graph traversal work normally. `search_nodes` uses a hybrid (fulltext + semantic) strategy and returns empty without AOSS. `search_memory_facts` uses semantic similarity only and works correctly.

### Neptune Driver Fixes

The `NeptuneDriver` required several fixes to work correctly with the graphiti-core search pipeline:

1. **`_database` attribute missing** — Added `_database: str = ''` class attribute to `NeptuneDriver` to satisfy graphiti-core's internal attribute access.

2. **`get_status` health check crash** — The `async for` loop in `get_status` expected a Neo4j async-iterable but Neptune's driver returns a plain tuple. Fixed by switching health check to `driver.execute_query()`.

3. **`execute_query` parameter handling** — `search_utils.py` calls `execute_query(query, params=filter_params, search_vector=..., limit=..., routing_='r')`. The `langchain_aws.NeptuneGraph.query()` silently drops the `params` dict. Fixed `execute_query` to:
   - Extract `params` kwarg as the Cypher parameter dict
   - Always merge `limit` into params (since queries use `$limit`)
   - Strip graphiti-internal kwargs (`routing_`, `search_vector`, `min_score`)

4. **Parameters not sent to Neptune** — `langchain_aws.NeptuneGraph.query()` calls `execute_open_cypher_query(openCypherQuery=query)` with **no parameters**. Fixed `_run_query` to call the underlying boto3 client directly: `self.client.client.execute_open_cypher_query(openCypherQuery=query, parameters=json.dumps(params))`.

5. **`np.float64` not JSON-serializable** — Cosine similarity scores are `numpy.float64`. Added a `_json_default` encoder that converts numpy scalars to Python floats before serializing params.

6. **AOSS `KeyError: 'hits'`** — `search_utils.py` accessed `res['hits']['total']['value']` directly after an AOSS query. When AOSS isn't configured `run_aoss_query` returns `{}`, causing `KeyError`. Fixed all 4 call sites to use `res.get('hits', {}).get('total', {}).get('value', 0)`.

### Bedrock Models

| Model | Use | Notes |
|-------|-----|-------|
| `us.anthropic.claude-sonnet-4-6` | LLM (Converse API) | Cross-region inference profile — required in `ca-central-1`; direct model ID not available on-demand |
| `amazon.titan-embed-text-v2:0` | Embedder (1024 dims) | Amazon-native — no Marketplace subscription required |

> **Why Titan instead of Cohere?** `cohere.embed-english-v3` requires a one-time AWS Marketplace subscription acceptance in the Bedrock console (Account → Model access). Until that is completed, `InvokeModel` returns `AccessDeniedException` with a Marketplace error. Amazon Titan Embeddings v2 is available to all Bedrock-enabled accounts without any subscription step.
>
> To switch back to Cohere once subscribed: update `embedder.provider` in `config-neptune-bedrock.yaml` to `bedrock_cohere` and `embedder.model` to `cohere.embed-english-v3`.

---

## Connecting Cursor to the MCP Server

```json
// ~/.cursor/mcp.json
{
  "mcpServers": {
    "graphiti": {
      "url": "http://3.96.188.247:8000/mcp/"
    }
  }
}
```

> **Note:** The EC2 public IP may change if the instance is restarted. Re-check with:
> ```bash
> aws ec2 describe-instances --region ca-central-1 \
>   --instance-ids i-032921bb1ebea509b \
>   --query "Reservations[0].Instances[0].PublicIpAddress" --output text
> ```

---

## Future Redeployment

When redeploying from scratch (new instance or after `terraform apply`), the `user_data` script handles everything automatically. To manually redeploy the application code:

```bash
# From repo root with AWS credentials active:
bash scripts/deploy-mcp.sh

# Smoke test:
bash scripts/test-neptune.sh
```

> **Important:** `deploy-mcp.sh` bundles the **local** copies of all patched files into the artifact. The patches in `vendor/graphiti/graphiti_core/` are not upstream — they live only in this repo. Always deploy from this repo.

---

## Next Steps

### High Priority

1. **Accept Cohere Marketplace subscription** — Go to the [Bedrock console → Model access](https://ca-central-1.console.aws.amazon.com/bedrock/home?region=ca-central-1#/model-access) and click "Enable" for `Cohere Embed English v3`. Once done, switch `config-neptune-bedrock.yaml` back to `bedrock_cohere`. Cohere embeddings have stronger multilingual performance. Titan is fully functional as an alternative.

2. **Run `terraform apply`** — The Terraform config was updated (removed deny policy, added Marketplace IAM, updated model IDs, switched to Titan). Apply so future instance replacements reflect these changes:
   ```bash
   terraform -chdir=infra/stacks/dev apply
   ```

3. **Provision AOSS** — Without OpenSearch (AOSS), `search_nodes` returns empty because fulltext BM25 search is disabled. Add an AOSS collection to `infra/modules/` and set `aoss_host` in `config-neptune-bedrock.yaml`. The `NeptuneDriver` already supports it — just needs the host URL.

4. **Elastic IP for EC2** — Assign an Elastic IP so the MCP URL doesn't change on restart. Add to `infra/modules/mcp_ec2/main.tf`.

### Integration

5. **Wire `context_graph/graph_sinks.py` to graphiti** — The current code pushes nodes/edges directly to Neptune via raw queries. Add a graphiti sink that calls `graphiti.add_episode()` for each indexed file/commit, so graphiti extracts entities and builds the temporal knowledge graph on top of Neptune.

6. **Define entity types** — Create Pydantic models for domain entities (`Repository`, `File`, `Function`, `Author`, `Commit`). Pass as `custom_types` to `Graphiti()`. Document in `config-neptune-bedrock.yaml` under `graphiti.entity_types`.

7. **Agent transcript ingestion** — Implement the agent transcript → graphiti episode pipeline. Each conversation turn becomes: `add_episode(content=<turn>, source=EpisodeType.message)`.

8. **GitHub Actions OIDC** — Set `AWS_ROLE_TO_ASSUME` secret in the repo to automate deploy on push to `main` (workflow at `.github/workflows/deploy-mcp-server.yml`).
