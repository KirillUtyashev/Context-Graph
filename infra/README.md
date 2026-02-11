# Infra (Terraform)

This directory provisions AWS infrastructure for the Context Graph.

## What we create (per environment: `dev`, `prod`)

- VPC (`10.0.0.0/16`) across 2 AZs in `ca-central-1`
  - Public subnets (EC2, NAT gateway)
  - Private subnets (Aurora, Neptune, Lambdas)
  - 1 NAT Gateway (so VPC Lambdas can reach GitHub / internet)
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

## Lambda source code location + deployment

Lambda source lives outside `infra/`:

- `lambda_augment/`
- `lambda_retrieve/`

Terraform packages these directories when you run `terraform apply`.

We also include a GitHub Actions workflow (`.github/workflows/deploy-lambdas.yml`) that can redeploy Lambda code on push (defaults to `dev`), or via manual dispatch for `prod`.

## MCP server code location + deployment

The MCP server app code lives outside `infra/`:

- `mcp_server/` (FastAPI app)

Terraform only provisions the EC2 instance + systemd unit + SSM permissions.

Deployment happens via GitHub Actions + SSM:

- `.github/workflows/deploy-mcp-server.yml`
  - On push to `main` touching `mcp_server/**`: deploys **dev**
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

## Notes

- Neptune is VPC-only, so we query it from the EC2 MCP instance or from VPC-attached Lambdas.
- Aurora is private, so we connect to it from the EC2 MCP instance (or later via a dedicated bastion)
