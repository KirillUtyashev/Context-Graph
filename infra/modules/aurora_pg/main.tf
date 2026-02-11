resource "random_password" "master" {
  length  = 24
  special = true

  # RDS rejects: '/', '@', '"', and spaces.
  # Constrain special characters to a known-safe set.
  override_special = "!#$%&*()-_=+[]{}<>:?.,|"
}

data "aws_rds_engine_version" "aurora_pg" {
  engine       = "aurora-postgresql"
  default_only = true
}

resource "aws_secretsmanager_secret" "db_master" {
  name = "${var.name}/aurora/master"

  tags = merge(var.tags, {
    Name = "${var.name}-aurora-master-secret"
  })
}

resource "aws_secretsmanager_secret_version" "db_master" {
  secret_id     = aws_secretsmanager_secret.db_master.id
  secret_string = random_password.master.result
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name}-aurora-subnets"
  subnet_ids = var.subnet_ids

  tags = merge(var.tags, {
    Name = "${var.name}-aurora-subnets"
  })
}

resource "aws_security_group" "db" {
  name        = "${var.name}-aurora-sg"
  description = "Aurora Postgres access"
  vpc_id      = var.vpc_id

  dynamic "ingress" {
    for_each = toset(var.allowed_sg_ids)
    content {
      description     = "Postgres from allowed SG"
      from_port       = 5432
      to_port         = 5432
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
    Name = "${var.name}-aurora-sg"
  })
}

resource "aws_rds_cluster_parameter_group" "this" {
  name        = "${var.name}-aurora-pg"
  family      = data.aws_rds_engine_version.aurora_pg.parameter_group_family
  description = "Aurora Postgres params for ${var.name}"

  # pgvector is installed with CREATE EXTENSION in SQL.
  # No special parameter required here for a course-project setup.

  tags = merge(var.tags, {
    Name = "${var.name}-aurora-pg"
  })
}

resource "aws_rds_cluster" "this" {
  cluster_identifier = "${var.name}-aurora"
  engine             = "aurora-postgresql"
  engine_version     = data.aws_rds_engine_version.aurora_pg.version

  database_name   = var.db_name
  master_username = var.master_username
  master_password = random_password.master.result

  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name

  storage_encrypted   = true
  deletion_protection = false
  skip_final_snapshot = true

  serverlessv2_scaling_configuration {
    min_capacity = var.serverless_min_acu
    max_capacity = var.serverless_max_acu
  }

  tags = merge(var.tags, {
    Name = "${var.name}-aurora"
  })
}

resource "aws_rds_cluster_instance" "this" {
  identifier         = "${var.name}-aurora-1"
  cluster_identifier = aws_rds_cluster.this.id
  engine             = aws_rds_cluster.this.engine
  engine_version     = aws_rds_cluster.this.engine_version
  instance_class     = "db.serverless"

  publicly_accessible = false

  tags = merge(var.tags, {
    Name = "${var.name}-aurora-1"
  })
}

