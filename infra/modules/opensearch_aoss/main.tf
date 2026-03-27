data "aws_caller_identity" "current" {}

# Encryption policy — must exist before the collection can be created.
resource "aws_opensearchserverless_security_policy" "encryption" {
  name   = "${var.name}-aoss-enc"
  type   = "encryption"
  policy = jsonencode({
    Rules = [{
      Resource     = ["collection/${var.name}-search"]
      ResourceType = "collection"
    }]
    AWSOwnedKey = true
  })
}

# Network policy — SEARCH collections cannot use VPC endpoint attachment.
# Public access is required; authentication is enforced via IAM V4 Signed requests
# (handled by opensearchpy Urllib3AWSV4SignerAuth in NeptuneDriver).
resource "aws_opensearchserverless_security_policy" "network" {
  name   = "${var.name}-aoss-net"
  type   = "network"
  policy = jsonencode([{
    Rules = [
      {
        Resource     = ["collection/${var.name}-search"]
        ResourceType = "collection"
      },
      {
        Resource     = ["collection/${var.name}-search"]
        ResourceType = "dashboard"
      }
    ]
    AllowFromPublic = true
  }])
}

# SEARCH collection for BM25 / inverted-index text search.
# Use SEARCH (not VECTORSEARCH) — the existing aoss_indices mappings in NeptuneDriver
# use standard `type: text` fields which require a SEARCH collection.
resource "aws_opensearchserverless_collection" "this" {
  name = "${var.name}-search"
  type = "SEARCH"

  tags = merge(var.tags, { Name = "${var.name}-search" })

  depends_on = [
    aws_opensearchserverless_security_policy.encryption,
    aws_opensearchserverless_security_policy.network,
  ]
}

# AOSS-native data access policy — grants the EC2 role index-level CRUD access.
# This is distinct from the IAM aoss:APIAccessAll policy in mcp_ec2/main.tf:
#   - The IAM policy allows the EC2 role to call the AOSS API endpoint.
#   - This data access policy authorizes document-level operations on the collection.
# Both are required.
resource "aws_opensearchserverless_access_policy" "ec2_access" {
  name        = "${var.name}-aoss-access"
  type        = "data"
  description = "Allow MCP EC2 role to read/write AOSS indices"
  policy = jsonencode([{
    Rules = [
      {
        Resource = ["index/${var.name}-search/*"]
        Permission = [
          "aoss:CreateIndex",
          "aoss:DeleteIndex",
          "aoss:UpdateIndex",
          "aoss:DescribeIndex",
          "aoss:ReadDocument",
          "aoss:WriteDocument"
        ]
        ResourceType = "index"
      },
      {
        Resource     = ["collection/${var.name}-search"]
        Permission   = ["aoss:DescribeCollectionItems"]
        ResourceType = "collection"
      }
    ]
    Principal = [
      "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.ec2_role_name}"
    ]
  }])
}
