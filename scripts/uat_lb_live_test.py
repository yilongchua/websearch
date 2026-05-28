#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx


API_URL = "http://localhost:9000/search"
TIMEOUT_SECONDS = 120

# 3 separate queries intended to run concurrently to trigger nginx load balancing.
QUERIES = [
    "latest enterprise browser security policy updates",
    "state of AI safety evaluation benchmarks",
    "recent supply chain resilience strategies in manufacturing",
]


@dataclass
class UATResult:
    query: str
    status_code: int
    upstream_addr: str
    upstream_status: str
    total_results: int
    ok: bool
    error: str | None = None


async def _run_single_query(client: httpx.AsyncClient, query: str) -> UATResult:
    try:
        response = await client.post(
            API_URL,
            json={
                "query": query,
                "write_markdown_package": False,
            },
        )
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        return UATResult(
            query=query,
            status_code=response.status_code,
            upstream_addr=response.headers.get("x-upstream-addr", "unknown"),
            upstream_status=response.headers.get("x-upstream-status", "unknown"),
            total_results=int(body.get("total_results", 0)) if isinstance(body, dict) else 0,
            ok=(response.status_code == 200),
            error=None if response.status_code == 200 else response.text[:300],
        )
    except Exception as exc:
        return UATResult(
            query=query,
            status_code=0,
            upstream_addr="error",
            upstream_status="error",
            total_results=0,
            ok=False,
            error=str(exc),
        )


async def main() -> int:
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        tasks = [_run_single_query(client, query) for query in QUERIES]
        results = await asyncio.gather(*tasks)

    unique_upstreams = sorted({item.upstream_addr for item in results if item.upstream_addr not in {"unknown", "error"}})
    all_ok = all(item.ok for item in results)

    summary = {
        "api_url": API_URL,
        "all_ok": all_ok,
        "unique_upstreams_count": len(unique_upstreams),
        "unique_upstreams": unique_upstreams,
        "results": [item.__dict__ for item in results],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    # Pass criteria:
    # - All requests return 200
    # - At least 2 backends observed (3 expected in most runs under concurrent load)
    if all_ok and len(unique_upstreams) >= 2:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
