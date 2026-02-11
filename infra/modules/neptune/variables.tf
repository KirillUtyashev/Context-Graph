variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "allowed_sg_ids" {
  description = "Security group IDs allowed to connect to Neptune (8182)."
  type        = list(string)
  default     = []
}

variable "min_capacity" {
  description = "Neptune Serverless v2 min capacity (ACU-like units)."
  type        = number
  default     = 1
}

variable "max_capacity" {
  description = "Neptune Serverless v2 max capacity."
  type        = number
  default     = 2
}

variable "tags" {
  type    = map(string)
  default = {}
}

