output "mcp_public_ip" {
  value = module.mcp.public_ip
}

output "mcp_url" {
  value = "http://${module.mcp.public_ip}:${var.mcp_ingress_port}"
}

output "mcp_artifacts_bucket" {
  value = module.mcp.artifacts_bucket_name
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
