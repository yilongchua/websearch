from __future__ import annotations

import json
import os
import re
import secrets
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def slugify(value: str) -> str:
    lowered = value.lower().strip()
    lowered = re.sub(r"[^a-z0-9]+", "-", lowered)
    lowered = re.sub(r"-{2,}", "-", lowered)
    return lowered.strip("-") or "query"


def render_markdown(payload: dict[str, Any]) -> str:
    lines: list[str] = [f"# Websearch Results: {payload['query']}", "", f"Generated at: {payload['generated_at']}", ""]

    for idx, item in enumerate(payload.get("results", []), start=1):
        lines.append(f"## {idx}. {item.get('title') or item.get('url')}")
        lines.append("")
        lines.append(f"- URL: {item.get('url', '')}")
        snippet = str(item.get("snippet") or "").strip()
        if snippet:
            lines.append(f"- Snippet: {snippet}")
        quality = item.get("extracted_content_quality")
        if isinstance(quality, dict):
            lines.append(f"- Quality score: {quality.get('quality_score', 0)}")
        lines.append("")

        extracted = str(item.get("extracted_content") or "").strip()
        if extracted:
            lines.append(extracted)
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def write_package(*, payload: dict[str, Any], output_dir: str | Path, package_name: str | None = None) -> dict[str, str]:
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    host_token = slugify(socket.gethostname())[:12] or "host"
    rand_token = secrets.token_hex(3)
    slug = slugify(package_name or str(payload.get("query") or "query"))
    package_dir = root / f"{stamp}_{host_token}_{rand_token}_{slug}"
    package_dir.mkdir(parents=True, exist_ok=True)

    json_path = package_dir / "result.json"
    markdown_path = package_dir / "result.md"
    tmp_suffix = f".tmp-{os.getpid()}-{secrets.token_hex(4)}"

    json_tmp_path = package_dir / f"{json_path.name}{tmp_suffix}"
    markdown_tmp_path = package_dir / f"{markdown_path.name}{tmp_suffix}"

    json_tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(json_tmp_path, json_path)

    markdown_tmp_path.write_text(render_markdown(payload), encoding="utf-8")
    os.replace(markdown_tmp_path, markdown_path)

    return {
        "dir": str(package_dir),
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
    }
