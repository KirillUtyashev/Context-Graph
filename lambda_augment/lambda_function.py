import json


def handler(event, context):
    # Placeholder augmentation receiver.
    # May come from GitHub webhook.
    # TODO: verify signature, parse event type, enqueue/work.
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(
            {
                "ok": True,
                "message": "Augmentation received",
                "method": event.get("requestContext", {}).get("http", {}).get("method"),
            }
        ),
    }

