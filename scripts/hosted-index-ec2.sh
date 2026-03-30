#!/usr/bin/env bash
# hosted-index-ec2.sh — Run hosted-index for both repos on EC2.
# Adds GitHub PR/issue/review provenance to Neptune + Bedrock LLM claim extraction.
set -uo pipefail

export AWS_DEFAULT_REGION=ca-central-1
export HOME=/root

LOG=/tmp/index-github-sync.log
> "$LOG"
exec > >(tee -a "$LOG") 2>&1

echo "=== GitHub sync started at $(date -u) ==="

PYTHON=/opt/mcp_server/venv/bin/python
NEPTUNE_HOST=context-graph-dev-neptune.cluster-cd2euaamm1l1.ca-central-1.neptune.amazonaws.com
AURORA_HOST=context-graph-dev-aurora.cluster-cd2euaamm1l1.ca-central-1.rds.amazonaws.com
REPO_CACHE=/tmp/repo-cache

AOSS_HOST=$(grep -oP 'AOSS_HOST=\K\S+' /etc/systemd/system/mcp-server.service | head -1)
GITHUB_TOKEN_VAL=$(grep -oP 'GITHUB_TOKEN=\K\S+' /etc/systemd/system/mcp-server.service | head -1 | tr -d '"')
AURORA_SECRET=$(grep -oP 'AURORA_PASSWORD_SECRET_ARN=\K\S+' /etc/systemd/system/mcp-server.service | head -1)

if [[ -z "$GITHUB_TOKEN_VAL" ]]; then
  echo "ERROR: GITHUB_TOKEN not found in systemd service file" >&2
  exit 1
fi

AURORA_PASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id "$AURORA_SECRET" --query SecretString --output text)

AURORA_DSN="postgresql://postgres:${AURORA_PASSWORD}@${AURORA_HOST}/context_graph"
export GITHUB_TOKEN="$GITHUB_TOKEN_VAL"

echo "AOSS_HOST: $AOSS_HOST"
echo "REPO_CACHE: $REPO_CACHE"
mkdir -p "$REPO_CACHE"

COMMON_ARGS=(
  --metadata-backend postgres
  --metadata-dsn "$AURORA_DSN"
  --graph-backend neptune
  --neptune-host "$NEPTUNE_HOST"
  --aoss-host "$AOSS_HOST"
  --llm-provider bedrock
  --llm-model us.anthropic.claude-sonnet-4-6
  --embed-provider bedrock
  --embed-dimensions 1024
  --repo-cache-dir "$REPO_CACHE"
  --execute
)

echo ""
echo "--- [1/2] hosted-index order-management-v2 ---"
echo "Start: $(date -u +%H:%M:%S)"
if "$PYTHON" -m context_graph hosted-index \
  --github-url https://github.com/dkucz/order-management-service-extended \
  --repo-id order-management-v2 \
  "${COMMON_ARGS[@]}"; then
  echo "order-management-v2 DONE at $(date -u +%H:%M:%S)"
else
  echo "order-management-v2 FAILED at $(date -u +%H:%M:%S)"
fi

echo ""
echo "--- [2/2] hosted-index test-harness-new2 ---"
echo "Start: $(date -u +%H:%M:%S)"
if "$PYTHON" -m context_graph hosted-index \
  --github-url https://github.com/patelhrid/context-graph-test-harness-new2 \
  --repo-id test-harness-new2 \
  "${COMMON_ARGS[@]}"; then
  echo "test-harness-new2 DONE at $(date -u +%H:%M:%S)"
else
  echo "test-harness-new2 FAILED at $(date -u +%H:%M:%S)"
fi

echo ""
echo "=== GitHub sync finished at $(date -u) ==="
