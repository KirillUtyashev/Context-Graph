output "cluster_endpoint" {
  value = aws_rds_cluster.this.endpoint
}

output "cluster_reader_endpoint" {
  value = aws_rds_cluster.this.reader_endpoint
}

output "security_group_id" {
  value = aws_security_group.db.id
}

output "master_password_secret_arn" {
  value = aws_secretsmanager_secret.db_master.arn
}

output "master_username" {
  value = var.master_username
}

output "db_name" {
  value = var.db_name
}

