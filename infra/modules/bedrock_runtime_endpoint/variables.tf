variable "name" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

# Security groups of clients (EC2/Lambda) allowed to connect to the endpoint
variable "allowed_sg_ids" {
  type    = list(string)
  default = []
}

