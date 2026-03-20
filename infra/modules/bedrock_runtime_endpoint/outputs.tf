output "security_group_id" {
  value = aws_security_group.endpoint.id
}

output "vpc_endpoint_id" {
  value = aws_vpc_endpoint.bedrock_runtime.id
}

output "dns_entries" {
  value = aws_vpc_endpoint.bedrock_runtime.dns_entry
}

