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

# Optional IAM tightening: if set non-empty, only allow invoking these model IDs/ARNs
variable "bedrock_allowed_model_ids" {
  type    = list(string)
  default = []
}
