locals {
  name = "${var.project_name}-${var.env}"
  tags = {
    Project = var.project_name
    Env     = var.env
  }
  repo_sync_index_enabled             = var.repo_sync_index_image_uri != ""

  bedrock_public_proxy_username         = var.bedrock_public_proxy_basic_auth_username
  bedrock_public_proxy_password         = var.bedrock_public_proxy_basic_auth_password != "" ? var.bedrock_public_proxy_basic_auth_password : random_password.bedrock_public_proxy_password[0].result
  bedrock_public_proxy_default_model_id = try(var.bedrock_allowed_model_ids[0], "")
}

module "network" {
  source   = "../../modules/network"
  name     = local.name
  vpc_cidr = var.vpc_cidr
  tags     = local.tags
}

module "mcp" {
  source                    = "../../modules/mcp_ec2"
  name                      = local.name
  vpc_id                    = module.network.vpc_id
  subnet_id                 = module.network.public_subnet_ids[0]
  ingress_port              = var.mcp_ingress_port
  ingress_cidr              = var.mcp_ingress_cidr
  enable_bedrock            = var.enable_bedrock
  bedrock_allowed_model_ids = var.bedrock_allowed_model_ids
  neptune_endpoint          = module.neptune.endpoint
  tags                      = local.tags
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

resource "random_password" "bedrock_public_proxy_password" {
  count   = var.bedrock_public_proxy_basic_auth_password == "" ? 1 : 0
  length  = 32
  special = false
}

module "bedrock_runtime_endpoint" {
  source         = "../../modules/bedrock_runtime_endpoint"
  name           = local.name
  vpc_id         = module.network.vpc_id
  subnet_ids     = [module.network.public_subnet_ids[0]]
  allowed_sg_ids = [module.mcp.security_group_id, aws_security_group.lambda.id]
  tags           = local.tags
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

module "repo_cache_efs" {
  source         = "../../modules/efs_cache"
  name           = local.name
  vpc_id         = module.network.vpc_id
  subnet_ids     = module.network.private_subnet_ids
  allowed_sg_ids = [aws_security_group.lambda.id]
  tags           = local.tags
}

resource "aws_secretsmanager_secret" "github_token" {
  name                    = "${local.name}/github/token"
  recovery_window_in_days = 0

  tags = merge(local.tags, {
    Name = "${local.name}-github-token"
  })
}

resource "aws_ecr_repository" "repo_sync_index" {
  name                 = "${local.name}-repo-sync-index"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = merge(local.tags, {
    Name = "${local.name}-repo-sync-index"
  })
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

module "repo_sync_index_lambda" {
  count                  = local.repo_sync_index_enabled ? 1 : 0
  source                 = "../../modules/lambda_container_url"
  name                   = "${local.name}-repo-sync-index"
  image_uri              = var.repo_sync_index_image_uri
  vpc_subnet_ids         = module.network.private_subnet_ids
  vpc_security_group_ids = [aws_security_group.lambda.id]
  efs_access_point_arn   = module.repo_cache_efs.access_point_arn
  efs_local_mount_path   = "/mnt/context-graph"
  environment = {
    AURORA_ENDPOINT            = module.aurora.cluster_endpoint
    AURORA_DB_NAME             = module.aurora.db_name
    AURORA_USERNAME            = module.aurora.master_username
    AURORA_PASSWORD_SECRET_ARN = module.aurora.master_password_secret_arn
    GITHUB_TOKEN_SECRET_ARN    = aws_secretsmanager_secret.github_token.arn
    NEPTUNE_ENDPOINT           = module.neptune.endpoint
    REPO_CACHE_DIR             = "/mnt/context-graph"
    ENV                        = var.env
  }
  tags = local.tags
}

resource "aws_iam_role_policy" "repo_sync_index_access" {
  count = local.repo_sync_index_enabled ? 1 : 0

  name = "${local.name}-repo-sync-index-access"
  role = module.repo_sync_index_lambda[0].role_name

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Sid    = "ReadGithubAndAuroraSecrets"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [
          aws_secretsmanager_secret.github_token.arn,
          module.aurora.master_password_secret_arn
        ]
      }
    ]
  })
}

module "bedrock_public_proxy_lambda" {
  source     = "../../modules/lambda_url"
  name       = "${local.name}-bedrock-proxy"
  source_dir = abspath("${path.root}/../../../lambda_bedrock_proxy")

  environment = {
    BASIC_AUTH_USERNAME = local.bedrock_public_proxy_username
    BASIC_AUTH_PASSWORD = local.bedrock_public_proxy_password
    DEFAULT_MODEL_ID    = local.bedrock_public_proxy_default_model_id
    ALLOWED_MODEL_IDS   = join(",", var.bedrock_allowed_model_ids)
  }

  tags = local.tags
}

resource "aws_iam_role_policy" "bedrock_public_proxy_invoke" {
  count = var.enable_bedrock ? 1 : 0

  name = "${local.name}-bedrock-proxy-invoke"
  role = module.bedrock_public_proxy_lambda.role_name

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

resource "aws_iam_role_policy" "bedrock_public_proxy_invoke_restrict_models" {
  count = (var.enable_bedrock && length(var.bedrock_allowed_model_ids) > 0) ? 1 : 0

  name = "${local.name}-bedrock-proxy-invoke-restrict-models"
  role = module.bedrock_public_proxy_lambda.role_name

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
          "StringNotEqualsIfExists" = {
            "bedrock:ModelId" = var.bedrock_allowed_model_ids
          }
        }
      }
    ]
  })
}
