output "instance_id" {
  value = aws_instance.this.id
}

output "public_ip" {
  value = aws_eip.mcp.public_ip
}

output "security_group_id" {
  value = aws_security_group.mcp.id
}

output "artifacts_bucket_name" {
  value = aws_s3_bucket.artifacts.bucket
}

output "artifacts_bucket_arn" {
  value = aws_s3_bucket.artifacts.arn
}

