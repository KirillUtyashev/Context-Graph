# Deployment

This guide covers deploying the Context Graph MCP server to an EC2 instance using the project's CI/CD pipeline and Terraform-provisioned infrastructure.

## Runtime Dependencies

The MCP server requires two packages installed into the same Python virtual environment:

| Package | Path | Role |
|---------|------|------|
| `context-graph` | `context_graph/` | Core library — storage drivers, ingestion, provenance, query |
| `context-graph-mcp` | `mcp_server/` | Standalone MCP application (entry point: `context-graph-mcp`) |

Both are installed as editable packages during deployment. The deploy script builds a tarball containing both source trees and installs them on the target instance.

## EC2 Deployment Model

Infrastructure is provisioned by Terraform in `infra/`. Each environment (`dev`, `prod`) gets a `t3.micro` EC2 instance with:

- A **systemd** unit (`mcp-server.service`) that runs the MCP server process under a dedicated `mcp` user.
- **SSM (AWS Systems Manager)** access for remote management — no SSH keys are needed.
- Public ingress on port `8000`.

The deploy script (`scripts/deploy-mcp.sh`) performs the following steps:

1. Packages `context_graph/` and `mcp_server/` into a versioned tarball.
2. Uploads the tarball and a remote deploy script to S3.
3. Triggers an SSM `RunShellScript` command on the target EC2 instance.
4. On the instance, the remote script extracts the release, creates (or reuses) a virtualenv at `/opt/mcp_server/venv`, installs both packages, and restarts the systemd service.

Releases are stored under `/opt/mcp_server/releases/<sha>/` with a `/opt/mcp_server/current` symlink pointing to the active release.

## GitHub Actions Workflow

Automated deployment is handled by [`.github/workflows/deploy-mcp-server.yml`](../.github/workflows/deploy-mcp-server.yml).

**Automatic triggers** — pushes to `main` that touch any of these paths:

- `mcp_server/**`
- `context_graph/**`
- `scripts/deploy-mcp.sh`
- `infra/**`

**Manual dispatch** — use the `workflow_dispatch` trigger to deploy a specific environment (`dev` or `prod`).

The workflow assumes an OIDC role (`AWS_ROLE_TO_ASSUME` secret) and runs `scripts/deploy-mcp.sh --env <env>` to perform the deployment.

## Configuration

The MCP server reads a YAML configuration file at startup. Point the app at it with the `--config` flag:

```bash
context-graph-mcp --config /path/to/config.yaml
```

### Configuration Sections

| Section | Purpose |
|---------|---------|
| `server` | Transport type (`http`), bind host, and port |
| `llm` | LLM provider, model ID, and provider-specific credentials |
| `embedder` | Embedding provider, model, and dimensions |
| `database` | Graph database provider and connection details |
| `namespace` | Tenant isolation settings (`group_id`, `user_id`, entity types) |

Environment variables can be interpolated using `${VAR_NAME}` or `${VAR_NAME:default}` syntax.

### Example Configurations

See `mcp_server/config/` for ready-made examples:

| File | Description |
|------|-------------|
| `config.yaml` | Default — OpenAI LLM/embeddings, local Neo4j |
| `config-docker-neo4j.yaml` | Docker-based Neo4j setup |
| `config-neptune-bedrock.yaml` | AWS Neptune graph + Bedrock LLM/embeddings |
| `mcp_config_stdio_example.json` | MCP client configuration for stdio transport |
