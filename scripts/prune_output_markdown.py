#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.config import get_config_value
from utils.events import prune_events
from utils.output_retention import prune_markdown_files


def main() -> None:
    parser = argparse.ArgumentParser(description="Prune markdown and event logs by retention policy.")
    parser.add_argument("--output-dir", default=None, help="Output root directory (defaults to service.output_dir)")
    parser.add_argument("--older-than-hours", type=int, default=24, help="Delete markdown files older than this many hours")
    parser.add_argument("--prune-markdown", action="store_true", help="Prune result.md files")
    parser.add_argument("--prune-events", action="store_true", help="Prune events/*.ndjson entries")
    parser.add_argument("--query-failure-hours", type=int, default=24, help="Retention for query_failed events")
    parser.add_argument("--source-failure-hours", type=int, default=24 * 7, help="Retention for source_failed events")
    parser.add_argument("--success-event-hours", type=int, default=24 * 7, help="Retention for success events")
    args = parser.parse_args()

    configured_output_dir = str(get_config_value("service.output_dir", "/app/output"))
    output_dir = Path(args.output_dir or configured_output_dir).expanduser().resolve()
    older_than_seconds = max(1, args.older_than_hours) * 3600
    should_prune_markdown = bool(args.prune_markdown or (not args.prune_markdown and not args.prune_events))
    should_prune_events = bool(args.prune_events)

    payload: dict[str, object] = {
        "output_dir": str(output_dir),
    }

    if should_prune_markdown:
        payload["markdown"] = {
            "older_than_hours": max(1, args.older_than_hours),
            **prune_markdown_files(output_dir=output_dir, older_than_seconds=older_than_seconds),
        }

    if should_prune_events:
        payload["events"] = prune_events(
            output_dir=output_dir,
            query_failure_ttl_seconds=max(1, args.query_failure_hours) * 3600,
            source_failure_ttl_seconds=max(1, args.source_failure_hours) * 3600,
            success_ttl_seconds=max(1, args.success_event_hours) * 3600,
        )

    print(json.dumps(payload))


if __name__ == "__main__":
    main()
