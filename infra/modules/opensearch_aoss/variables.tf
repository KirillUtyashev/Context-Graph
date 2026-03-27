variable "name" {
  type        = string
  description = "Name prefix — used for all resource names."
}

variable "ec2_role_name" {
  type        = string
  description = "IAM role name of the MCP EC2 instance. Granted index-level access via AOSS data access policy."
}

variable "tags" {
  type    = map(string)
  default = {}
}
