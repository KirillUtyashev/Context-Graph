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

