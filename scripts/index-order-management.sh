#!/usr/bin/env bash
# index-order-management.sh — Run on EC2 via SSM to index order-management-service-extended
# into Neptune (Graphiti) and Aurora (context_graph) under repo_id=order-management.
#
# Expected to run as root (SSM default). Logs everything to /tmp/index-order-management.log.
# Safe to re-run: apply-commit uses ON CONFLICT / upsert semantics.
set -uo pipefail

LOG=/tmp/index-order-management.log
exec > >(tee -a "$LOG") 2>&1

echo "========================================================"
echo "  order-management indexing  started at $(date -u)"
echo "========================================================"

REPO_ID=order-management
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
echo "--- [1/4] Downloading repo from S3 ---"
aws s3 cp "s3://${S3_BUCKET}/mcp-server/order-management.tar.gz" /tmp/order-management.tar.gz
rm -rf "$REPO"
tar -xzf /tmp/order-management.tar.gz -C /tmp/
echo "Extracted to $REPO"
ls "$REPO" | head -20

# ---------------------------------------------------------------------------
# 2. Get Aurora credentials from Secrets Manager
# ---------------------------------------------------------------------------
echo ""
echo "--- [2/4] Fetching Aurora credentials ---"

# Try main unit file first, then drop-in directory
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
# Common CLI args shared by both apply-commit and ingest-episode
# ---------------------------------------------------------------------------
COMMON_ARGS=(
  --repo-id "$REPO_ID"
  --metadata-backend postgres
  --metadata-dsn "$AURORA_DSN"
  --graph-backend neptune
  --neptune-host "$NEPTUNE_HOST"
  --llm-provider bedrock
  --llm-model us.anthropic.claude-sonnet-4-6
  --embed-provider bedrock
  --embed-dimensions 1024
  --execute
)

# ---------------------------------------------------------------------------
# 3. Commit history — all 58 commits oldest → newest
# ---------------------------------------------------------------------------
echo ""
echo "--- [3/4] Indexing commit history ---"

COMMITS=$(git -C "$REPO" log --reverse --format="%H")
TOTAL=$(echo "$COMMITS" | wc -l)
N=0

for SHA in $COMMITS; do
  N=$((N + 1))
  echo ""
  echo "[${N}/${TOTAL}] $(date -u +%H:%M:%S) apply-commit $SHA"
  "$PYTHON" -m context_graph apply-commit \
    "$REPO" \
    --commit "$SHA" \
    "${COMMON_ARGS[@]}" \
  || echo "WARNING: apply-commit $SHA failed, continuing"
done

echo ""
echo "=== Commit indexing complete (${N} commits processed) ==="

# ---------------------------------------------------------------------------
# 4. Docs ingestion — all markdown files under docs/
# ---------------------------------------------------------------------------
echo ""
echo "--- [4/4] Ingesting docs as episodes ---"

DOC_NUM=0
while IFS= read -r -d '' doc_file; do
  DOC_NUM=$((DOC_NUM + 1))
  relative="${doc_file#"$REPO"/}"
  source_id="doc-$(echo "$relative" | tr '/' '-' | sed 's/\.md$//')"
  echo ""
  echo "[doc ${DOC_NUM}] $(date -u +%H:%M:%S) $relative"
  "$PYTHON" -m context_graph ingest-episode \
    --source-type text \
    --source-id "$source_id" \
    --text-file "$doc_file" \
    "${COMMON_ARGS[@]}" \
  || echo "WARNING: ingest-episode $relative failed, continuing"
done < <(find "$REPO/docs" -name "*.md" -print0 | sort -z)

echo ""
echo "=== Docs ingestion complete ($DOC_NUM files) ==="
echo ""
echo "========================================================"
echo "  order-management indexing  finished at $(date -u)"
echo "========================================================"
