data "aws_region" "current" {}

locals {
  endpoint_name = "${var.name}-bedrock-runtime"
}

resource "aws_security_group" "endpoint" {
  name        = "${local.endpoint_name}-sg"
  description = "Allow VPC clients to reach Bedrock Runtime VPC endpoint"
  vpc_id      = var.vpc_id

  dynamic "ingress" {
    for_each = toset(var.allowed_sg_ids)
    content {
      description     = "HTTPS from allowed client SG"
      from_port       = 443
      to_port         = 443
      protocol        = "tcp"
      security_groups = [ingress.value]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${local.endpoint_name}-sg" })
}

resource "aws_vpc_endpoint" "bedrock_runtime" {
  vpc_id              = var.vpc_id
  vpc_endpoint_type   = "Interface"
  service_name        = "com.amazonaws.${data.aws_region.current.name}.bedrock-runtime"
  private_dns_enabled = true

  subnet_ids         = var.subnet_ids
  security_group_ids = [aws_security_group.endpoint.id]

  tags = merge(var.tags, { Name = local.endpoint_name })
}

