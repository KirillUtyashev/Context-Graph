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
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "bedrock_invoke_restrict_models" {
  count = (var.enable_bedrock && length(var.bedrock_allowed_model_ids) > 0) ? 1 : 0

  name = "${var.name}-mcp-bedrock-invoke-restrict-models"
  role = aws_iam_role.ssm.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid    = "RestrictBedrockModelIds"
        Effect = "Deny"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:Converse",
          "bedrock:ConverseStream",
          "bedrock:CountTokens"
        ]
        Resource = "*"
        Condition = {
          "StringNotEquals" = {
            "bedrock:ModelId" = var.bedrock_allowed_model_ids
          }
        }
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
  instance_type          = "t3.micro"
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [aws_security_group.mcp.id]
  iam_instance_profile   = aws_iam_instance_profile.this.name

  user_data = <<-EOF
    #!/bin/bash
    set -euo pipefail

    # Base runtime only. App code is deployed out-of-band via GitHub Actions + SSM.
    dnf update -y
    dnf install -y python3 python3-pip curl awscli tar

    # Directories for the deployed app
    mkdir -p /opt/mcp_server/current
    mkdir -p /opt/mcp_server/releases

    # Create a dedicated user
    id -u mcp >/dev/null 2>&1 || useradd -r -s /sbin/nologin mcp
    chown -R mcp:mcp /opt/mcp_server

    # Systemd unit to run the FastAPI server.
    # Note: we do NOT enable/start it here because code isn't present yet.
    cat > /etc/systemd/system/mcp-server.service <<'UNIT'
    [Unit]
    Description=Context Graph MCP Server (FastAPI)
    After=network-online.target
    Wants=network-online.target

    [Service]
    Type=simple
    User=mcp
    Group=mcp
    WorkingDirectory=/opt/mcp_server/current
    Environment=PORT=${var.ingress_port}
    Environment=AWS_REGION=${data.aws_region.current.name}
    Environment=RETRIEVE_LAMBDA_NAME=${var.name}-retrieve
    ExecStart=/opt/mcp_server/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${var.ingress_port}
    Restart=on-failure
    RestartSec=2

    [Install]
    WantedBy=multi-user.target
    UNIT

    # Deployment script invoked by the GitHub Actions workflow (via SSM).
    # The instance has an IAM role that can read s3://.../mcp-server/* from the artifacts bucket.
    cat > /usr/local/bin/deploy-mcp-server.sh <<'SCRIPT'
    #!/usr/bin/env bash
    set -euo pipefail

    usage() {
      echo "Usage: deploy-mcp-server.sh <artifacts-bucket> <sha>" >&2
      exit 2
    }

    bucket="$${1:-}"
    sha="$${2:-}"
    [[ -n "$${bucket}" && -n "$${sha}" ]] || usage

    key="mcp-server/$${sha}.tar.gz"
    release_dir="/opt/mcp_server/releases/$${sha}"

    mkdir -p /opt/mcp_server/releases
    rm -rf "$${release_dir}"
    mkdir -p "$${release_dir}"

    aws s3 cp "s3://$${bucket}/$${key}" /tmp/mcp_server.tar.gz
    tar -xzf /tmp/mcp_server.tar.gz -C "$${release_dir}"

    rm -rf /opt/mcp_server/current
    ln -sfn "$${release_dir}" /opt/mcp_server/current

    id -u mcp >/dev/null 2>&1 || useradd -r -s /sbin/nologin mcp
    chown -R mcp:mcp /opt/mcp_server

    python3 -m venv /opt/mcp_server/venv
    /opt/mcp_server/venv/bin/pip install --upgrade pip
    /opt/mcp_server/venv/bin/pip install -r /opt/mcp_server/current/requirements.txt

    systemctl daemon-reload
    systemctl enable --now mcp-server.service
    systemctl restart mcp-server.service
    SCRIPT
    chmod 0755 /usr/local/bin/deploy-mcp-server.sh

    systemctl daemon-reload
  EOF

  tags = merge(var.tags, { Name = "${var.name}-mcp" })
}

