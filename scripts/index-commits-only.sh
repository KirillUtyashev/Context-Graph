#!/usr/bin/env bash
# index-commits-only.sh — Index all 58 commits of order-management-service-extended.
# Run on EC2 via SSM. Commit SHAs are hardcoded (git not available on EC2).
# The repo tarball must already be at /tmp/order-management-service-extended/ from
# the first run of index-order-management.sh.
set -uo pipefail

LOG=/tmp/index-commits.log
exec > >(tee -a "$LOG") 2>&1

echo "========================================================"
echo "  order-management COMMIT indexing started at $(date -u)"
echo "========================================================"

REPO_ID=order-management
NEPTUNE_HOST=context-graph-dev-neptune.cluster-cd2euaamm1l1.ca-central-1.neptune.amazonaws.com
AURORA_HOST=context-graph-dev-aurora.cluster-cd2euaamm1l1.ca-central-1.rds.amazonaws.com
REGION=ca-central-1
PYTHON=/opt/mcp_server/venv/bin/python
REPO=/tmp/order-management-service-extended
S3_BUCKET=context-graph-dev-472730591037-mcp-artifacts

export AWS_DEFAULT_REGION=$REGION

# Re-download the repo if not present
if [ ! -d "$REPO" ]; then
  echo "Repo not found, downloading from S3..."
  aws s3 cp "s3://${S3_BUCKET}/mcp-server/order-management.tar.gz" /tmp/order-management.tar.gz
  tar -xzf /tmp/order-management.tar.gz -C /tmp/
fi
echo "Repo at $REPO"

# Get Aurora credentials
AURORA_PASSWORD_SECRET_ARN=""
for f in /etc/systemd/system/mcp-server.service \
          /etc/systemd/system/mcp-server.service.d/*.conf; do
  [[ -f "$f" ]] || continue
  ARN=$(grep -oP 'AURORA_PASSWORD_SECRET_ARN=\K\S+' "$f" | head -1 || true)
  if [[ -n "$ARN" ]]; then
    AURORA_PASSWORD_SECRET_ARN="$ARN"
    break
  fi
done
[[ -n "$AURORA_PASSWORD_SECRET_ARN" ]] || { echo "ERROR: no AURORA_PASSWORD_SECRET_ARN"; exit 1; }

AURORA_PASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id "$AURORA_PASSWORD_SECRET_ARN" \
  --query SecretString --output text --region "$REGION")
AURORA_DSN="postgresql://postgres:${AURORA_PASSWORD}@${AURORA_HOST}/context_graph"
echo "Aurora DSN ready"

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

# All 58 commits — oldest (index 0) to newest (index 57)
COMMITS=(
  b84ac35c2b714d0d07899c25c2fff718f4b4a664
  09525d381ba39a0bbe15316326a8a559ef73ade9
  ab8a8e94a21b3ee6248c96232027c958719e0608
  574593bd8f7beafbf2ebfd2fc2ac6ca0a2cbab43
  11b12a7ceb2762148b2d015529c049c07755818a
  51af1ff06e7032deb398dc6763fecd1af3c8f2ba
  41237ddd76bef38e81b9abcd882314b7044ebd48
  4392b2fbe646e3f24083a931c88d29246f45b43b
  ef296d0ecb8a9ba4a78ae497f0b7ce5e66035253
  522a059ad8460d4c25fb49075a4f3fb68a29c916
  763245adc0398d7118422d19de7e21701524373f
  2ee598395b2781fef1b098e2993d9df770365ef3
  13641f76920c63da9112078ccf308197342a8519
  d1641d891eeeb0c3cf6bbae7f707f97cfa16f845
  88736d1e77ccd9c983f5977f4d3c279cf18ba833
  0f60168b1163410e10764e840b98938d9fbf5a7d
  dd230a4517060919ff004b0843fe092f841d6a81
  47e9b68cdf0daca12ed8f35bc5f219c984bfa18f
  787f1e287a3f17935857822b00915cd83427fa87
  2fc16444dea9720d8f62fad82ae24628cebc45a9
  0e20a1dfd923c372a9614ce4bd112ca5dc739275
  49888e82405b9bd300916814cf6fe46dad2bd0b7
  acfffcf5ca771a5bbfdccecd9334db38001749e5
  748a1c7a21765d3cec6c3c9933540860a363c67b
  ac4dccb4142cf287bf809fed410f9aca798c3155
  8e8ada33ce4134827b36f3e798f73e0707cc5721
  bbd5cf1cbd97260b2d8ad99fa8ca770705b57e8e
  185bd8b83a090c08cfa3bed16a274dbbe77dd6fa
  79252e5d1150869b4d59de6cb2a7c25de11b4e5f
  de1240174507c11b044d5aaa2086ec39c055819b
  1fb5a89f9ff05bbc50dfec0b4ec7f415f0532dce
  2d5eae67a0842bd93f2ce10de5692fcf3c7d4f50
  087b5396b21ea1ebbc298c40782f1350d7524686
  a5f4f92bab9a4749dd9dbbbd6f12460141d0ad28
  68d8d844b2ca08bc72353bbd4dc11e014205f38d
  b3f25761d98b63283629001d9e6efdad18beee40
  c9fca929993b9db1a5b805ca8006eab882d2cf25
  e9b4a34fc799acacca7880afd030e9bc7e2c2174
  183d0266feb90656c7a7f3d172d8f4f31bce9ed7
  3d45511470e31923564499670e6342c9e1e227e2
  cd61c59113d2cb667fca16fbfee8e3799518faf1
  4ac8e46997b28886533f6424940fb4e230577851
  160e8fc62db745ed90f13b60a07adfff44cdd254
  365956c6b1fb5145a94503d1150d3122bb6e6cbb
  72a43b207409bf66a4e34c99f11e0c9d36d1e645
  578a92732c2d204b5a8db0a6a2659c5d0675b5b0
  27ac758258c02c5fd6c01cf09c8aeff45c64be22
  8cecb11058dc1e35613ce9f921ff135f2af5d15b
  0ca608721d21df5ba0fbf02bca6b463110f303fc
  52c9862e358229122ddbeae464f24265b3f2e90b
  a84ef2dd3a2b2447d95e17be3244dbdf747d1c1a
  359e620825b31fbf498c14c1453e9d1ffe9eb593
  ab27a4498eb6ac5af17d98b22c96f43118ab6a45
  628022c360b7e73b6750b94dc315146d2a67032b
  8a840a9cb438a745b9fda0b859b3d082cbadaf9a
  e063e2bc2f823ef2157fc187e124814588923160
  0ac4009f959f5c891f47dc3fa6493eaf91a040d1
  6490a76ff9fdf108f1cd241ef4459ad6a51b3f14
)

TOTAL=${#COMMITS[@]}
N=0
FAILED=0

for SHA in "${COMMITS[@]}"; do
  N=$((N + 1))
  echo ""
  echo "[${N}/${TOTAL}] $(date -u +%H:%M:%S) apply-commit $SHA"
  "$PYTHON" -m context_graph apply-commit \
    "$REPO" \
    --commit "$SHA" \
    "${COMMON_ARGS[@]}" \
  && echo "OK $SHA" \
  || { echo "WARNING: apply-commit $SHA failed"; FAILED=$((FAILED + 1)); }
done

echo ""
echo "========================================================"
echo "  Commit indexing complete: ${N} processed, ${FAILED} failed"
echo "  Finished at $(date -u)"
echo "========================================================"
