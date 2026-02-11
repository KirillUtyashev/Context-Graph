variable "name" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "vpc_subnet_ids" {
  description = "If set, Lambda will run in this VPC subnets."
  type        = list(string)
  default     = []
}

variable "vpc_security_group_ids" {
  description = "If set, Lambda will attach these SGs."
  type        = list(string)
  default     = []
}

variable "environment" {
  type    = map(string)
  default = {}
}

variable "source_dir" {
  description = "Directory containing lambda_function.py"
  type        = string
}

