#!/usr/bin/env bash
# deploy-mcp.sh — Build, upload, and deploy the Context Graph MCP server to EC2

set -euo pipefail

ENV="dev"
for arg in "$@"; do
  case $arg in
    --env=*) ENV="${arg#*=}" ;;
    --env) shift; ENV="${1:-dev}" ;;
  esac
done

AWS_REGION="${AWS_REGION:-ca-central-1}"
PROJECT_NAME="context-graph"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTEXT_GRAPH_DIR="${REPO_ROOT}/context_graph"
MCP_APP_DIR="${REPO_ROOT}/mcp_server"

echo "==> Deploying MCP server to env=${ENV} region=${AWS_REGION}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${PROJECT_NAME}-${ENV}-${ACCOUNT_ID}-mcp-artifacts"
NAME_TAG="${PROJECT_NAME}-${ENV}-mcp"

INSTANCE_ID="$(aws ec2 describe-instances \
  --region "${AWS_REGION}" \
  --filters "Name=tag:Name,Values=${NAME_TAG}" "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].InstanceId" \
  --output text)"

if [[ "${INSTANCE_ID}" == "None" || -z "${INSTANCE_ID}" ]]; then
  echo "ERROR: No running EC2 instance found with Name tag '${NAME_TAG}'" >&2
  exit 1
fi

SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
ARTIFACT="/tmp/mcp_server_${SHA}.tar.gz"
STAGING_DIR="/tmp/mcp_server_staging_${SHA}"

echo "==> Building artifact at ${ARTIFACT} (sha=${SHA})"

rm -rf "${STAGING_DIR}"
mkdir -p "${STAGING_DIR}"

mkdir -p "${STAGING_DIR}/context_graph_src/context_graph"
rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
  --exclude='docker-compose.local.yml' \
  "${CONTEXT_GRAPH_DIR}/" "${STAGING_DIR}/context_graph_src/context_graph/"
cat > "${STAGING_DIR}/context_graph_src/pyproject.toml" <<'PYPROJECT_EOF'
[project]
name = "context-graph"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = [
    "neo4j>=5.22,<6",
    "langchain-aws>=0.2,<1",
    "opensearch-py>=2.7,<3",
    "psycopg[binary]>=3.2,<4",
    "boto3>=1.39,<2",
    "openai>=1.91,<2",
    "tenacity>=9,<10",
    "pydantic>=2.11,<3",
    "numpy>=1,<3",
    "httpx>=0.27,<1",
    "python-dotenv>=1.0,<2",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["context_graph"]
PYPROJECT_EOF

mkdir -p "${STAGING_DIR}/mcp_server_src"
rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
  "${MCP_APP_DIR}/src/" "${STAGING_DIR}/mcp_server_src/src/"
rsync -a --exclude='__pycache__' \
  "${MCP_APP_DIR}/config/" "${STAGING_DIR}/mcp_server_src/config/"
cp "${MCP_APP_DIR}/pyproject.toml" "${STAGING_DIR}/mcp_server_src/pyproject.toml"
cp "${MCP_APP_DIR}/README.md" "${STAGING_DIR}/mcp_server_src/README.md"
cp "${MCP_APP_DIR}/.env.example" "${STAGING_DIR}/mcp_server_src/.env.example"

tar -czf "${ARTIFACT}" -C "${STAGING_DIR}" .
echo "==> Artifact size: $(du -sh "${ARTIFACT}" | cut -f1)"

DEPLOY_SCRIPT="/tmp/deploy-mcp-server-${SHA}.sh"
cat > "${DEPLOY_SCRIPT}" <<'DEPLOY_SCRIPT_CONTENT'
#!/usr/bin/env bash
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

PYTHON_BIN="python3"
for py in python3.12 python3.11; do
  if command -v $py &>/dev/null; then PYTHON_BIN=$py; break; fi
done
echo "==> Using $PYTHON_BIN ($($PYTHON_BIN --version))"

$PYTHON_BIN -m venv /opt/mcp_server/venv
/opt/mcp_server/venv/bin/pip install --upgrade pip --quiet

echo "==> Installing context_graph core package..."
/opt/mcp_server/venv/bin/pip install -e /opt/mcp_server/current/context_graph_src --quiet

echo "==> Installing standalone MCP app..."
/opt/mcp_server/venv/bin/pip install -e /opt/mcp_server/current/mcp_server_src --quiet

/opt/mcp_server/venv/bin/python -c "from context_graph.storage.backend.neptune.driver import NeptuneBackendDriver; print('context_graph storage import OK')"
/opt/mcp_server/venv/bin/context-graph-mcp --help >/dev/null
echo "context-graph-mcp entrypoint OK"

systemctl daemon-reload
systemctl enable --now mcp-server.service
systemctl restart mcp-server.service

echo "==> Deployment complete"
systemctl status mcp-server.service --no-pager -l | tail -n 20
DEPLOY_SCRIPT_CONTENT
chmod +x "${DEPLOY_SCRIPT}"

S3_KEY="mcp-server/${SHA}.tar.gz"
S3_DEPLOY_KEY="mcp-server/deploy-mcp-server.sh"

echo "==> Uploading artifact to s3://${BUCKET}/${S3_KEY}"
aws s3 cp "${ARTIFACT}" "s3://${BUCKET}/${S3_KEY}" --region "${AWS_REGION}"
aws s3 cp "${ARTIFACT}" "s3://${BUCKET}/mcp-server/latest.tar.gz" --region "${AWS_REGION}"

echo "==> Uploading deploy script to s3://${BUCKET}/${S3_DEPLOY_KEY}"
aws s3 cp "${DEPLOY_SCRIPT}" "s3://${BUCKET}/${S3_DEPLOY_KEY}" --region "${AWS_REGION}"

echo "==> Triggering deployment on EC2 via SSM..."

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
aws ssm wait command-executed \
  --region "${AWS_REGION}" \
  --command-id "${CMD_ID}" \
  --instance-id "${INSTANCE_ID}"

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

echo "==> Deployment complete. SHA=${SHA}"
echo "==> MCP server: http://${PUBLIC_IP}:8000/mcp/"
