#!/usr/bin/env bash
# deploy-mcp.sh — Build, upload, and deploy the graphiti MCP server to EC2
#
# Usage:
#   AWS_PROFILE=<your-profile> bash scripts/deploy-mcp.sh [--env dev]
#
# Prerequisites:
#   - AWS credentials active (via AWS_PROFILE or instance role / env vars)
#   - aws CLI and rsync installed locally
#   - The EC2 instance is running (terraform apply already done)
#
# What this script does:
#   1. Resolves the artifacts S3 bucket name and EC2 instance ID from AWS.
#   2. Builds a tar.gz artifact from vendor/graphiti/mcp_server/ and the
#      graphiti-core source (vendor/graphiti/), bundled as graphiti_core_src/.
#   3. Uploads the artifact + a new deploy script to s3://<bucket>/mcp-server/
#   4. Invokes the updated deploy script on EC2 via SSM.

set -euo pipefail

ENV="dev"
for arg in "$@"; do
  case $arg in
    --env=*) ENV="${arg#*=}" ;;
    --env)   shift; ENV="${1:-dev}" ;;
  esac
done

AWS_REGION="${AWS_REGION:-ca-central-1}"
PROJECT_NAME="context-graph"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MCP_SERVER_DIR="${REPO_ROOT}/vendor/graphiti/mcp_server"
GRAPHITI_CORE_DIR="${REPO_ROOT}/vendor/graphiti"

echo "==> Deploying MCP server to env=${ENV} region=${AWS_REGION}"

# ---------------------------------------------------------------------------
# Resolve AWS account + bucket + instance
# ---------------------------------------------------------------------------
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${PROJECT_NAME}-${ENV}-${ACCOUNT_ID}-mcp-artifacts"
NAME_TAG="${PROJECT_NAME}-${ENV}-mcp"

echo "==> S3 bucket: ${BUCKET}"
echo "==> Looking for EC2 instance with Name tag: ${NAME_TAG}"

INSTANCE_ID="$(aws ec2 describe-instances \
  --region "${AWS_REGION}" \
  --filters "Name=tag:Name,Values=${NAME_TAG}" "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].InstanceId" \
  --output text)"

if [[ "${INSTANCE_ID}" == "None" || -z "${INSTANCE_ID}" ]]; then
  echo "ERROR: No running EC2 instance found with Name tag '${NAME_TAG}'" >&2
  exit 1
fi
echo "==> EC2 instance: ${INSTANCE_ID}"

# ---------------------------------------------------------------------------
# Build artifact
# ---------------------------------------------------------------------------
SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
ARTIFACT="/tmp/mcp_server_${SHA}.tar.gz"

echo "==> Building artifact at ${ARTIFACT} (sha=${SHA})"

STAGING_DIR="/tmp/mcp_server_staging_${SHA}"
rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}"

# Copy mcp_server contents (main.py, src/, config/, requirements.txt, etc.)
cp -a "${MCP_SERVER_DIR}/." "${STAGING_DIR}/"

# Bundle graphiti-core source as graphiti_core_src/ inside the artifact.
# On EC2, the deploy script will: pip install -e ./graphiti_core_src[neptune]
mkdir -p "${STAGING_DIR}/graphiti_core_src"
rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
  --exclude='mcp_server' --exclude='uv.lock' --exclude='.venv' \
  "${GRAPHITI_CORE_DIR}/" "${STAGING_DIR}/graphiti_core_src/"

tar -czf "${ARTIFACT}" -C "${STAGING_DIR}" .
echo "==> Artifact size: $(du -sh "${ARTIFACT}" | cut -f1)"

# ---------------------------------------------------------------------------
# Write updated deploy script locally (handles vendored graphiti-core)
# ---------------------------------------------------------------------------
DEPLOY_SCRIPT="/tmp/deploy-mcp-server-${SHA}.sh"
cat > "${DEPLOY_SCRIPT}" <<'DEPLOY_SCRIPT_CONTENT'
#!/usr/bin/env bash
# deploy-mcp-server.sh — installed at /usr/local/bin/deploy-mcp-server.sh on EC2
set -euo pipefail

usage() {
  echo "Usage: deploy-mcp-server.sh <artifacts-bucket> <sha>" >&2
  exit 2
}

bucket="${1:-}"
sha="${2:-}"
[[ -n "${bucket}" && -n "${sha}" ]] || usage

key="mcp-server/${sha}.tar.gz"
release_dir="/opt/mcp_server/releases/${sha}"

mkdir -p /opt/mcp_server/releases
rm -rf "${release_dir}"
mkdir -p "${release_dir}"

aws s3 cp "s3://${bucket}/${key}" /tmp/mcp_server.tar.gz
tar -xzf /tmp/mcp_server.tar.gz -C "${release_dir}"

rm -rf /opt/mcp_server/current
ln -sfn "${release_dir}" /opt/mcp_server/current

id -u mcp >/dev/null 2>&1 || useradd -r -s /sbin/nologin mcp
chown -R mcp:mcp /opt/mcp_server

# Prefer python3.11+ (graphiti-core requires >=3.10; AL2023 default is 3.9)
PYTHON_BIN="python3"
for py in python3.12 python3.11; do
  if command -v $py &>/dev/null; then PYTHON_BIN=$py; break; fi
done
echo "==> Using $PYTHON_BIN ($($PYTHON_BIN --version))"

$PYTHON_BIN -m venv /opt/mcp_server/venv
/opt/mcp_server/venv/bin/pip install --upgrade pip --quiet

# Install vendored graphiti-core (bundled in the artifact as graphiti_core_src/)
GRAPHITI_SRC="/opt/mcp_server/current/graphiti_core_src"
if [ -d "${GRAPHITI_SRC}" ]; then
  echo "==> Installing vendored graphiti-core[neptune]..."
  /opt/mcp_server/venv/bin/pip install -e "${GRAPHITI_SRC}[neptune]" --quiet

  # AL2023 puts the dist-info in lib64 but .pth files in lib; ensure graphiti_core
  # is importable by writing a .pth file to both site-packages locations.
  for SP in $(/opt/mcp_server/venv/bin/python -c "import site; [print(p) for p in site.getsitepackages()]"); do
    echo "${GRAPHITI_SRC}" > "${SP}/_graphiti_core_src.pth" 2>/dev/null || true
  done
fi

# Install remaining MCP server dependencies
echo "==> Installing MCP server dependencies..."
/opt/mcp_server/venv/bin/pip install -r /opt/mcp_server/current/requirements.txt --quiet

# Verify graphiti_core is importable before starting the service
/opt/mcp_server/venv/bin/python -c "from graphiti_core.driver.neptune_driver import NeptuneDriver; print('graphiti_core import OK')"

systemctl daemon-reload
systemctl enable --now mcp-server.service
systemctl restart mcp-server.service

echo "==> Deployment complete"
systemctl status mcp-server.service --no-pager -l | tail -n 20
DEPLOY_SCRIPT_CONTENT
chmod +x "${DEPLOY_SCRIPT}"

# ---------------------------------------------------------------------------
# Upload artifact + deploy script to S3
# ---------------------------------------------------------------------------
S3_KEY="mcp-server/${SHA}.tar.gz"
S3_DEPLOY_KEY="mcp-server/deploy-mcp-server.sh"

echo "==> Uploading artifact to s3://${BUCKET}/${S3_KEY}"
aws s3 cp "${ARTIFACT}" "s3://${BUCKET}/${S3_KEY}" --region "${AWS_REGION}"
aws s3 cp "${ARTIFACT}" "s3://${BUCKET}/mcp-server/latest.tar.gz" --region "${AWS_REGION}"

echo "==> Uploading deploy script to s3://${BUCKET}/${S3_DEPLOY_KEY}"
aws s3 cp "${DEPLOY_SCRIPT}" "s3://${BUCKET}/${S3_DEPLOY_KEY}" --region "${AWS_REGION}"

echo "==> Upload complete"

# ---------------------------------------------------------------------------
# Deploy via SSM — download updated deploy script then run it
# ---------------------------------------------------------------------------
echo "==> Triggering deployment on EC2 via SSM..."

# SSM parameters: an array of commands (each element is one shell command)
CMD_ID="$(aws ssm send-command \
  --region "${AWS_REGION}" \
  --instance-ids "${INSTANCE_ID}" \
  --document-name "AWS-RunShellScript" \
  --comment "Deploy MCP server ${SHA}" \
  --parameters "commands=[
    \"aws s3 cp s3://${BUCKET}/${S3_DEPLOY_KEY} /usr/local/bin/deploy-mcp-server.sh --region ${AWS_REGION}\",
    \"chmod +x /usr/local/bin/deploy-mcp-server.sh\",
    \"/usr/local/bin/deploy-mcp-server.sh ${BUCKET} ${SHA}\"
  ]" \
  --query "Command.CommandId" \
  --output text)"

echo "==> SSM command ID: ${CMD_ID}"
echo "==> Waiting for deployment (may take ~3 min for pip install)..."

# SSM wait has a default timeout of ~1 hour
aws ssm wait command-executed \
  --region "${AWS_REGION}" \
  --command-id "${CMD_ID}" \
  --instance-id "${INSTANCE_ID}"

echo ""
echo "==> SSM command output:"
aws ssm get-command-invocation \
  --region "${AWS_REGION}" \
  --command-id "${CMD_ID}" \
  --instance-id "${INSTANCE_ID}" \
  --query "[Status, StandardOutputContent, StandardErrorContent]" \
  --output text

PUBLIC_IP="$(aws ec2 describe-instances \
  --region "${AWS_REGION}" \
  --instance-ids "${INSTANCE_ID}" \
  --query "Reservations[0].Instances[0].PublicIpAddress" \
  --output text)"

echo ""
echo "==> Deployment complete. SHA=${SHA}"
echo "==> MCP server: http://${PUBLIC_IP}:8000/mcp/"
echo ""
echo "==> Run 'bash scripts/test-neptune.sh' to verify the Neptune connection."
