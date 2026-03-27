variable "name" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "subnet_id" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "ingress_port" {
  type    = number
  default = 8000
}

variable "ingress_cidr" {
  type    = string
  default = "0.0.0.0/0"
}

variable "enable_bedrock" {
  type    = bool
  default = true
}

variable "neptune_endpoint" {
  type        = string
  description = "Neptune cluster endpoint (without protocol prefix). Injected as NEPTUNE_HOST=neptune-db://<endpoint> in the systemd unit."
  default     = ""
}

variable "aurora_endpoint" {
  type        = string
  description = "Aurora PostgreSQL cluster endpoint. Injected as AURORA_ENDPOINT in the systemd unit."
  default     = ""
}

variable "aurora_password_secret_arn" {
  type        = string
  description = "Secrets Manager ARN for the Aurora master password. Injected as AURORA_PASSWORD_SECRET_ARN and granted GetSecretValue."
  default     = ""
}

variable "aurora_db_name" {
  type        = string
  description = "Aurora database name. Injected as AURORA_DB_NAME in the systemd unit."
  default     = "context_graph"
}

variable "aurora_username" {
  type        = string
  description = "Aurora master username. Injected as AURORA_USERNAME in the systemd unit."
  default     = "postgres"
}

variable "aoss_host" {
  type        = string
  description = "AOSS collection endpoint hostname (no https:// prefix). When non-empty, BM25/keyword search is enabled. Leave empty to disable."
  default     = ""
}

variable "aoss_collection_arn" {
  type        = string
  description = "AOSS collection ARN. Used to scope the aoss:APIAccessAll IAM policy. Required when aoss_host is set."
  default     = ""
}

# Optional IAM tightening: if set non-empty, only allow invoking these model IDs/ARNs
variable "bedrock_allowed_model_ids" {
  type    = list(string)
  default = []
}
