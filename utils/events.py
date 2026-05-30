from __future__ import annotations

import json
import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


# Common multi-part public suffixes so that e.g. "bbc.co.uk" collapses to
# "bbc.co.uk" rather than the meaningless "co.uk". Not exhaustive, but covers
# the suffixes most likely to show up in search results.
_MULTI_PART_SUFFIXES = frozenset(
    {
        "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "ltd.uk", "net.uk",
        "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
        "co.nz", "net.nz", "org.nz", "govt.nz",
        "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
        "co.kr", "or.kr", "co.in", "net.in", "org.in", "gen.in", "firm.in",
        "com.br", "net.br", "org.br", "gov.br",
        "com.cn", "net.cn", "org.cn", "gov.cn",
        "com.sg", "edu.sg", "gov.sg",
        "com.hk", "org.hk", "gov.hk",
        "com.tw", "org.tw",
        "co.za", "org.za", "gov.za",
        "com.mx", "com.tr", "com.ar", "com.ru", "com.ua", "com.my",
        "co.id", "co.th", "com.ph", "com.vn", "com.pk",
    }
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def registrable_domain(value: str) -> str:
    """Collapse a URL or host to its registrable root domain.

    "https://m.facebook.com/foo" and "www.facebook.com" both become
    "facebook.com" so a single failed page blocks the whole site.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    # urlsplit needs a scheme (or leading //) to populate netloc.
    if "://" not in raw:
        raw = "//" + raw
    host = urlsplit(raw).netloc
    host = host.split("@")[-1].split(":")[0].strip(".")
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return last_two


def _to_epoch(ts: str) -> float:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _event_day(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y%m%d")


def events_root(output_dir: str | Path) -> Path:
    root = Path(output_dir).expanduser().resolve() / "events"
    root.mkdir(parents=True, exist_ok=True)
    return root


def event_file_path(output_dir: str | Path, ts: str) -> Path:
    return events_root(output_dir) / f"events-{_event_day(ts)}.ndjson"


def append_event(*, output_dir: str | Path, event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event)
    payload.setdefault("event_id", uuid.uuid4().hex)
    payload.setdefault("timestamp", utc_now_iso())
    payload.setdefault("instance_id", slugify_instance(socket.gethostname()))

    path = event_file_path(output_dir, str(payload["timestamp"]))
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return payload


def slugify_instance(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in (value or "instance"))
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "instance"


def iter_events(*, output_dir: str | Path, since_ts: float | None = None) -> list[dict[str, Any]]:
    root = events_root(output_dir)
    events: list[dict[str, Any]] = []
    for file_path in sorted(root.glob("events-*.ndjson")):
        try:
            for raw in file_path.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                item = json.loads(raw)
                ts = _to_epoch(str(item.get("timestamp") or ""))
                if since_ts is not None and ts <= since_ts:
                    continue
                events.append(item)
        except Exception:
            continue
    events.sort(key=lambda item: str(item.get("timestamp") or ""))
    return events


def failed_domains(
    *,
    output_dir: str | Path,
    lookback_seconds: int = 604800,
    min_failures: int = 2,
) -> set[str]:
    """Root domains to skip, derived from recent source extraction events.

    A URL only counts as failed if it produced at least one ``source_failed``
    event and *no* ``source_succeeded`` event — extraction emits one event per
    mode (http/cli/library), so a URL that fell back to a working mode must not
    be treated as a failure. A root domain is blocked once it accumulates
    ``min_failures`` distinct failed URLs within the lookback window.
    """
    since_ts = time.time() - max(0, int(lookback_seconds)) if lookback_seconds else None
    succeeded_urls: set[str] = set()
    failed_urls: set[str] = set()

    for event in iter_events(output_dir=output_dir, since_ts=since_ts):
        event_type = str(event.get("event_type") or "")
        if not event_type.startswith("source_"):
            continue
        url = str(event.get("url") or "").strip()
        if not url:
            continue
        if event_type == "source_succeeded":
            succeeded_urls.add(url)
        elif event_type == "source_failed":
            failed_urls.add(url)

    domain_failures: dict[str, int] = {}
    for url in failed_urls:
        if url in succeeded_urls:
            continue
        domain = registrable_domain(url)
        if not domain:
            continue
        domain_failures[domain] = domain_failures.get(domain, 0) + 1

    threshold = max(1, int(min_failures))
    return {domain for domain, count in domain_failures.items() if count >= threshold}


def prune_events(
    *,
    output_dir: str | Path,
    query_failure_ttl_seconds: int,
    source_failure_ttl_seconds: int,
    success_ttl_seconds: int,
) -> dict[str, int]:
    root = events_root(output_dir)
    now = time.time()
    files_touched = 0
    files_deleted = 0
    events_deleted = 0

    for file_path in sorted(root.glob("events-*.ndjson")):
        kept_lines: list[str] = []
        changed = False
        try:
            lines = file_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue

        for raw in lines:
            if not raw.strip():
                continue
            try:
                item = json.loads(raw)
            except Exception:
                changed = True
                events_deleted += 1
                continue

            ts = _to_epoch(str(item.get("timestamp") or ""))
            age = now - ts if ts > 0 else success_ttl_seconds + 1
            event_type = str(item.get("event_type") or "")
            status = str(item.get("status") or "")

            ttl = success_ttl_seconds
            if event_type == "query_failed" or (event_type.startswith("query_") and status == "failed"):
                ttl = query_failure_ttl_seconds
            elif event_type == "source_failed" or (event_type.startswith("source_") and status == "failed"):
                ttl = source_failure_ttl_seconds

            if age > ttl:
                changed = True
                events_deleted += 1
                continue

            kept_lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))

        if not changed:
            continue

        files_touched += 1
        if kept_lines:
            file_path.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
        else:
            try:
                file_path.unlink(missing_ok=True)
                files_deleted += 1
            except Exception:
                pass

    return {
        "event_files_touched": files_touched,
        "event_files_deleted": files_deleted,
        "events_deleted": events_deleted,
    }


@dataclass
class QueryStats:
    total_queries: int = 0
    succeeded_queries: int = 0
    failed_queries: int = 0
    total_sources: int = 0
    succeeded_sources: int = 0
    failed_sources: int = 0
    avg_query_ms: float = 0.0


def summarize_events(*, output_dir: str | Path, window_seconds: int) -> dict[str, Any]:
    since = time.time() - max(1, window_seconds)
    events = iter_events(output_dir=output_dir, since_ts=since)

    stats = QueryStats()
    total_ms = 0.0
    for item in events:
        et = str(item.get("event_type") or "")
        if et == "query_succeeded":
            stats.total_queries += 1
            stats.succeeded_queries += 1
            ms = float(item.get("total_ms") or 0.0)
            total_ms += ms
        elif et == "query_failed":
            stats.total_queries += 1
            stats.failed_queries += 1
            ms = float(item.get("total_ms") or 0.0)
            total_ms += ms
        elif et == "source_succeeded":
            stats.total_sources += 1
            stats.succeeded_sources += 1
        elif et == "source_failed":
            stats.total_sources += 1
            stats.failed_sources += 1

    if stats.total_queries > 0:
        stats.avg_query_ms = round(total_ms / stats.total_queries, 2)

    return {
        "window_seconds": max(1, window_seconds),
        "total_queries": stats.total_queries,
        "succeeded_queries": stats.succeeded_queries,
        "failed_queries": stats.failed_queries,
        "total_sources": stats.total_sources,
        "succeeded_sources": stats.succeeded_sources,
        "failed_sources": stats.failed_sources,
        "avg_query_ms": stats.avg_query_ms,
    }


def list_query_events(*, output_dir: str | Path, limit: int = 50, before_ts: str | None = None) -> dict[str, Any]:
    items = [
        event
        for event in iter_events(output_dir=output_dir)
        if str(event.get("event_type") or "") in {"query_succeeded", "query_failed"}
    ]
    if before_ts:
        items = [event for event in items if str(event.get("timestamp") or "") < before_ts]

    items.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    page = items[: max(1, min(limit, 200))]
    next_cursor = page[-1].get("timestamp") if len(items) > len(page) and page else None

    for row in page:
        package = row.get("package")
        if isinstance(package, dict):
            md_path = Path(str(package.get("markdown_path") or ""))
            row["markdown_available"] = md_path.exists()
        else:
            row["markdown_available"] = False

    return {"items": page, "next_cursor": next_cursor}


def list_source_events(*, output_dir: str | Path, query_id: str) -> list[dict[str, Any]]:
    items = [
        event
        for event in iter_events(output_dir=output_dir)
        if str(event.get("query_id") or "") == query_id and str(event.get("event_type") or "").startswith("source_")
    ]
    items.sort(key=lambda item: str(item.get("timestamp") or ""))
    return items
