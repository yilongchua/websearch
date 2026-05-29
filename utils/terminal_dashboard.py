from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from .events import list_query_events, list_source_events, summarize_events


def _fmt_duration_seconds(ms: object) -> str:
    try:
        value = float(ms) / 1000.0
    except Exception:
        return "-"
    return f"{value:.1f}s"


def _truncate(text: object, max_len: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_len:
        return value
    return value[: max(0, max_len - 3)] + "..."


def _source_label(url: str) -> str:
    host = urlparse(url).netloc or url
    return host.replace("www.", "")


def _to_epoch(ts: object) -> float:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _render_table(
    *,
    generated_at: str,
    window_seconds: int,
    summary: dict[str, object],
    latest_queries: list[dict[str, object]],
) -> str:
    lines: list[str] = []
    window_hours = max(1, int(window_seconds // 3600))
    lines.append("")
    lines.append("+" + "-" * 86 + "+")
    lines.append(f"| Dashboard Snapshot | at={generated_at} | window={window_seconds}s ({window_hours}h) |")
    lines.append(
        "| Queries: total={total} ok={ok} fail={fail} avg={avg} | Sources: total={st} ok={sok} fail={sfail} |".format(
            total=summary.get("total_queries", 0),
            ok=summary.get("succeeded_queries", 0),
            fail=summary.get("failed_queries", 0),
            avg=_fmt_duration_seconds(summary.get("avg_query_ms", 0.0)),
            st=summary.get("total_sources", 0),
            sok=summary.get("succeeded_sources", 0),
            sfail=summary.get("failed_sources", 0),
        )
    )
    lines.append("| Note: Captures only the last 3 hours of searches.                                  |")
    lines.append("+" + "-" * 86 + "+")
    lines.append("")
    lines.append("-" * 120)
    lines.append("TS(UTC)              STATUS  TOTAL   QUERY                                FAILED SOURCES")
    lines.append("-" * 120)
    for item in latest_queries:
        failed = item.get("failed_sources", [])
        failed_text = ", ".join(str(x) for x in failed) if isinstance(failed, list) and failed else "-"
        lines.append(
            "{ts:<20} {status:<7} {total:<6} {query:<36} {failed}".format(
                ts=_truncate(item.get("timestamp"), 19),
                status=_truncate(item.get("status"), 7),
                total=_fmt_duration_seconds(item.get("total_ms")),
                query=_truncate(item.get("query"), 36),
                failed=_truncate(failed_text, 48),
            )
        )
    lines.append("")
    return "\n".join(lines)


def run_dashboard_logs(
    *,
    output_dir: str,
    interval_seconds: float = 10.0,
    window_seconds: int = 86400,
    limit: int = 10,
    output_format: str = "table",
) -> None:
    interval_seconds = max(1.0, float(interval_seconds))
    limit = max(1, min(int(limit), 100))
    window_seconds = max(60, int(window_seconds))
    output_format = str(output_format or "table").strip().lower()
    if output_format not in {"table", "json"}:
        output_format = "table"

    while True:
        summary = summarize_events(output_dir=output_dir, window_seconds=window_seconds)
        queries_payload = list_query_events(output_dir=output_dir, limit=limit)
        queries = queries_payload.get("items", [])
        since_ts = time.time() - window_seconds
        latest_queries: list[dict[str, object]] = []
        for item in queries:
            if _to_epoch(item.get("timestamp")) < since_ts:
                continue
            query_id = str(item.get("query_id") or "")
            source_events = list_source_events(output_dir=output_dir, query_id=query_id) if query_id else []
            failed_sources: list[str] = []
            for src in source_events:
                if str(src.get("event_type") or "") != "source_failed":
                    continue
                label = _source_label(str(src.get("url") or ""))
                if label and label not in failed_sources:
                    failed_sources.append(label)
            latest_queries.append(
                {
                    "timestamp": item.get("timestamp"),
                    "query_id": query_id,
                    "status": item.get("status"),
                    "query": item.get("query"),
                    "total_ms": item.get("total_ms"),
                    "total_s": round(float(item.get("total_ms") or 0.0) / 1000.0, 2),
                    "markdown_available": item.get("markdown_available"),
                    "failed_sources": failed_sources,
                }
            )

        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        snapshot = {
            "type": "dashboard_snapshot",
            "generated_at": generated_at,
            "window_seconds": window_seconds,
            "summary": summary,
            "latest_queries": latest_queries,
        }
        if output_format == "json":
            print(json.dumps(snapshot, ensure_ascii=False), flush=True)
        else:
            print(_render_table(generated_at=generated_at, window_seconds=window_seconds, summary=summary, latest_queries=latest_queries), flush=True)
        time.sleep(interval_seconds)
