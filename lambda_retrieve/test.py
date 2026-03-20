# lambda_retrieve/test_local.py
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("NEPTUNE_ENDPOINT", "localhost")
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("GITHUB_TOKEN", "")

from lambda_function import handler


class FakeContext:
    aws_request_id = "local-test-retrieve-001"


def make_event(payload: dict) -> dict:
    return {
        "requestContext": {"http": {"method": "POST"}},
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }


def test_retrieve_task():
    event = make_event({
        "action": "retrieve_task",
        "project_id": "test-project",
        "task": "authentication",
        "k": 3,
    })
    result = handler(event, FakeContext())
    body = json.loads(result["body"])
    print("status:", result["statusCode"])
    print(json.dumps(body, indent=2))


if __name__ == "__main__":
    test_retrieve_task()
