output "mcp_public_ip" {
  value = module.mcp.public_ip
}

output "mcp_url" {
  value = "http://${module.mcp.public_ip}:${var.mcp_ingress_port}"
}

output "mcp_artifacts_bucket" {
  value = module.mcp.artifacts_bucket_name
}

output "bedrock_runtime_vpc_endpoint_id" {
  value = module.bedrock_runtime_endpoint.vpc_endpoint_id
}

output "bedrock_runtime_vpc_endpoint_dns_entries" {
  value = module.bedrock_runtime_endpoint.dns_entries
}

output "aurora_endpoint" {
  value = module.aurora.cluster_endpoint
}

output "aurora_master_password_secret_arn" {
  value = module.aurora.master_password_secret_arn
}

output "neptune_endpoint" {
  value = module.neptune.endpoint
}

output "augment_function_url" {
  value = module.augment_lambda.function_url
}

output "retrieve_function_url" {
  value = module.retrieve_lambda.function_url
}

output "repo_sync_index_function_url" {
  value = local.repo_sync_index_enabled ? module.repo_sync_index_lambda[0].function_url : null
}

output "repo_sync_index_ecr_repository_url" {
  value = aws_ecr_repository.repo_sync_index.repository_url
}

output "github_token_secret_arn" {
  value = aws_secretsmanager_secret.github_token.arn
}

output "bedrock_public_proxy_function_url" {
  value = module.bedrock_public_proxy_lambda.function_url
}

output "bedrock_public_proxy_basic_auth_username" {
  value = var.bedrock_public_proxy_basic_auth_username
}

output "bedrock_public_proxy_basic_auth_password" {
  value     = local.bedrock_public_proxy_password
  sensitive = true
}

output "bedrock_public_proxy_default_model_id" {
  value = local.bedrock_public_proxy_default_model_id
}
