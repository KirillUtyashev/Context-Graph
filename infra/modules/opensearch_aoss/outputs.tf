output "collection_endpoint" {
  description = "AOSS collection endpoint hostname (no https:// prefix). Pass as aoss_host to NeptuneDriver — opensearchpy expects a bare hostname in the host dict."
  value       = replace(aws_opensearchserverless_collection.this.collection_endpoint, "https://", "")
}

output "collection_arn" {
  description = "AOSS collection ARN. Used to scope the IAM aoss:APIAccessAll policy in mcp_ec2."
  value       = aws_opensearchserverless_collection.this.arn
}
