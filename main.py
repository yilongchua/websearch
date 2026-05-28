from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder

from schema.models import SearchRequest, SearchResponse
from utils.config import get_config_value
from utils.pipeline import initialize_shared_clients, run_query, shutdown_shared_clients
from utils.terminal_dashboard import run_dashboard_logs

_SEARCH_SEMAPHORE: asyncio.Semaphore | None = None
_SEARCH_SEMAPHORE_LIMIT: int | None = None


def _search_semaphore() -> asyncio.Semaphore:
    global _SEARCH_SEMAPHORE, _SEARCH_SEMAPHORE_LIMIT

    limit = max(1, int(get_config_value("server.max_concurrent_requests", 8)))
    if _SEARCH_SEMAPHORE is None or _SEARCH_SEMAPHORE_LIMIT != limit:
        _SEARCH_SEMAPHORE = asyncio.Semaphore(limit)
        _SEARCH_SEMAPHORE_LIMIT = limit
    return _SEARCH_SEMAPHORE


async def _acquire_search_slot() -> asyncio.Semaphore:
    semaphore = _search_semaphore()
    queue_timeout = max(0.0, float(get_config_value("server.queue_timeout_seconds", 2.0)))
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=queue_timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "search_capacity_exhausted",
                "message": "search service is at capacity; retry later",
            },
        ) from exc
    return semaphore


async def _run_query_with_timeout(req: SearchRequest) -> dict:
    request_timeout = float(get_config_value("server.request_timeout_seconds", 120.0))
    query_task = run_query(
        query=req.query,
        write_markdown_package=req.write_markdown_package,
        package_name=req.package_name,
    )
    if request_timeout <= 0:
        return await query_task
    try:
        return await asyncio.wait_for(query_task, timeout=request_timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail={
                "error_code": "search_timeout",
                "message": "search request exceeded the configured timeout",
            },
        ) from exc


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await initialize_shared_clients()
    try:
        yield
    finally:
        await shutdown_shared_clients()


app = FastAPI(title="Websearch API", version="0.3.0", lifespan=lifespan)

MCP_TOOL_NAME = "websearch.search"
MCP_SERVER_NAME = "websearch"
MCP_SERVER_VERSION = "0.3.0"


@app.get("/health")
async def health() -> dict[str, str]:
    return {
        "ok": "true",
        "service": "websearch",
        "crawler_mode": str(get_config_value("crawler.mode", "cli")),
    }


@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest) -> dict:
    semaphore = await _acquire_search_slot()
    try:
        return await _run_query_with_timeout(req)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        semaphore.release()


def _mcp_tool_schema() -> dict[str, Any]:
    return {
        "name": MCP_TOOL_NAME,
        "description": "Run web search and extraction, returning JSON results.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "write_markdown_package": {"type": "boolean", "default": True},
                "package_name": {"type": ["string", "null"]},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }


def _mcp_response(rpc_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _mcp_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


@app.post("/mcp")
async def mcp_endpoint(payload: dict[str, Any]) -> dict[str, Any]:
    rpc_id = payload.get("id")
    method = str(payload.get("method") or "").strip()
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}

    if method == "initialize":
        return _mcp_response(
            rpc_id,
            {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION},
                "capabilities": {"tools": {}},
            },
        )

    if method == "tools/list":
        return _mcp_response(rpc_id, {"tools": [_mcp_tool_schema()]})

    if method == "tools/call":
        tool_name = str(params.get("name") or "").strip()
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if tool_name != MCP_TOOL_NAME:
            return _mcp_error(rpc_id, -32602, f"Unknown tool: {tool_name}")
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _mcp_error(rpc_id, -32602, "Missing required argument: query")

        req = SearchRequest(
            query=query,
            write_markdown_package=bool(arguments.get("write_markdown_package", True)),
            package_name=arguments.get("package_name"),
        )
        try:
            result_payload = await search(req)
        except HTTPException as exc:
            return _mcp_error(rpc_id, -32000, f"Search failed: {exc.detail}")
        encoded_payload = jsonable_encoder(result_payload)

        return _mcp_response(
            rpc_id,
            {
                "content": [{"type": "text", "text": json.dumps(encoded_payload, ensure_ascii=False)}],
                "structuredContent": encoded_payload,
                "isError": False,
            },
        )

    if method == "ping":
        return _mcp_response(rpc_id, {"ok": True, "ts": datetime.now(timezone.utc).isoformat()})

    return _mcp_error(rpc_id, -32601, f"Method not found: {method}")


def _api_base_url() -> str:
    host = str(get_config_value("server.api_host", "127.0.0.1"))
    if host == "0.0.0.0":
        host = "127.0.0.1"
    port = int(get_config_value("server.api_port", 9000))
    return f"http://{host}:{port}"


def cli() -> None:
    parser = argparse.ArgumentParser(description="Unified SearXNG + Crawl4AI websearch")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run FastAPI server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    search_cmd = sub.add_parser("search", help="Call local /search API and print JSON")
    search_cmd.add_argument("--query", required=True)
    search_cmd.add_argument("--write-markdown-package", action="store_true")
    search_cmd.add_argument("--package-name", default=None)
    search_cmd.add_argument("--base-url", default=None)

    dashboard_logs_cmd = sub.add_parser("dashboard-logs", help="Emit JSON dashboard snapshots for docker logs")
    dashboard_logs_cmd.add_argument("--output-dir", default=None)
    dashboard_logs_cmd.add_argument("--interval-seconds", type=float, default=None)
    dashboard_logs_cmd.add_argument("--window-seconds", type=int, default=None)
    dashboard_logs_cmd.add_argument("--limit", type=int, default=None)

    args = parser.parse_args()

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run(
            "main:app",
            host=args.host or str(get_config_value("server.api_host", "0.0.0.0")),
            port=args.port or int(get_config_value("server.api_port", 9000)),
            reload=False,
        )
        return

    if args.cmd == "dashboard-logs":
        output_dir = args.output_dir or str(get_config_value("service.output_dir", "/app/output"))
        run_dashboard_logs(
            output_dir=output_dir,
            interval_seconds=args.interval_seconds or float(get_config_value("dashboard.interval_seconds", 10.0)),
            window_seconds=args.window_seconds or int(get_config_value("dashboard.window_seconds", 86400)),
            limit=args.limit or int(get_config_value("dashboard.limit", 10)),
        )
        return

    base_url = args.base_url or _api_base_url()
    endpoint = f"{base_url.rstrip('/')}/search"
    body = {
        "query": args.query,
        "write_markdown_package": bool(args.write_markdown_package),
        "package_name": args.package_name,
    }

    response = httpx.post(endpoint, json=body, timeout=90)
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    cli()
