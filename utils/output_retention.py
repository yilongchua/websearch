from __future__ import annotations

import json
import time
from pathlib import Path

from .events import prune_events

THROTTLE_MARKER_FILENAME = ".last_md_prune_at"
EVENT_THROTTLE_MARKER_FILENAME = ".last_event_prune_at"


def prune_markdown_files(*, output_dir: str | Path, older_than_seconds: int = 86400) -> dict[str, int]:
    root = Path(output_dir).expanduser().resolve()
    if not root.exists():
        return {"deleted_markdown_files": 0, "removed_empty_dirs": 0}

    now = time.time()
    deleted_markdown_files = 0
    removed_empty_dirs = 0

    for markdown_path in root.rglob("result.md"):
        if not markdown_path.is_file():
            continue
        try:
            age_seconds = now - markdown_path.stat().st_mtime
            if age_seconds <= older_than_seconds:
                continue
            markdown_path.unlink(missing_ok=True)
            deleted_markdown_files += 1
        except Exception:
            continue

    # Clean up empty package directories after markdown pruning.
    for directory in sorted((path for path in root.rglob("*") if path.is_dir()), key=lambda item: len(item.parts), reverse=True):
        try:
            if any(directory.iterdir()):
                continue
            directory.rmdir()
            removed_empty_dirs += 1
        except Exception:
            continue

    return {
        "deleted_markdown_files": deleted_markdown_files,
        "removed_empty_dirs": removed_empty_dirs,
    }


def maybe_prune_markdown_daily(*, output_dir: str | Path, older_than_seconds: int = 86400) -> dict[str, int] | None:
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    marker_path = root / THROTTLE_MARKER_FILENAME
    now = time.time()

    if marker_path.exists():
        try:
            marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
            last_ran_at = float(marker_payload.get("last_ran_at", 0.0))
        except Exception:
            last_ran_at = 0.0
        if (now - last_ran_at) < older_than_seconds:
            return None

    result = prune_markdown_files(output_dir=root, older_than_seconds=older_than_seconds)
    marker_payload = {
        "last_ran_at": now,
        "deleted_markdown_files": result["deleted_markdown_files"],
        "removed_empty_dirs": result["removed_empty_dirs"],
    }
    marker_path.write_text(json.dumps(marker_payload), encoding="utf-8")
    return result


def maybe_prune_event_logs_daily(
    *,
    output_dir: str | Path,
    query_failure_ttl_seconds: int = 86400,
    source_failure_ttl_seconds: int = 7 * 86400,
    success_ttl_seconds: int = 7 * 86400,
    throttle_seconds: int = 86400,
) -> dict[str, int] | None:
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    marker_path = root / EVENT_THROTTLE_MARKER_FILENAME
    now = time.time()

    if marker_path.exists():
        try:
            marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
            last_ran_at = float(marker_payload.get("last_ran_at", 0.0))
        except Exception:
            last_ran_at = 0.0
        if (now - last_ran_at) < throttle_seconds:
            return None

    result = prune_events(
        output_dir=root,
        query_failure_ttl_seconds=query_failure_ttl_seconds,
        source_failure_ttl_seconds=source_failure_ttl_seconds,
        success_ttl_seconds=success_ttl_seconds,
    )
    marker_payload = {
        "last_ran_at": now,
        **result,
    }
    marker_path.write_text(json.dumps(marker_payload), encoding="utf-8")
    return result
