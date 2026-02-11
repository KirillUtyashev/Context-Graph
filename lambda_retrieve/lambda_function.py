import json


def handler(event, context):
    # Placeholder retrieval tool.
    # TODO: query Neptune + Aurora, assemble a context bundle under a token budget.
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"ok": True, "message": "retrieval endpoint"}),
    }

