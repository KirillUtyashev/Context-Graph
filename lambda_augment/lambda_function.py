import os, json, base64, hmac, hashlib

def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method")
    if method != "POST":
        return {"statusCode": 405, "body": "Method not allowed"}

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    # (A) GitHub webhook verification (optional but recommended for GitHub)
    sig = headers.get("x-hub-signature-256")
    if sig:
        secret = os.environ["GITHUB_WEBHOOK_SECRET"].encode()
        digest = hmac.new(secret, raw_body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, f"sha256={digest}"):
            return {"statusCode": 401, "body": "Invalid signature"}

    # (B) Parse + validate payload
    try:
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": "Invalid JSON"}

    # Example required fields for your own ingestion format
    # if "repo_url" not in payload or "docs_url" not in payload: ...

    return {"statusCode": 200, "body": json.dumps({"ok": True})}
