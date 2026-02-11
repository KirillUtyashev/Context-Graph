variable "name" {
  description = "Name prefix."
  type        = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  description = "Private subnet IDs for the DB subnet group."
  type        = list(string)
}

variable "allowed_sg_ids" {
  description = "Security group IDs allowed to connect to Postgres."
  type        = list(string)
  default     = []
}

variable "db_name" {
  type    = string
  default = "context_graph"
}

variable "master_username" {
  type    = string
  default = "postgres"
}

variable "serverless_min_acu" {
  type    = number
  default = 0.5
}

variable "serverless_max_acu" {
  type    = number
  default = 2
}

variable "tags" {
  type    = map(string)
  default = {}
}

