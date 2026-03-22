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
  type    = list(string)
  default = []
}

variable "access_point_path" {
  type    = string
  default = "/context-graph"
}

variable "posix_uid" {
  type    = number
  default = 1000
}

variable "posix_gid" {
  type    = number
  default = 1000
}

variable "root_permissions" {
  type    = string
  default = "750"
}

variable "tags" {
  type    = map(string)
  default = {}
}
