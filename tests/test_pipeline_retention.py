from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils import pipeline
from utils.events import append_event, list_query_events, list_source_events, prune_events, summarize_events
from utils.output_retention import THROTTLE_MARKER_FILENAME, maybe_prune_markdown_daily, prune_markdown_files
from utils.packaging import write_package


def test_extract_best_content_http_fast_path(monkeypatch, tmp_path: Path):
    calls = {"cli": 0, "lib": 0}

    async def fake_http(_url: str):
        return "http candidate", None

    async def fake_cli(_url: str) -> str:
        calls["cli"] += 1
        return "cli candidate"

    async def fake_lib(_url: str) -> str:
        calls["lib"] += 1
        return "lib candidate"

    def fake_quality(text: str) -> dict:
        if "http" in text:
            return {"quality_score": 0.95, "cleaned_text": "http cleaned"}
        return {"quality_score": 0.2, "cleaned_text": text}

    def fake_config(path: str, default):
        mapping = {
            "crawler.mode": "cli",
            "crawler.use_library_fallback": True,
            "search.http_fast_path_threshold": 0.6,
            "search.crawler_fast_path_threshold": 0.7,
        }
        return mapping.get(path, default)

    monkeypatch.setattr(pipeline, "_http_fallback_extract", fake_http)
    monkeypatch.setattr(pipeline, "_crawl_with_crwl_cli", fake_cli)
    monkeypatch.setattr(pipeline, "_crawl_with_library", fake_lib)
    monkeypatch.setattr(pipeline, "assess_content_quality", fake_quality)
    monkeypatch.setattr(pipeline, "get_config_value", fake_config)

    extracted, quality = asyncio.run(
        pipeline._extract_best_content(
            "https://example.com", query_id="q1", output_dir=str(tmp_path), source_statuses=[]
        )
    )
    assert extracted == "http cleaned"
    assert float(quality["quality_score"]) == 0.95
    assert calls["cli"] == 0
    assert calls["lib"] == 0


def test_extract_best_content_cli_fast_path(monkeypatch, tmp_path: Path):
    calls = {"lib": 0}

    async def fake_http(_url: str):
        return "http candidate", None

    async def fake_cli(_url: str) -> str:
        return "cli candidate"

    async def fake_lib(_url: str) -> str:
        calls["lib"] += 1
        return "lib candidate"

    def fake_quality(text: str) -> dict:
        if "http" in text:
            return {"quality_score": 0.2, "cleaned_text": "http cleaned"}
        if "cli" in text:
            return {"quality_score": 0.85, "cleaned_text": "cli cleaned"}
        return {"quality_score": 0.4, "cleaned_text": text}

    def fake_config(path: str, default):
        mapping = {
            "crawler.mode": "cli",
            "crawler.use_library_fallback": True,
            "search.http_fast_path_threshold": 0.6,
            "search.crawler_fast_path_threshold": 0.7,
        }
        return mapping.get(path, default)

    monkeypatch.setattr(pipeline, "_http_fallback_extract", fake_http)
    monkeypatch.setattr(pipeline, "_crawl_with_crwl_cli", fake_cli)
    monkeypatch.setattr(pipeline, "_crawl_with_library", fake_lib)
    monkeypatch.setattr(pipeline, "assess_content_quality", fake_quality)
    monkeypatch.setattr(pipeline, "get_config_value", fake_config)

    extracted, quality = asyncio.run(
        pipeline._extract_best_content(
            "https://example.com", query_id="q1", output_dir=str(tmp_path), source_statuses=[]
        )
    )
    assert extracted == "cli cleaned"
    assert float(quality["quality_score"]) == 0.85
    assert calls["lib"] == 0


def test_extract_best_content_falls_back_to_best_candidate(monkeypatch, tmp_path: Path):
    async def fake_http(_url: str):
        return "http candidate", None

    async def fake_cli(_url: str) -> str:
        return "cli candidate"

    async def fake_lib(_url: str) -> str:
        return "lib candidate"

    def fake_quality(text: str) -> dict:
        if "http" in text:
            return {"quality_score": 0.45, "cleaned_text": "http cleaned"}
        if "cli" in text:
            return {"quality_score": 0.5, "cleaned_text": "cli cleaned"}
        return {"quality_score": 0.65, "cleaned_text": "lib cleaned"}

    def fake_config(path: str, default):
        mapping = {
            "crawler.mode": "library",
            "crawler.use_library_fallback": True,
            "search.http_fast_path_threshold": 0.9,
            "search.crawler_fast_path_threshold": 0.9,
        }
        return mapping.get(path, default)

    monkeypatch.setattr(pipeline, "_http_fallback_extract", fake_http)
    monkeypatch.setattr(pipeline, "_crawl_with_crwl_cli", fake_cli)
    monkeypatch.setattr(pipeline, "_crawl_with_library", fake_lib)
    monkeypatch.setattr(pipeline, "assess_content_quality", fake_quality)
    monkeypatch.setattr(pipeline, "get_config_value", fake_config)

    extracted, quality = asyncio.run(
        pipeline._extract_best_content(
            "https://example.com", query_id="q1", output_dir=str(tmp_path), source_statuses=[]
        )
    )
    assert extracted == "lib cleaned"
    assert float(quality["quality_score"]) == 0.65


def test_extract_best_content_mcp_fast_path(monkeypatch, tmp_path: Path):
    calls = {"lib": 0}

    async def fake_http(_url: str):
        return "http candidate", None

    async def fake_mcp(_url: str) -> str:
        return "mcp candidate"

    async def fake_lib(_url: str) -> str:
        calls["lib"] += 1
        return "lib candidate"

    def fake_quality(text: str) -> dict:
        if "http" in text:
            return {"quality_score": 0.2, "cleaned_text": "http cleaned"}
        if "mcp" in text:
            return {"quality_score": 0.88, "cleaned_text": "mcp cleaned"}
        return {"quality_score": 0.3, "cleaned_text": text}

    def fake_config(path: str, default):
        mapping = {
            "crawler.mode": "mcp",
            "crawler.use_library_fallback": True,
            "search.http_fast_path_threshold": 0.6,
            "search.crawler_fast_path_threshold": 0.7,
        }
        return mapping.get(path, default)

    monkeypatch.setattr(pipeline, "_http_fallback_extract", fake_http)
    monkeypatch.setattr(pipeline, "_crawl_with_mcp", fake_mcp)
    monkeypatch.setattr(pipeline, "_crawl_with_library", fake_lib)
    monkeypatch.setattr(pipeline, "assess_content_quality", fake_quality)
    monkeypatch.setattr(pipeline, "get_config_value", fake_config)

    extracted, quality = asyncio.run(
        pipeline._extract_best_content(
            "https://example.com", query_id="q1", output_dir=str(tmp_path), source_statuses=[]
        )
    )
    assert extracted == "mcp cleaned"
    assert float(quality["quality_score"]) == 0.88
    assert calls["lib"] == 0


def test_extract_best_content_mcp_fallback_to_library(monkeypatch, tmp_path: Path):
    async def fake_http(_url: str):
        return "http candidate", None

    async def fake_mcp(_url: str) -> str:
        raise RuntimeError("mcp down")

    async def fake_lib(_url: str) -> str:
        return "lib candidate"

    def fake_quality(text: str) -> dict:
        if "http" in text:
            return {"quality_score": 0.2, "cleaned_text": "http cleaned"}
        return {"quality_score": 0.8, "cleaned_text": "lib cleaned"}

    def fake_config(path: str, default):
        mapping = {
            "crawler.mode": "mcp",
            "crawler.use_library_fallback": True,
            "search.http_fast_path_threshold": 0.6,
            "search.crawler_fast_path_threshold": 0.7,
        }
        return mapping.get(path, default)

    monkeypatch.setattr(pipeline, "_http_fallback_extract", fake_http)
    monkeypatch.setattr(pipeline, "_crawl_with_mcp", fake_mcp)
    monkeypatch.setattr(pipeline, "_crawl_with_library", fake_lib)
    monkeypatch.setattr(pipeline, "assess_content_quality", fake_quality)
    monkeypatch.setattr(pipeline, "get_config_value", fake_config)

    extracted, quality = asyncio.run(
        pipeline._extract_best_content(
            "https://example.com", query_id="q1", output_dir=str(tmp_path), source_statuses=[]
        )
    )
    assert extracted == "lib cleaned"
    assert float(quality["quality_score"]) == 0.8


def test_prune_markdown_files_removes_old_markdown_only(tmp_path: Path):
    old_dir = tmp_path / "old_pkg"
    old_dir.mkdir()
    old_md = old_dir / "result.md"
    old_json = old_dir / "result.json"
    old_md.write_text("old markdown", encoding="utf-8")
    old_json.write_text("old json", encoding="utf-8")

    new_dir = tmp_path / "new_pkg"
    new_dir.mkdir()
    new_md = new_dir / "result.md"
    new_md.write_text("new markdown", encoding="utf-8")

    old_timestamp = time.time() - (2 * 86400)
    new_timestamp = time.time() - 60
    os.utime(old_md, (old_timestamp, old_timestamp))
    os.utime(new_md, (new_timestamp, new_timestamp))

    result = prune_markdown_files(output_dir=tmp_path, older_than_seconds=86400)

    assert result["deleted_markdown_files"] == 1
    assert old_md.exists() is False
    assert old_json.exists() is True
    assert new_md.exists() is True


def test_maybe_prune_markdown_daily_throttles(tmp_path: Path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    md_path = pkg / "result.md"
    md_path.write_text("keep me for now", encoding="utf-8")
    timestamp = time.time() - (2 * 86400)
    os.utime(md_path, (timestamp, timestamp))

    first = maybe_prune_markdown_daily(output_dir=tmp_path, older_than_seconds=86400)
    second = maybe_prune_markdown_daily(output_dir=tmp_path, older_than_seconds=86400)

    assert first is not None
    assert second is None
    assert (tmp_path / THROTTLE_MARKER_FILENAME).exists()


def test_run_query_triggers_daily_prune(monkeypatch, tmp_path: Path):
    calls = {"prune": 0, "event_prune": 0}

    def fake_config(path: str, default):
        if path == "service.output_dir":
            return str(tmp_path)
        return default

    def fake_prune(*, output_dir: Path, older_than_seconds: int):
        calls["prune"] += 1
        assert output_dir == tmp_path.resolve()
        assert older_than_seconds == 86400
        return {"deleted_markdown_files": 0, "removed_empty_dirs": 0}

    def fake_event_prune(**_kwargs):
        calls["event_prune"] += 1
        return {"events_deleted": 0, "event_files_touched": 0, "event_files_deleted": 0}

    async def fake_query(_query: str):
        return [{"title": "A", "url": "https://a", "snippet": "aa"}]

    async def fake_enrich(results, *, query_id: str, output_dir: str, source_statuses: list[dict]):
        return results

    monkeypatch.setattr(pipeline, "get_config_value", fake_config)
    monkeypatch.setattr(pipeline, "maybe_prune_markdown_daily", fake_prune)
    monkeypatch.setattr(pipeline, "maybe_prune_event_logs_daily", fake_event_prune)
    monkeypatch.setattr(pipeline, "_query_searxng", fake_query)
    monkeypatch.setattr(pipeline, "_enrich_results", fake_enrich)

    payload = asyncio.run(pipeline.run_query(query="hello", write_markdown_package=False))
    assert payload["query"] == "hello"
    assert calls["prune"] == 1
    assert calls["event_prune"] == 1


def test_event_retention_and_dashboard_helpers(tmp_path: Path):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=8)).isoformat()

    append_event(output_dir=tmp_path, event={"event_type": "query_failed", "status": "failed", "timestamp": old, "query_id": "q-old"})
    append_event(output_dir=tmp_path, event={"event_type": "source_failed", "status": "failed", "timestamp": old, "query_id": "q-old", "url": "https://x", "mode": "http"})
    append_event(output_dir=tmp_path, event={"event_type": "query_succeeded", "status": "succeeded", "query_id": "q-new", "query": "hello", "total_ms": 12.3})
    append_event(output_dir=tmp_path, event={"event_type": "source_succeeded", "status": "succeeded", "query_id": "q-new", "url": "https://a", "mode": "http"})

    result = prune_events(
        output_dir=tmp_path,
        query_failure_ttl_seconds=86400,
        source_failure_ttl_seconds=7 * 86400,
        success_ttl_seconds=7 * 86400,
    )
    assert result["events_deleted"] >= 2

    summary = summarize_events(output_dir=tmp_path, window_seconds=7 * 86400)
    assert summary["succeeded_queries"] >= 1

    queries = list_query_events(output_dir=tmp_path, limit=10)
    assert isinstance(queries["items"], list)

    sources = list_source_events(output_dir=tmp_path, query_id="q-new")
    assert len(sources) >= 1


def test_write_package_generates_unique_dirs(tmp_path: Path):
    payload = {
        "query": "hello world",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_results": 0,
        "results": [],
    }

    package_one = write_package(payload=payload, output_dir=tmp_path)
    package_two = write_package(payload=payload, output_dir=tmp_path)

    assert package_one["dir"] != package_two["dir"]
    assert Path(package_one["json_path"]).exists()
    assert Path(package_one["markdown_path"]).exists()
    assert Path(package_two["json_path"]).exists()
    assert Path(package_two["markdown_path"]).exists()
