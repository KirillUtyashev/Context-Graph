locals {
  name = "${var.project_name}-${var.env}"
  tags = {
    Project = var.project_name
    Env     = var.env
  }
}

module "network" {
  source   = "../../modules/network"
  name     = local.name
  vpc_cidr = var.vpc_cidr
  tags     = local.tags
}

module "mcp" {
  source       = "../../modules/mcp_ec2"
  name         = local.name
  vpc_id       = module.network.vpc_id
  subnet_id    = module.network.public_subnet_ids[0]
  ingress_port = var.mcp_ingress_port
  ingress_cidr = var.mcp_ingress_cidr
  tags         = local.tags
}

resource "aws_security_group" "lambda" {
  name        = "${local.name}-lambda-sg"
  description = "Lambda SG for VPC access to Aurora/Neptune"
  vpc_id      = module.network.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${local.name}-lambda-sg" })
}

module "aurora" {
  source             = "../../modules/aurora_pg"
  name               = local.name
  vpc_id             = module.network.vpc_id
  subnet_ids         = module.network.private_subnet_ids
  allowed_sg_ids     = [module.mcp.security_group_id, aws_security_group.lambda.id]
  serverless_min_acu = var.aurora_min_acu
  serverless_max_acu = var.aurora_max_acu
  tags               = local.tags
}

module "neptune" {
  source         = "../../modules/neptune"
  name           = local.name
  vpc_id         = module.network.vpc_id
  subnet_ids     = module.network.private_subnet_ids
  allowed_sg_ids = [module.mcp.security_group_id, aws_security_group.lambda.id]
  min_capacity   = var.neptune_min_capacity
  max_capacity   = var.neptune_max_capacity
  tags           = local.tags
}

module "augment_lambda" {
  source                 = "../../modules/lambda_url"
  name                   = "${local.name}-augment"
  source_dir             = abspath("${path.root}/../../../lambda_augment")
  vpc_subnet_ids         = module.network.private_subnet_ids
  vpc_security_group_ids = [aws_security_group.lambda.id]
  environment = {
    AURORA_ENDPOINT  = module.aurora.cluster_endpoint
    NEPTUNE_ENDPOINT = module.neptune.endpoint
    ENV              = var.env
  }
  tags = local.tags
}

module "retrieve_lambda" {
  source                 = "../../modules/lambda_url"
  name                   = "${local.name}-retrieve"
  source_dir             = abspath("${path.root}/../../../lambda_retrieve")
  vpc_subnet_ids         = module.network.private_subnet_ids
  vpc_security_group_ids = [aws_security_group.lambda.id]
  environment = {
    AURORA_ENDPOINT  = module.aurora.cluster_endpoint
    NEPTUNE_ENDPOINT = module.neptune.endpoint
    ENV              = var.env
  }
  tags = local.tags
}

