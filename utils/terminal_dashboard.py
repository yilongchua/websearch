from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from .events import list_query_events, summarize_events


def run_dashboard_logs(*, output_dir: str, interval_seconds: float = 10.0, window_seconds: int = 86400, limit: int = 10) -> None:
    interval_seconds = max(1.0, float(interval_seconds))
    limit = max(1, min(int(limit), 100))
    window_seconds = max(60, int(window_seconds))

    while True:
        summary = summarize_events(output_dir=output_dir, window_seconds=window_seconds)
        queries_payload = list_query_events(output_dir=output_dir, limit=limit)
        queries = queries_payload.get("items", [])
        latest_queries: list[dict[str, object]] = []
        for item in queries:
            latest_queries.append(
                {
                    "timestamp": item.get("timestamp"),
                    "query_id": item.get("query_id"),
                    "status": item.get("status"),
                    "query": item.get("query"),
                    "total_ms": item.get("total_ms"),
                    "markdown_available": item.get("markdown_available"),
                }
            )

        snapshot = {
            "type": "dashboard_snapshot",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_seconds": window_seconds,
            "summary": summary,
            "latest_queries": latest_queries,
        }
        print(json.dumps(snapshot, ensure_ascii=False), flush=True)
        time.sleep(interval_seconds)
