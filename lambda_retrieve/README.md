# Lambda: `lambda_augment`

This Lambda handles **initial ingestion** and **write-time augmentation** for a project's context graph.

## What it does

- **Ingests a GitHub org**:
    - Lists public repos in the org.
    - Creates `Project`, `CodeHost`, and `Repo` nodes in Neptune (project‑prefixed labels).
    - Optionally creates a `Doc` node for a linked Google Doc.
    - Fetches each repo's README, embeds it, and creates:
        - `Repo` → `Doc` (`readme`) → `Embedding` chain.

- **Augments or deletes**:
    - `action: "augment"` — currently a stub.
    - `action: "delete"` — deletes all nodes/edges for a given `project_id` (via prefixed labels).

## Entry point

- **Handler**: `lambda_function.handler`
- **Trigger**: Lambda Function URL (expects HTTP `POST` with JSON body).

## Payloads

### Ingest org

```json
{
  "action": "ingest_org",
  "project_id": "proj-123",
  "project_name": "My Project",
  "owner": "team-xyz",
  "github_org_url": "https://github.com/my-org",
  "google_docs_url": "https://docs.google.com/document/d/...",
  "max_repos": 50
}
```

### Response (200 on success)

```json
{
"ok": true,
"action": "ingest_org",
"org": "my-org",
"repo_count": 12,
"readme_count": 9,
"sample": []
}
```

### Delete org

```json
{
  "action": "delete",
  "project_id": "proj-123"
}
```

### Response (200 on success)

```json
{
  "ok": true,
  "action": "delete",
  "project_id": "proj-123"
}
```
