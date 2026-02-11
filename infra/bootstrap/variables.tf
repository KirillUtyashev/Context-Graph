variable "aws_region" {
  description = "AWS region for the bootstrap resources."
  type        = string
  default     = "ca-central-1"
}

variable "project_name" {
  description = "Used for tagging and naming."
  type        = string
  default     = "context-graph"
}

