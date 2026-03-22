variable "name" {
  type = string
}

variable "image_uri" {
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

variable "timeout" {
  type    = number
  default = 900
}

variable "memory_size" {
  type    = number
  default = 4096
}

variable "ephemeral_storage_mb" {
  type    = number
  default = 1024
}

variable "efs_access_point_arn" {
  type    = string
  default = null
}

variable "efs_local_mount_path" {
  type    = string
  default = null
}
