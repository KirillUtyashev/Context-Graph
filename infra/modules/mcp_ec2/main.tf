data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

locals {
  bucket_name_prefix    = lower(replace(var.name, "_", "-"))
  artifacts_bucket_name = "${local.bucket_name_prefix}-${data.aws_caller_identity.current.account_id}-mcp-artifacts"
}

resource "aws_s3_bucket" "artifacts" {
  bucket        = local.artifacts_bucket_name
  force_destroy = true

  tags = merge(var.tags, { Name = local.artifacts_bucket_name })
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_security_group" "mcp" {
  name        = "${var.name}-mcp-sg"
  description = "MCP server SG"
  vpc_id      = var.vpc_id

  ingress {
    description = "Public MCP HTTP"
    from_port   = var.ingress_port
    to_port     = var.ingress_port
    protocol    = "tcp"
    cidr_blocks = [var.ingress_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name}-mcp-sg" })
}

resource "aws_iam_role" "ssm" {
  name = "${var.name}-mcp-ec2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect    = "Allow",
      Action    = "sts:AssumeRole",
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

  tags = merge(var.tags, { Name = "${var.name}-mcp-ec2-role" })
}

resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "artifacts_read" {
  name = "${var.name}-mcp-artifacts-read"
  role = aws_iam_role.ssm.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid      = "ListArtifactsBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.artifacts.arn
        Condition = {
          StringLike = {
            "s3:prefix" = ["mcp-server/*"]
          }
        }
      },
      {
        Sid    = "ReadArtifactsObjects"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:GetObjectVersion"
        ]
        Resource = "${aws_s3_bucket.artifacts.arn}/mcp-server/*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "invoke_retrieve_lambda" {
  name = "${var.name}-mcp-invoke-retrieve"
  role = aws_iam_role.ssm.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid    = "InvokeRetrieveLambda"
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [
          "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.name}-retrieve",
          "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.name}-retrieve:*"
        ]
      }
    ]
  })
}

locals {
  # Build Bedrock resource ARNs for the allowed model IDs.
  # Bedrock uses two different ARN patterns:
  #   - Foundation models:   arn:aws:bedrock:*::foundation-model/<id>
  #   - Inference profiles:  arn:aws:bedrock:*:*:inference-profile/<id>
  # Inference profiles (e.g. us.anthropic.*) are required for cross-region throughput
  # and use account-scoped ARNs; foundation-model ARNs are global (no account).
  # We emit both patterns for every entry so that InvokeModel and Converse work.
  _bedrock_model_arns = length(var.bedrock_allowed_model_ids) > 0 ? flatten([
    for m in var.bedrock_allowed_model_ids : [
      "arn:aws:bedrock:*::foundation-model/${m}",
      "arn:aws:bedrock:*:*:inference-profile/${m}"
    ]
  ]) : ["*"]
}

resource "aws_iam_role_policy" "bedrock_invoke" {
  count = var.enable_bedrock ? 1 : 0

  name = "${var.name}-mcp-bedrock-invoke"
  role = aws_iam_role.ssm.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid    = "InvokeBedrockOnDemand"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:Converse",
          "bedrock:ConverseStream",
          "bedrock:CountTokens"
        ]
        # Scope to specific model ARNs when a list is provided; otherwise all models.
        Resource = local._bedrock_model_arns
      }
    ]
  })
}

# The old Deny-based restriction policy was removed. The Deny approach using
# StringNotEqualsIfExists broke the Converse API because bedrock:ModelId is
# not set by Converse (only bedrock:InferredModelId is). Scoping the Allow
# Resource directly is the correct mechanism.
resource "aws_iam_role_policy" "bedrock_invoke_restrict_models" {
  count = 0 # disabled — model scoping is now handled via Allow Resource ARNs above

  name = "${var.name}-mcp-bedrock-invoke-restrict-models"
  role = aws_iam_role.ssm.id

  policy = jsonencode({ Version = "2012-10-17", Statement = [] })
}

resource "aws_iam_role_policy" "aurora_secret_read" {
  count = var.aurora_password_secret_arn != "" ? 1 : 0

  name = "${var.name}-mcp-aurora-secret-read"
  role = aws_iam_role.ssm.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid    = "ReadAuroraSecret"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [var.aurora_password_secret_arn]
      }
    ]
  })
}

resource "aws_iam_role_policy" "aoss_api_access" {
  count = var.aoss_host != "" ? 1 : 0

  name = "${var.name}-mcp-aoss-api-access"
  role = aws_iam_role.ssm.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid    = "AOSSAPIAccess"
        Effect = "Allow"
        Action = ["aoss:APIAccessAll"]
        # Scope to the specific collection ARN when provided; fall back to "*"
        # (the AOSS data access policy in the opensearch_aoss module provides
        # the fine-grained per-index authorization in that case).
        Resource = var.aoss_collection_arn != "" ? [var.aoss_collection_arn] : ["*"]
      }
    ]
  })
}

resource "aws_iam_instance_profile" "this" {
  name = "${var.name}-mcp-ec2-profile"
  role = aws_iam_role.ssm.name
}

resource "aws_instance" "this" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = "t3.medium"
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [aws_security_group.mcp.id]
  iam_instance_profile   = aws_iam_instance_profile.this.name

  user_data = <<-EOF
    #!/bin/bash
    set -euo pipefail

    # Base runtime. graphiti-core requires Python >=3.10; AL2023 default is 3.9.
    # Do NOT install 'curl' — it conflicts with curl-minimal on AL2023.
    dnf update -y
    dnf install -y python3.11 python3.11-pip awscli tar

    # Directories for the deployed app
    mkdir -p /opt/mcp_server/current
    mkdir -p /opt/mcp_server/releases

    # Create a dedicated user
    id -u mcp >/dev/null 2>&1 || useradd -r -s /sbin/nologin mcp
    chown -R mcp:mcp /opt/mcp_server

    # Systemd unit — written with python and printf to avoid heredoc indentation
    # issues caused by the outer <<-EOF stripping leading tabs but not spaces.
    python3 -c "
import textwrap, pathlib
unit = textwrap.dedent('''
    [Unit]
    Description=Context Graph MCP Server
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    User=mcp
    Group=mcp
    WorkingDirectory=/opt/mcp_server/current
    Environment=PORT=${var.ingress_port}
    Environment=AWS_REGION=${data.aws_region.current.name}
    Environment=AWS_DEFAULT_REGION=${data.aws_region.current.name}
    Environment=RETRIEVE_LAMBDA_NAME=${var.name}-retrieve
    Environment=NEPTUNE_HOST=neptune-db://${var.neptune_endpoint}
    Environment=CONFIG_PATH=/opt/mcp_server/current/config/config-neptune-bedrock.yaml
    Environment=AURORA_ENDPOINT=${var.aurora_endpoint}
    Environment=AURORA_DB_NAME=${var.aurora_db_name}
    Environment=AURORA_USERNAME=${var.aurora_username}
    Environment=AURORA_PASSWORD_SECRET_ARN=${var.aurora_password_secret_arn}
    Environment=AOSS_HOST=${var.aoss_host}
    Environment=CONTEXT_GRAPH_LLM_PROVIDER=bedrock
    ExecStart=/opt/mcp_server/venv/bin/python /opt/mcp_server/current/main.py
    Restart=on-failure
    RestartSec=2

    [Install]
    WantedBy=multi-user.target
''').lstrip()
pathlib.Path('/etc/systemd/system/mcp-server.service').write_text(unit)
"

    systemctl daemon-reload

    # deploy-mcp-server.sh is downloaded from S3 by the GitHub Actions workflow before invocation.
    # Write a placeholder that prints a clear error if called before the real script is deployed.
    cat > /usr/local/bin/deploy-mcp-server.sh <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
usage() { echo "Usage: deploy-mcp-server.sh <artifacts-bucket> <sha>" >&2; exit 2; }
bucket="$${1:-}"; sha="$${2:-}"
[[ -n "$${bucket}" && -n "$${sha}" ]] || usage
key="mcp-server/$${sha}.tar.gz"
release_dir="/opt/mcp_server/releases/$${sha}"
mkdir -p /opt/mcp_server/releases
rm -rf "$${release_dir}"; mkdir -p "$${release_dir}"
aws s3 cp "s3://$${bucket}/$${key}" /tmp/mcp_server.tar.gz
tar -xzf /tmp/mcp_server.tar.gz -C "$${release_dir}"
rm -rf /opt/mcp_server/current
ln -sfn "$${release_dir}" /opt/mcp_server/current
id -u mcp >/dev/null 2>&1 || useradd -r -s /sbin/nologin mcp
chown -R mcp:mcp /opt/mcp_server
PYTHON_BIN="python3"; for py in python3.12 python3.11; do command -v $py &>/dev/null && PYTHON_BIN=$py && break; done
echo "==> Using $PYTHON_BIN ($($PYTHON_BIN --version))"
$PYTHON_BIN -m venv /opt/mcp_server/venv
/opt/mcp_server/venv/bin/pip install --upgrade pip --quiet
CONTEXT_GRAPH_SRC="/opt/mcp_server/current/context_graph_src"
if [ -d "$${CONTEXT_GRAPH_SRC}" ]; then
  echo "==> Installing context_graph package..."
  /opt/mcp_server/venv/bin/pip install -e "$${CONTEXT_GRAPH_SRC}" --quiet
  for SP in $(/opt/mcp_server/venv/bin/python -c "import site; [print(p) for p in site.getsitepackages()]"); do
    echo "$${CONTEXT_GRAPH_SRC}" > "$${SP}/_context_graph_src.pth" 2>/dev/null || true
  done
fi
/opt/mcp_server/venv/bin/pip install -r /opt/mcp_server/current/requirements.txt --quiet
/opt/mcp_server/venv/bin/python -c "from context_graph.storage.backend.neptune.driver import NeptuneBackendDriver; print('context_graph storage import OK')"
/opt/mcp_server/venv/bin/python -c "from context_graph.server.mcp.server import GraphitiService; print('context_graph mcp import OK')"
systemctl daemon-reload
systemctl enable --now mcp-server.service
systemctl restart mcp-server.service
echo "==> Deployment complete"
systemctl status mcp-server.service --no-pager -l | tail -n 20
SCRIPT
    chmod 0755 /usr/local/bin/deploy-mcp-server.sh

    systemctl daemon-reload
  EOF

  tags = merge(var.tags, { Name = "${var.name}-mcp" })
}

resource "aws_eip" "mcp" {
  domain = "vpc"

  tags = merge(var.tags, { Name = "${var.name}-mcp-eip" })
}

resource "aws_eip_association" "mcp" {
  instance_id   = aws_instance.this.id
  allocation_id = aws_eip.mcp.id
}
