resource "aws_neptune_subnet_group" "this" {
  name       = "${var.name}-neptune-subnets"
  subnet_ids = var.subnet_ids

  tags = merge(var.tags, {
    Name = "${var.name}-neptune-subnets"
  })
}

data "aws_rds_engine_version" "neptune" {
  engine       = "neptune"
  default_only = true
}

resource "aws_security_group" "neptune" {
  name        = "${var.name}-neptune-sg"
  description = "Neptune access"
  vpc_id      = var.vpc_id

  dynamic "ingress" {
    for_each = toset(var.allowed_sg_ids)
    content {
      description     = "Neptune from allowed SG"
      from_port       = 8182
      to_port         = 8182
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

  tags = merge(var.tags, {
    Name = "${var.name}-neptune-sg"
  })
}

resource "aws_neptune_cluster_parameter_group" "this" {
  name        = "${var.name}-neptune-pg"
  family      = data.aws_rds_engine_version.neptune.parameter_group_family
  description = "Neptune params for ${var.name}"

  tags = merge(var.tags, {
    Name = "${var.name}-neptune-pg"
  })
}

resource "aws_neptune_cluster" "this" {
  cluster_identifier                   = "${var.name}-neptune"
  neptune_subnet_group_name            = aws_neptune_subnet_group.this.name
  vpc_security_group_ids               = [aws_security_group.neptune.id]
  neptune_cluster_parameter_group_name = aws_neptune_cluster_parameter_group.this.name
  engine_version                       = data.aws_rds_engine_version.neptune.version

  skip_final_snapshot = true
  deletion_protection = false

  # Neptune is VPC-only by design.

  serverless_v2_scaling_configuration {
    min_capacity = var.min_capacity
    max_capacity = var.max_capacity
  }

  tags = merge(var.tags, {
    Name = "${var.name}-neptune"
  })
}

resource "aws_neptune_cluster_instance" "this" {
  count              = 1
  identifier         = "${var.name}-neptune-1"
  cluster_identifier = aws_neptune_cluster.this.id
  instance_class     = "db.serverless"

  tags = merge(var.tags, {
    Name = "${var.name}-neptune-1"
  })
}

