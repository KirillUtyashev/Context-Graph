# MCP Server (Context Graph)

- Exposes an MCP HTTP endpoint at `GET /mcp` (served by `fastmcp`).
- Provides a single tool, `retrieve_context(prompt: str)`, which forwards the prompt to the `lambda_retrieve` AWS Lambda and returns its response.
- Health check `GET /health`

## Setup (local)

```bash
cd mcp_server
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

You must configure **one** of the following:

- **Function URL (recommended for local dev)**: set `RETRIEVE_FUNCTION_URL` to the Lambda Function URL for `lambda_retrieve`.
- **Lambda name (SDK invoke)**: set `RETRIEVE_LAMBDA_NAME` to the Lambda function name. This requires AWS credentials with `lambda:InvokeFunction`.

Optional:

- **`RETRIEVE_TIMEOUT_SECONDS`**: timeout (seconds) for the Function URL HTTP request. Default: `25`.
- **`AWS_REGION` / `AWS_DEFAULT_REGION`**: used when invoking via AWS SDK (if not set, boto3 falls back to its normal resolution chain).

Example:

```bash
export RETRIEVE_FUNCTION_URL="https://<your-function-url-id>.lambda-url.<region>.on.aws/"
export RETRIEVE_TIMEOUT_SECONDS="25"
```

## Run

From `mcp_server/`:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Then verify:

```bash
curl -sS http://localhost:8000/health
curl -sS http://localhost:8000/
```

## Deployment (AWS)

Infrastructure provisions an EC2 instance intended to run this server. Deployment is handled via GitHub Actions + AWS SSM:

- `.github/workflows/deploy-mcp-server.yml` deploys when `mcp_server/**` changes (defaults to `dev`), with manual dispatch for `prod`.

