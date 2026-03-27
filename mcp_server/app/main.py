import io
import asyncio
import json
import os
from typing import Any, Dict

import boto3
import httpx
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse


def _parse_lambda_url_response(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Lambda Function URL responses commonly wrap the real payload in a string `body`
    with an outer `{statusCode, headers, body}` envelope.
    """
    body = resp_json.get("body")
    if isinstance(body, str):
        try:
            parsed_body: Any = json.loads(body)
        except json.JSONDecodeError:
            parsed_body = body

        # Prefer returning the decoded payload directly (what the agent/tool wants).
        if isinstance(parsed_body, dict):
            return parsed_body

        # Otherwise, keep a small envelope for debuggability.
        return {"statusCode": resp_json.get("statusCode"), "body": parsed_body}

    # Fall back to returning the raw response object
    return resp_json


async def _call_retrieve_lambda(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calls the `lambda_retrieve` Lambda using one of:
    - RETRIEVE_FUNCTION_URL: HTTPS POST (no auth in this project's Terraform)
    - RETRIEVE_LAMBDA_NAME: AWS SDK invoke (requires EC2 role permission)
    """
    retrieve_url = os.getenv("RETRIEVE_FUNCTION_URL", "").strip()
    if retrieve_url:
        timeout_s = float(os.getenv("RETRIEVE_TIMEOUT_SECONDS", "25"))
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            r = await client.post(
                retrieve_url,
                json=payload,
                headers={"content-type": "application/json"},
            )
            r.raise_for_status()
            return _parse_lambda_url_response(r.json())

    retrieve_name = os.getenv("RETRIEVE_LAMBDA_NAME", "").strip()
    if retrieve_name:
        # Match Lambda Function URL event shape (what the current lambda expects long-term)
        payload_obj = {
            "headers": {"content-type": "application/json"},
            "body": json.dumps(payload),
        }
        payload_bytes = json.dumps(payload_obj).encode("utf-8")
        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")

        def _invoke_sync() -> Dict[str, Any]:
            client = boto3.client("lambda", region_name=region)
            resp = client.invoke(
                FunctionName=retrieve_name,
                InvocationType="RequestResponse",
                Payload=io.BytesIO(payload_bytes),
            )
            raw = resp["Payload"].read()
            decoded = json.loads(raw.decode("utf-8"))
            if isinstance(decoded, dict):
                return _parse_lambda_url_response(decoded)
            return {"body": decoded}

        return await asyncio.to_thread(_invoke_sync)

    raise RuntimeError(
        "Retrieve lambda not configured. Set RETRIEVE_FUNCTION_URL or RETRIEVE_LAMBDA_NAME."
    )


def create_app():
    mcp = FastMCP(name="Context Graph MCP Server")

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    @mcp.custom_route("/", methods=["GET"])
    async def root(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "ok": True,
                "message": "Context Graph MCP server is running",
                "mcp_endpoint": "/mcp",
            }
        )

    @mcp.tool
    async def retrieve_context(prompt: str) -> Dict[str, Any]:
        """
        Retrieve additional context from the Context Graph.

        This tool forwards the current user prompt to the `lambda_retrieve` 
        Lambda, and returns whatever context bundle it responds with.
        """
        return await _call_retrieve_lambda({"prompt": prompt})

    @mcp.tool
    async def search_code(
        query: str,
        project: str | None = None,
        file_pattern: str | None = None,
        limit: int | None = None,
        regex: bool = False,
    ) -> Dict[str, Any]:
        """
        Grep-like text search within indexed project files.

        This interface is intended to replace `search_graph` for query-time code lookup.
        """
        return await _call_retrieve_lambda(
            {
                "tool": "search_code",
                "query": query,
                "project": project,
                "file_pattern": file_pattern,
                "limit": limit,
                "regex": regex,
            }
        )

    @mcp.tool
    async def trace_call_path(
        function_name: str,
        project: str | None = None,
        direction: str | None = None,
        depth: int | None = None,
        edge_types: list[str] | None = None,
    ) -> Dict[str, Any]:
        """
        Traverse call relationships to show who calls a function and what it calls.
        """
        return await _call_retrieve_lambda(
            {
                "tool": "trace_call_path",
                "function_name": function_name,
                "project": project,
                "direction": direction,
                "depth": depth,
                "edge_types": edge_types,
            }
        )

    @mcp.tool
    async def get_code_snippet(
        qualified_name: str,
        project: str | None = None,
        include_neighbors: bool | None = None,
        auto_resolve: bool | None = None,
    ) -> Dict[str, Any]:
        """
        Read source code for a function, class, or symbol by qualified name.
        """
        return await _call_retrieve_lambda(
            {
                "tool": "get_code_snippet",
                "qualified_name": qualified_name,
                "project": project,
                "include_neighbors": include_neighbors,
                "auto_resolve": auto_resolve,
            }
        )

    return mcp.http_app()


app = create_app()
