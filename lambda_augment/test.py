# lambda_augment/test_local.py
import json
import os

# ── set env vars before importing the handler ──────────────────────────────
os.environ["NEPTUNE_ENDPOINT"] = "localhost"       # won't be hit in dummy mode
os.environ.setdefault("GEMINI_API_KEY", "")        # blank → DummyEmbeddingService

from lambda_function import handler


class FakeContext:
    aws_request_id = "local-test-001"


def make_event(payload: dict) -> dict:
    """Wrap a payload dict into a Lambda Function URL POST event."""
    body = json.dumps(payload)
    return {
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"content-type": "application/json"},
        "body": body,
        "isBase64Encoded": False,
    }


# ── test cases ─────────────────────────────────────────────────────────────

def test_ingest_org():
    event = make_event({
        "project_id": "proj_langchain_ai",
        "project_name": "LangChain ingestion test",
        "owner": "kirill",
        "github_org_url": "https://github.com/langchain-ai",
        "google_docs_url": "https://docs.google.com/document/d/1AiFHoM8f3Un5eiulCs5feeKRfCi8ubt0DUEE36hTplI/edit?usp=sharing",
        "max_repos": 2,
        "action": "delete"
    })
    result = handler(event, FakeContext())
    print("ingest_org:", json.dumps(json.loads(result["body"]), indent=2))


def test_delete():
    event = make_event({
        "action": "delete",
        "project_id": "test-project",
    })
    result = handler(event, FakeContext())
    print("delete:", json.dumps(json.loads(result["body"]), indent=2))


def test_bad_action():
    event = make_event({"action": "nonexistent"})
    result = handler(event, FakeContext())
    assert result["statusCode"] == 500
    print("bad_action:", json.dumps(json.loads(result["body"]), indent=2))


def test_get_rejected():
    event = {
        "requestContext": {"http": {"method": "GET"}},
        "headers": {},
        "body": "",
        "isBase64Encoded": False,
    }
    result = handler(event, FakeContext())
    assert result["statusCode"] == 405
    print("GET rejected:", json.dumps(json.loads(result["body"]), indent=2))


if __name__ == "__main__":
    test_ingest_org()
    test_delete()
    test_bad_action()
    test_get_rejected()
