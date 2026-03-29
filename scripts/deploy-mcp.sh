#!/usr/bin/env bash
# deploy-mcp.sh — Build, upload, and deploy the Context Graph MCP server to EC2
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
#   2. Builds a tar.gz artifact from the internal Context Graph MCP server,
#      including the MCP wrappers, config, and context_graph package source.
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
CONTEXT_GRAPH_DIR="${REPO_ROOT}/context_graph"
MCP_CONFIG_DIR="${REPO_ROOT}/context_graph/server/mcp"

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

# Copy MCP entrypoints and config bundle
cp "${REPO_ROOT}/main.py" "${STAGING_DIR}/main.py"
cp "${MCP_CONFIG_DIR}/requirements.txt" "${STAGING_DIR}/requirements.txt"
mkdir -p "${STAGING_DIR}/config"
cp -a "${MCP_CONFIG_DIR}/config/." "${STAGING_DIR}/config/"

# Bundle context_graph source as context_graph_src/context_graph/ inside the artifact.
# The .pth file will add context_graph_src/ to sys.path, making `import context_graph` work.
mkdir -p "${STAGING_DIR}/context_graph_src/context_graph"
rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
  --exclude='docker-compose.local.yml' \
  "${CONTEXT_GRAPH_DIR}/" "${STAGING_DIR}/context_graph_src/context_graph/"
# Write a minimal pyproject.toml so pip treats context_graph_src/ as a package root
cat > "${STAGING_DIR}/context_graph_src/pyproject.toml" << 'PYPROJECT_EOF'
[project]
name = "context-graph-pkg"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "psycopg[binary]>=3.2,<4",
    "boto3>=1.34,<2",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["context_graph"]
PYPROJECT_EOF

tar -czf "${ARTIFACT}" -C "${STAGING_DIR}" .
echo "==> Artifact size: $(du -sh "${ARTIFACT}" | cut -f1)"

# ---------------------------------------------------------------------------
# Write updated deploy script locally
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

# Install context_graph package (bundled in the artifact as context_graph_src/)
CONTEXT_GRAPH_SRC="/opt/mcp_server/current/context_graph_src"
if [ -d "${CONTEXT_GRAPH_SRC}" ]; then
  echo "==> Installing context_graph package..."
  /opt/mcp_server/venv/bin/pip install -e "${CONTEXT_GRAPH_SRC}" --quiet
  for SP in $(/opt/mcp_server/venv/bin/python -c "import site; [print(p) for p in site.getsitepackages()]"); do
    echo "${CONTEXT_GRAPH_SRC}" > "${SP}/_context_graph_src.pth" 2>/dev/null || true
  done
fi

# Install remaining MCP server dependencies
echo "==> Installing MCP server dependencies..."
/opt/mcp_server/venv/bin/pip install -r /opt/mcp_server/current/requirements.txt --quiet

# Verify the internal runtime is importable after all dependencies are installed
/opt/mcp_server/venv/bin/python -c "from context_graph.storage.backend.neptune.driver import NeptuneBackendDriver; print('context_graph storage import OK')"
/opt/mcp_server/venv/bin/python -c "from context_graph.server.mcp.server import GraphitiService; print('context_graph mcp import OK')"

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
