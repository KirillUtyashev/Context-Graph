variable "aws_region" {
  type    = string
  default = "ca-central-1"
}

variable "project_name" {
  type    = string
  default = "context-graph"
}

variable "env" {
  type    = string
  default = "dev"
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "mcp_ingress_port" {
  type    = number
  default = 8000
}

variable "mcp_ingress_cidr" {
  type    = string
  default = "0.0.0.0/0"
}

variable "enable_bedrock" {
  type    = bool
  default = true
}

variable "bedrock_allowed_model_ids" {
  type    = list(string)
  default = ["anthropic.claude-sonnet-4-6", "anthropic.claude-opus-4-6-v1", "cohere.embed-english-v3", "us.anthropic.claude-sonnet-4-6", "us.anthropic.claude-opus-4-6-v1", "amazon.titan-embed-text-v2:0"]
}

variable "bedrock_public_proxy_basic_auth_username" {
  type    = string
  default = "bedrock"
}

variable "bedrock_public_proxy_basic_auth_password" {
  type      = string
  sensitive = true
  default   = ""
}

variable "aurora_min_acu" {
  type    = number
  default = 0.5
}

variable "aurora_max_acu" {
  type    = number
  default = 2
}

variable "neptune_min_capacity" {
  type    = number
  default = 1
}

variable "neptune_max_capacity" {
  type    = number
  default = 2
}

