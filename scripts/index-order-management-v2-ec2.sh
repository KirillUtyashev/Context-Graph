#!/usr/bin/env bash
# index-order-management-v2-ec2.sh — Run on EC2 via SSM to re-index order-management-v2
# with AOSS enabled and root_path pointing to /tmp (accessible to MCP server).
#
# Expected to run as root (SSM default). Logs everything to /tmp/index-order-management-v2.log.
# Safe to re-run: resets context first, then re-indexes all first-parent commits.
set -uo pipefail

LOG=/tmp/index-order-management-v2.log
# Truncate log for a clean run
> "$LOG"
exec > >(tee -a "$LOG") 2>&1

echo "========================================================"
echo "  order-management-v2 indexing  started at $(date -u)"
echo "========================================================"

REPO_ID=order-management-v2
NEPTUNE_HOST=context-graph-dev-neptune.cluster-cd2euaamm1l1.ca-central-1.neptune.amazonaws.com
AURORA_HOST=context-graph-dev-aurora.cluster-cd2euaamm1l1.ca-central-1.rds.amazonaws.com
REGION=ca-central-1
PYTHON=/opt/mcp_server/venv/bin/python
S3_BUCKET=context-graph-dev-472730591037-mcp-artifacts
REPO=/tmp/order-management-service-extended

export AWS_DEFAULT_REGION=$REGION

# ---------------------------------------------------------------------------
# 1. Download + extract the repo
# ---------------------------------------------------------------------------
echo ""
echo "--- [1/5] Downloading repo from S3 ---"
aws s3 cp "s3://${S3_BUCKET}/mcp-server/order-management-v2.tar.gz" /tmp/order-management-v2.tar.gz
rm -rf "$REPO"
mkdir -p "$REPO"
tar -xzf /tmp/order-management-v2.tar.gz -C "$REPO"
echo "Extracted to $REPO"
ls "$REPO" | head -20

# ---------------------------------------------------------------------------
# 2. Get Aurora credentials from Secrets Manager
# ---------------------------------------------------------------------------
echo ""
echo "--- [2/5] Fetching Aurora credentials ---"

AURORA_PASSWORD_SECRET_ARN=""
for f in /etc/systemd/system/mcp-server.service \
          /etc/systemd/system/mcp-server.service.d/*.conf; do
  [[ -f "$f" ]] || continue
  ARN=$(grep -oP 'AURORA_PASSWORD_SECRET_ARN=\K\S+' "$f" | head -1 || true)
  if [[ -n "$ARN" ]]; then
    AURORA_PASSWORD_SECRET_ARN="$ARN"
    echo "Found AURORA_PASSWORD_SECRET_ARN in $f"
    break
  fi
done

if [[ -z "$AURORA_PASSWORD_SECRET_ARN" ]]; then
  echo "ERROR: AURORA_PASSWORD_SECRET_ARN not found in systemd unit files" >&2
  exit 1
fi

AURORA_PASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id "$AURORA_PASSWORD_SECRET_ARN" \
  --query SecretString --output text \
  --region "$REGION")

AURORA_DSN="postgresql://postgres:${AURORA_PASSWORD}@${AURORA_HOST}/context_graph"
echo "Aurora DSN constructed (password hidden)"

# ---------------------------------------------------------------------------
# 3. Read AOSS host from systemd environment
# ---------------------------------------------------------------------------
echo ""
echo "--- [3/5] Reading AOSS host ---"

AOSS_HOST_VAL=""
for f in /etc/systemd/system/mcp-server.service \
          /etc/systemd/system/mcp-server.service.d/*.conf; do
  [[ -f "$f" ]] || continue
  VAL=$(grep -oP 'AOSS_HOST=\K\S+' "$f" | head -1 || true)
  if [[ -n "$VAL" ]]; then
    AOSS_HOST_VAL="$VAL"
    echo "Found AOSS_HOST in $f: $AOSS_HOST_VAL"
    break
  fi
done

if [[ -z "$AOSS_HOST_VAL" ]]; then
  echo "ERROR: AOSS_HOST not found in systemd unit files" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Common CLI args (used for apply-commit)
# ---------------------------------------------------------------------------
COMMON_ARGS=(
  --repo-id "$REPO_ID"
  --metadata-backend postgres
  --metadata-dsn "$AURORA_DSN"
  --graph-backend neptune
  --neptune-host "$NEPTUNE_HOST"
  --aoss-host "$AOSS_HOST_VAL"
  --llm-provider bedrock
  --llm-model us.anthropic.claude-sonnet-4-6
  --embed-provider bedrock
  --embed-dimensions 1024
  --execute
)

# reset-context does not accept LLM/embed args — use a trimmed arg set
RESET_ARGS=(
  --repo-id "$REPO_ID"
  --metadata-backend postgres
  --metadata-dsn "$AURORA_DSN"
  --graph-backend neptune
  --neptune-host "$NEPTUNE_HOST"
  --aoss-host "$AOSS_HOST_VAL"
  --execute
)

# ---------------------------------------------------------------------------
# 4. Reset existing order-management-v2 context (clears Neptune + Aurora + AOSS)
# ---------------------------------------------------------------------------
echo ""
echo "--- [4/5] Resetting existing order-management-v2 context ---"
"$PYTHON" -m context_graph reset-context "${RESET_ARGS[@]}"
echo "Reset complete."

# ---------------------------------------------------------------------------
# 5. Commit history — all first-parent commits oldest → newest
# ---------------------------------------------------------------------------
echo ""
echo "--- [5/5] Indexing commit history ---"

COMMITS=$(git -C "$REPO" log --first-parent --reverse --format="%H")
TOTAL=$(echo "$COMMITS" | wc -l)
N=0
FAIL=0

for SHA in $COMMITS; do
  N=$((N + 1))
  echo ""
  echo "[${N}/${TOTAL}] $(date -u +%H:%M:%S) apply-commit $SHA"
  if "$PYTHON" -m context_graph apply-commit \
    "$REPO" \
    --commit "$SHA" \
    "${COMMON_ARGS[@]}"; then
    true
  else
    FAIL=$((FAIL + 1))
    echo "WARNING: apply-commit $SHA failed (total failures: $FAIL), continuing"
  fi
done

echo ""
echo "=== Commit indexing complete: ${N} commits, ${FAIL} failures ==="
echo ""
echo "========================================================"
echo "  order-management-v2 indexing  finished at $(date -u)"
echo "  Commits: ${N} processed, ${FAIL} failed"
echo "  Log:     $LOG"
echo "========================================================"
