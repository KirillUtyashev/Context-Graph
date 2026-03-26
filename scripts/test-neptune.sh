#!/usr/bin/env bash
# test-neptune.sh — Smoke-test the Neptune connection and MCP server health from EC2
#
# Usage:
#   AWS_PROFILE=<your-profile> bash scripts/test-neptune.sh [--env dev]
#
# What this does:
#   1. Runs a Python Neptune connection test on the EC2 instance via SSM.
#      (Neptune is VPC-only; this must run from inside the VPC.)
#   2. Checks the MCP server's systemd service status.
#
# Run AFTER deploy-mcp.sh has completed successfully.

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
NAME_TAG="${PROJECT_NAME}-${ENV}-mcp"

echo "==> Neptune smoke test — env=${ENV} region=${AWS_REGION}"

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
# 1. Neptune Python connection test via SSM
#    We write the Python script to /tmp on the instance, then execute it.
# ---------------------------------------------------------------------------
echo "==> Running Neptune connection test on EC2..."

# Each element of the commands array is a separate shell command.
# We write the python test to a file first to avoid quoting problems.
CMD_ID="$(aws ssm send-command \
  --region "${AWS_REGION}" \
  --instance-ids "${INSTANCE_ID}" \
  --document-name "AWS-RunShellScript" \
  --comment "Neptune smoke test" \
  --parameters 'commands=[
    "cat > /tmp/neptune_test.py << '\''PYEOF'\''",
    "import asyncio, os, subprocess",
    "from context_graph.storage.graph.neptune_driver import NeptuneDriver",
    "host = os.environ.get(\"NEPTUNE_HOST\", \"\")",
    "if not host:",
    "    r = subprocess.run([\"bash\",\"-c\",\"grep NEPTUNE_HOST /etc/systemd/system/mcp-server.service\"], capture_output=True, text=True)",
    "    for line in r.stdout.splitlines():",
    "        if \"NEPTUNE_HOST=\" in line:",
    "            host = line.split(\"NEPTUNE_HOST=\", 1)[-1].strip()",
    "print(f\"Connecting to: {host}\")",
    "driver = NeptuneDriver(host=host)",
    "async def test():",
    "    await driver.build_indices_and_constraints()",
    "    print(\"SUCCESS: Neptune OK\")",
    "asyncio.run(test())",
    "PYEOF",
    "sudo -u mcp /opt/mcp_server/venv/bin/python /tmp/neptune_test.py 2>&1"
  ]' \
  --query "Command.CommandId" \
  --output text)"

echo "==> SSM command: ${CMD_ID} — waiting..."
aws ssm wait command-executed \
  --region "${AWS_REGION}" \
  --command-id "${CMD_ID}" \
  --instance-id "${INSTANCE_ID}" || true

echo "==> Neptune test result:"
aws ssm get-command-invocation \
  --region "${AWS_REGION}" \
  --command-id "${CMD_ID}" \
  --instance-id "${INSTANCE_ID}" \
  --query "[Status, StandardOutputContent, StandardErrorContent]" \
  --output text

# ---------------------------------------------------------------------------
# 2. MCP server health check
# ---------------------------------------------------------------------------
echo ""
echo "==> Checking MCP server health from EC2..."

HEALTH_CMD_ID="$(aws ssm send-command \
  --region "${AWS_REGION}" \
  --instance-ids "${INSTANCE_ID}" \
  --document-name "AWS-RunShellScript" \
  --comment "MCP health check" \
  --parameters 'commands=[
    "systemctl status mcp-server.service --no-pager -l | tail -n 30",
    "sleep 2",
    "curl -s -o /dev/null -w \"HTTP %{http_code}\" http://localhost:8000/health 2>&1 || echo \"(health endpoint not available - check /mcp/ instead)\""
  ]' \
  --query "Command.CommandId" \
  --output text)"

echo "==> SSM command: ${HEALTH_CMD_ID} — waiting..."
aws ssm wait command-executed \
  --region "${AWS_REGION}" \
  --command-id "${HEALTH_CMD_ID}" \
  --instance-id "${INSTANCE_ID}" || true

echo "==> MCP server status:"
aws ssm get-command-invocation \
  --region "${AWS_REGION}" \
  --command-id "${HEALTH_CMD_ID}" \
  --instance-id "${INSTANCE_ID}" \
  --query "[Status, StandardOutputContent, StandardErrorContent]" \
  --output text

# ---------------------------------------------------------------------------
# Print Cursor config
# ---------------------------------------------------------------------------
echo ""
PUBLIC_IP="$(aws ec2 describe-instances \
  --region "${AWS_REGION}" \
  --instance-ids "${INSTANCE_ID}" \
  --query "Reservations[0].Instances[0].PublicIpAddress" \
  --output text)"

echo "==> Smoke test complete."
echo "==> If Neptune shows 'SUCCESS' and MCP server is 'active (running)', add to ~/.cursor/mcp.json:"
echo ""
cat <<EOF
{
  "mcpServers": {
    "graphiti": {
      "url": "http://${PUBLIC_IP}:8000/mcp/"
    }
  }
}
EOF
