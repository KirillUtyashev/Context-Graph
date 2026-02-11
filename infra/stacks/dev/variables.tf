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

