from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException

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
    dashboard_logs_cmd.add_argument("--interval-seconds", type=float, default=10.0)
    dashboard_logs_cmd.add_argument("--window-seconds", type=int, default=86400)
    dashboard_logs_cmd.add_argument("--limit", type=int, default=10)

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
            interval_seconds=args.interval_seconds,
            window_seconds=args.window_seconds,
            limit=args.limit,
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
