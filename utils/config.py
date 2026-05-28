from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "api_host": "0.0.0.0",
        "api_port": 9000,
    },
    "service": {
        "searxng_base_url": "http://127.0.0.1:8080",
        "output_dir": "/app/output",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
    },
    "search": {
        "max_results": 5,
        "searxng_timeout_seconds": 30.0,
        "searxng_max_retries": 2,
        "searxng_retry_backoff_seconds": 1.0,
        "extract_top_k": 2,
        "extract_max_chars": 6000,
        "extract_timeout_seconds": 45.0,
        "extract_max_retries": 1,
        "extract_retry_backoff_seconds": 1.5,
        "extract_max_workers": 2,
        "http_fast_path_threshold": 0.6,
        "crawler_fast_path_threshold": 0.7,
        "language": "en-US",
        "safesearch": 1,
        "categories": [],
        "engines": [],
    },
    "crawler": {
        "mode": "cli",
        "crwl_binary": "crwl",
        "deep_crawl": "none",
        "deep_max_pages": 10,
        "use_library_fallback": True,
        "http_fallback_enabled": True,
    },
    "cleanup": {
        "prompt_path": "/app/prompt/body_cleanup_prompt.j2",
        "max_chars": 20000,
    },
    "retention": {
        "query_failure_seconds": 86400,
        "source_failure_seconds": 604800,
        "success_event_seconds": 604800,
        "event_prune_throttle_seconds": 86400,
    },
    "debug": {
        "include_query_diagnostics": False,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _candidate_paths() -> list[Path]:
    env_path = os.getenv("WEBSEARCH_CONFIG_PATH")
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path))

    root_dir = Path(__file__).resolve().parents[1]
    cwd = Path.cwd()
    for candidate in (
        cwd / "config" / "config.yaml",
        cwd / "config.yaml",
        root_dir / "config" / "config.yaml",
        root_dir / "config.yaml",
    ):
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)

    for path in _candidate_paths():
        if not path.exists():
            continue
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            config = _deep_merge(config, payload)
        break

    return config


def get_config_value(path: str, default: Any) -> Any:
    node: Any = load_config()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
