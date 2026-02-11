#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOTSTRAP_DIR="${ROOT_DIR}/bootstrap"

bucket="$(terraform -chdir="${BOOTSTRAP_DIR}" output -raw tf_state_bucket_name)"
table="$(terraform -chdir="${BOOTSTRAP_DIR}" output -raw tf_lock_table_name)"
region="$(terraform -chdir="${BOOTSTRAP_DIR}" output -raw aws_region)"

init_stack () {
  local stack="$1"
  local key="$2"

  terraform -chdir="${ROOT_DIR}/stacks/${stack}" init -reconfigure \
    -backend-config="bucket=${bucket}" \
    -backend-config="dynamodb_table=${table}" \
    -backend-config="region=${region}" \
    -backend-config="key=${key}"
}

init_stack "dev"  "context-graph/dev/terraform.tfstate"
init_stack "prod" "context-graph/prod/terraform.tfstate"

echo "Initialized dev/prod backends."

