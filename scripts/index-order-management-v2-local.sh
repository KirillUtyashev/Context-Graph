#!/usr/bin/env bash
# index-order-management-v2-local.sh
#
# Indexes order-management-service-extended as repo_id=order-management-v2
# using the local pipeline against Neptune + Aurora via SSM port-forward tunnels.
#
# PREREQUISITES (run once before this script):
#
#   1. Install SSM Session Manager plugin (Ubuntu/WSL2):
#        curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" \
#          -o /tmp/session-manager-plugin.deb
#        sudo dpkg -i /tmp/session-manager-plugin.deb
#
#   2. Create Python venv (from repo root):
#        uv sync
#
#   3. Configure AWS credentials with SSO or set AWS_PROFILE:
#        export AWS_PROFILE=<your-profile>   # must have neptune-db:*, rds:*, bedrock:* perms
#        aws sts get-caller-identity         # verify
#
#   4. Export the Aurora password (see Step A below to fetch it):
#        export AURORA_PASSWORD="<password>"
#
#   5. Start SSM tunnels in two separate terminals BEFORE running this script:
#
#      Terminal A — Neptune:
#        aws ssm start-session \
#          --target i-01ae87d724ae9ef95 \
#          --document-name AWS-StartPortForwardingSessionToRemoteHost \
#          --parameters '{"host":["context-graph-dev-neptune.cluster-cd2euaamm1l1.ca-central-1.neptune.amazonaws.com"],"portNumber":["8182"],"localPortNumber":["8182"]}' \
#          --region ca-central-1
#
#      Terminal B — Aurora:
#        aws ssm start-session \
#          --target i-01ae87d724ae9ef95 \
#          --document-name AWS-StartPortForwardingSessionToRemoteHost \
#          --parameters '{"host":["context-graph-dev-aurora.cluster-cd2euaamm1l1.ca-central-1.rds.amazonaws.com"],"portNumber":["5432"],"localPortNumber":["5432"]}' \
#          --region ca-central-1
#
#   6. Route Neptune hostname to localhost (so TLS cert matches over the tunnel):
#        echo "127.0.0.1  context-graph-dev-neptune.cluster-cd2euaamm1l1.ca-central-1.neptune.amazonaws.com" \
#          | sudo tee -a /etc/hosts
#      Remember to remove this line after indexing is complete.
#
# STEP A — Fetch Aurora password (run once, set AURORA_PASSWORD before this script):
#
#   AURORA_SECRET_ARN=$(aws secretsmanager list-secrets \
#     --region ca-central-1 \
#     --query "SecretList[?contains(Name, 'aurora') || contains(Name, 'context-graph')].ARN | [0]" \
#     --output text)
#   export AURORA_PASSWORD=$(aws secretsmanager get-secret-value \
#     --secret-id "$AURORA_SECRET_ARN" \
#     --query SecretString --output text \
#     --region ca-central-1)
#
set -uo pipefail

LOG=/tmp/index-order-management-v2.log
exec > >(tee -a "$LOG") 2>&1

echo "========================================================"
echo "  order-management-v2 indexing  started at $(date -u)"
echo "========================================================"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REPO_ID=order-management-v2
NEPTUNE_HOST=context-graph-dev-neptune.cluster-cd2euaamm1l1.ca-central-1.neptune.amazonaws.com
REGION=ca-central-1
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
PYTHON="${REPO_ROOT}/.venv/bin/python"
REPO="${REPO_ROOT}/order-management-service-extended"

export AWS_DEFAULT_REGION=$REGION

# ---------------------------------------------------------------------------
# Validate prerequisites
# ---------------------------------------------------------------------------
if [[ -z "${AURORA_PASSWORD:-}" ]]; then
  echo "ERROR: AURORA_PASSWORD is not set. See the STEP A comment at the top of this script." >&2
  exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: Python venv not found at $PYTHON" >&2
  echo "       Run: uv sync   (from $REPO_ROOT)" >&2
  exit 1
fi

if [[ ! -d "$REPO/.git" ]]; then
  echo "ERROR: Repo not found at $REPO" >&2
  exit 1
fi

AURORA_DSN="postgresql://postgres:${AURORA_PASSWORD}@localhost:5432/context_graph"

# ---------------------------------------------------------------------------
# Common CLI args
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
# 1. Commit history — all commits oldest → newest
# ---------------------------------------------------------------------------
echo ""
echo "--- [1/2] Indexing commit history ---"

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

# ---------------------------------------------------------------------------
# 2. Docs ingestion — all markdown files under docs/
# ---------------------------------------------------------------------------
echo ""
echo "--- [2/2] Ingesting docs as episodes ---"

DOC_NUM=0
DOC_FAIL=0

if [[ -d "$REPO/docs" ]]; then
  while IFS= read -r -d '' doc_file; do
    DOC_NUM=$((DOC_NUM + 1))
    relative="${doc_file#"$REPO"/}"
    source_id="doc-$(echo "$relative" | tr '/' '-' | sed 's/\.md$//')"
    echo ""
    echo "[doc ${DOC_NUM}] $(date -u +%H:%M:%S) $relative"
    if "$PYTHON" -m context_graph ingest-episode \
      --source-type text \
      --source-id "$source_id" \
      --text-file "$doc_file" \
      "${COMMON_ARGS[@]}"; then
      true
    else
      DOC_FAIL=$((DOC_FAIL + 1))
      echo "WARNING: ingest-episode $relative failed (total failures: $DOC_FAIL), continuing"
    fi
  done < <(find "$REPO/docs" -name "*.md" -print0 | sort -z)
else
  echo "No docs/ directory found at $REPO/docs — skipping"
fi

echo ""
echo "=== Docs ingestion complete: ${DOC_NUM} files, ${DOC_FAIL} failures ==="

echo ""
echo "========================================================"
echo "  order-management-v2 indexing  finished at $(date -u)"
echo "  Commits: ${N} processed, ${FAIL} failed"
echo "  Docs:    ${DOC_NUM} processed, ${DOC_FAIL} failed"
echo "  Log:     $LOG"
echo "========================================================"
