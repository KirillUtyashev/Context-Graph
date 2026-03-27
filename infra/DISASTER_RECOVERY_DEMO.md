

## 1. Show production is up and document current state

**Narrate:** “We’re in prod. I’ll show the current infrastructure and key outputs.”

```bash
cd /home/kai/csc490/Context-Graph/infra/stacks/prod
terraform plan
```

```bash
terraform output
```

```bash
curl "$(terraform output -raw augment_function_url)"
```

**Narrate:** “We have VPC, Aurora, Neptune, Lambdas, EC2 MCP server, and Bedrock proxy. Configuration and access controls are in Terraform and tfvars.”

---

## 2. Disaster: delete the production environment

**Narrate:** “We’re simulating a disaster by destroying the entire production environment with Terraform.”

```bash
terraform destroy -auto-approve
```

Wait for destroy to finish. When it’s done:

```bash
terraform output
# Should show empty or errors (no resources)
```

**Narrate:** “Production is gone: no databases, no Lambdas, no EC2, no VPC. Only our IaC code and remote state remain.”

---

## 3. Restore production using IaC only

**Narrate:** “We restore everything from Infrastructure as Code—no manual console work.”

```bash
terraform init -reconfigure
```


Then apply:

```bash
terraform apply -auto-approve
```

Wait for apply to complete. Then:

```bash
terraform output
```

**Narrate:** “Terraform recreated VPC, subnets, NAT, Aurora, Neptune, Lambdas with Function URLs, EC2 MCP instance, Bedrock VPC endpoint, and the Bedrock proxy. Configuration and security are again defined by our code and tfvars.”

---

## 4. Verification of system functionality

**Narrate:** “We verify that the restored system is working.”

### 4.1 Data processing services / deployed applications

```bash
curl "$(terraform output -raw augment_function_url)"

 aws bedrock-runtime converse --region ca-central-1 --model-id global.amazon.nova-2-lite-v1:0 --messages '[{"role":"user","content":[{"text":"Reply with the single word OK."}]}]' --inference-config '{"maxTokens":16,"temperature":0}'
```

**MCP server (EC2):**

```bash
MCP_URL=$(terraform output -raw mcp_url)
curl -s -o /dev/null -w "mcp_url HTTP status: %{http_code}\n" "$MCP_URL/"
```

### 4.2 Database systems and their data

**Narrate:** “Aurora and Neptune are recreated by Terraform. They start empty; data would be repopulated by our pipelines and application.”

Show that the infrastructure exists:

```bash
cd /home/kai/csc490/Context-Graph/infra/stacks/prod
terraform output aurora_endpoint
terraform output neptune_endpoint
```
