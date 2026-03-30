# Infra (Terraform)

This directory provisions AWS infrastructure for the Context Graph.

## What we create (per environment: `dev`, `prod`)

- VPC (`10.0.0.0/16`) across 2 AZs in `ca-central-1`
  - Public subnets (EC2, NAT gateway)
  - Private subnets (Aurora, Neptune, Lambdas)
  - 1 NAT Gateway (so VPC Lambdas can reach GitHub / internet)
- Bedrock Runtime Interface VPC Endpoint (PrivateLink)
  - Lets EC2/Lambdas call `bedrock-runtime` privately inside the VPC
- Aurora PostgreSQL (Serverless v2)
  - For storing context resources
- Neptune (Gremlin) inside the VPC
  - For storing the graph itself
- EC2 `t3.micro` MCP server instance
  - Public ingress on port `8000` (course-project-simple)
  - Managed via SSM (no SSH keys needed)
- 2 AWS Lambda functions with Lambda Function URLs (no API Gateway)
  - `augment`: GitHub webhook receiver / augmentation entrypoint
  - `retrieve`: Called by the tool to traverse the graph
  - `bedrock-proxy`: Public internet endpoint that calls Bedrock with HTTP Basic Auth
    - Mostly for manual calling

## Lambda source code location + deployment

The Terraform stacks still contain legacy lambda references. This repo no longer ships a matching
top-level lambda source tree or deploy workflow, so lambda deployment should be treated as a
follow-up cleanup/migration task rather than a working source of truth.

## MCP server code location + deployment

The MCP server app code lives outside `infra/`:

- `mcp_server/` (standalone MCP app)

Terraform only provisions the EC2 instance + systemd unit + SSM permissions.

Deployment happens via GitHub Actions + SSM:

- `.github/workflows/deploy-mcp-server.yml`
  - On push to `main` touching `mcp_server/**` or `context_graph/**`: deploys **dev**
  - Manual dispatch can deploy **prod**

## Remote Terraform state (one-time bootstrap)

Need to bootstrap a bucket for terraform state to store in

Create backend resources:
```bash
cd infra/bootstrap
terraform init
terraform apply
```

Initialize dev/prod stacks with the created backend:

```bash
bash infra/scripts/init_backends.sh
```

(Optionally) Can be done manually:

```bash
cd infra/stacks/dev
terraform init \
  -backend-config="bucket=<bootstrap output tf_state_bucket_name>" \
  -backend-config="dynamodb_table=<bootstrap output tf_lock_table_name>" \
  -backend-config="region=ca-central-1" \
  -backend-config="key=context-graph/dev/terraform.tfstate"
```

(Same for `infra/stacks/prod` but with `key=context-graph/prod/terraform.tfstate`.)

Apply dev/prod:

```bash
cd infra/stacks/dev
terraform apply
```

```bash
cd infra/stacks/prod
terraform apply
```

## Bedrock (Claude Sonnet 4.6, on‑demand)

This stack gives the MCP EC2 instance IAM permissions to call Amazon Bedrock Runtime using on‑demand pricing (no provisioned throughput).

The endpoint can also be called manually using basic auth.

Terraform settings (per stack `terraform.tfvars`):

- `bedrock_allowed_model_ids`: allowed `modelId` values (defaults to Claude Sonnet 4.6 + Claude Opus 4.6)
- `bedrock_public_proxy_basic_auth_username`: username for the public Bedrock proxy Function URL
- `bedrock_public_proxy_basic_auth_password`: password for the public Bedrock proxy Function URL (if empty, Terraform generates one)

### Call the model

From within the VPC (like on the EC2) you can run:

```python
import json
import boto3

MODEL_ID = "global.anthropic.claude-sonnet-4-6"  # or an inference profile ID/ARN
REGION = "ca-central-1"

brt = boto3.client("bedrock-runtime", region_name=REGION)

resp = brt.converse(
    modelId=MODEL_ID,
    messages=[
        {"role": "user", "content": [{"text": "Explain on-demand pricing in one sentence."}]}
    ],
    inferenceConfig={"maxTokens": 128, "temperature": 0.2},
)

print(json.dumps(resp["output"]["message"], indent=2))
```

Alternatively, you can call it through the AWS CLI:

```bash
aws bedrock-runtime converse \
  --region <bedrock-region> \
  --model-id "global.anthropic.claude-sonnet-4-6" \
  --messages '[{"role":"user","content":[{"text":"Write a haiku about graphs."}]}]' \
  --inference-config '{"maxTokens":256,"temperature":0.7}'
```

Or, you can call the model via the public proxy (curl + Basic Auth)

After `terraform apply`, read these outputs from your stack:

- `bedrock_public_proxy_function_url`
- `bedrock_public_proxy_basic_auth_username`
- `bedrock_public_proxy_basic_auth_password`

Example request (uses the proxy's `DEFAULT_MODEL_ID` if `model_id` is omitted):

```bash
curl -u "<user>:<pass>" \
  -H "content-type: application/json" \
  -d '{"prompt":"Write a haiku about graphs.","inferenceConfig":{"maxTokens":128,"temperature":0.7}}' \
  "<function_url>/"
```

You can also pass through a Bedrock `converse` call directly:

```bash
curl -u "<user>:<pass>" \
  -H "content-type: application/json" \
  -d '{"model_id":"anthropic.claude-sonnet-4-6","converse":{"messages":[{"role":"user","content":[{"text":"Say hello."}]}],"inferenceConfig":{"maxTokens":64,"temperature":0.2}}}' \
  "<function_url>/"
```

## Notes

- Neptune is VPC-only, so we query it from the EC2 MCP instance or from VPC-attached Lambdas.
- Aurora is private, so we connect to it from the EC2 MCP instance (or later via a dedicated bastion)
