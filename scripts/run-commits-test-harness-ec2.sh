#!/usr/bin/env bash
# run-commits-test-harness-ec2.sh — Index context-graph-test-harness-new2 on EC2.
set -uo pipefail

export AWS_DEFAULT_REGION=ca-central-1
export HOME=/root

LOG=/tmp/index-test-harness-new2.log
> "$LOG"
exec > >(tee -a "$LOG") 2>&1

echo "=== test-harness-new2 indexing started at $(date -u) ==="

REPO=/tmp/test-harness-new2
REPO_ID=test-harness-new2
PYTHON=/opt/mcp_server/venv/bin/python
NEPTUNE_HOST=context-graph-dev-neptune.cluster-cd2euaamm1l1.ca-central-1.neptune.amazonaws.com
AURORA_HOST=context-graph-dev-aurora.cluster-cd2euaamm1l1.ca-central-1.rds.amazonaws.com

AOSS_HOST=$(grep -oP 'AOSS_HOST=\K\S+' /etc/systemd/system/mcp-server.service | head -1)
AURORA_SECRET=$(grep -oP 'AURORA_PASSWORD_SECRET_ARN=\K\S+' /etc/systemd/system/mcp-server.service | head -1)
AURORA_PASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id "$AURORA_SECRET" \
  --query SecretString --output text \
  --region ca-central-1)

AURORA_DSN="postgresql://postgres:${AURORA_PASSWORD}@${AURORA_HOST}/context_graph"
echo "AOSS_HOST: $AOSS_HOST"

echo "--- Downloading repo from S3 ---"
aws s3 cp s3://context-graph-dev-472730591037-mcp-artifacts/mcp-server/test-harness-new2.tar.gz \
  /tmp/test-harness-new2.tar.gz --region ca-central-1
rm -rf "$REPO"
mkdir -p "$REPO"
tar -xzf /tmp/test-harness-new2.tar.gz -C "$REPO"
echo "Extracted to $REPO"
ls "$REPO" | head -10

git config --global --add safe.directory "$REPO"
COMMITS=$(git -C "$REPO" log --first-parent --reverse --format="%H")
TOTAL=$(echo "$COMMITS" | wc -l)
N=0
FAIL=0

for SHA in $COMMITS; do
  N=$((N + 1))
  echo ""
  echo "[${N}/${TOTAL}] $(date -u +%H:%M:%S) apply-commit $SHA"
  if "$PYTHON" -m context_graph apply-commit "$REPO" \
    --commit "$SHA" \
    --repo-id "$REPO_ID" \
    --metadata-backend postgres \
    --metadata-dsn "$AURORA_DSN" \
    --graph-backend neptune \
    --neptune-host "$NEPTUNE_HOST" \
    --aoss-host "$AOSS_HOST" \
    --llm-provider bedrock \
    --llm-model us.anthropic.claude-sonnet-4-6 \
    --embed-provider bedrock \
    --embed-dimensions 1024 \
    --execute; then
    true
  else
    FAIL=$((FAIL + 1))
    echo "WARNING: $SHA failed (total failures: $FAIL), continuing"
  fi
done

echo ""
echo "=== DONE: ${N} commits, ${FAIL} failures at $(date -u) ==="
