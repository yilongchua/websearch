from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils import pipeline
from utils.events import (
    append_event,
    failed_domains,
    list_query_events,
    list_source_events,
    prune_events,
    registrable_domain,
    summarize_events,
)
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

    async def fake_query(_query: str, *, blocklist=None):
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


def test_enrich_results_skips_failed_sources_and_continues_until_top_k(monkeypatch, tmp_path: Path):
    results = [
        {"title": "one", "url": "https://one.example", "snippet": "one"},
        {"title": "two", "url": "https://two.example", "snippet": "two"},
        {"title": "three", "url": "https://three.example", "snippet": "three"},
    ]

    async def fake_extract(url: str, *, query_id: str, output_dir: str, source_statuses: list[dict]):
        if "one.example" in url:
            return "", {}
        return f"content for {url}", {"quality_score": 0.9, "quality_reasons": ["ok"]}

    def fake_config(path: str, default):
        mapping = {
            "search.extract_top_k": 2,
            "search.extract_max_workers": 1,
        }
        return mapping.get(path, default)

    monkeypatch.setattr(pipeline, "_extract_best_content", fake_extract)
    monkeypatch.setattr(pipeline, "get_config_value", fake_config)

    enriched = asyncio.run(
        pipeline._enrich_results(
            results,
            query_id="q1",
            output_dir=str(tmp_path),
            source_statuses=[],
        )
    )

    # First candidate failed and was filtered out; next two were used to satisfy top_k=2 successes.
    assert "extracted_content" not in enriched[0]
    assert enriched[1]["extracted_content"].startswith("content for https://two.example")
    assert enriched[2]["extracted_content"].startswith("content for https://three.example")


def test_registrable_domain_collapses_to_root():
    assert registrable_domain("https://m.facebook.com/some/path") == "facebook.com"
    assert registrable_domain("www.facebook.com") == "facebook.com"
    assert registrable_domain("https://news.bbc.co.uk/story") == "bbc.co.uk"
    assert registrable_domain("http://example.com:8080/x") == "example.com"
    assert registrable_domain("example.com") == "example.com"
    assert registrable_domain("") == ""


def test_failed_domains_threshold_and_mode_fallback(tmp_path: Path):
    # URL fails on http but succeeds via cli -> NOT a failure (per-mode events).
    append_event(output_dir=tmp_path, event={"event_type": "source_failed", "status": "failed", "query_id": "q1", "url": "https://recovers.com/1", "mode": "http"})
    append_event(output_dir=tmp_path, event={"event_type": "source_succeeded", "status": "succeeded", "query_id": "q1", "url": "https://recovers.com/1", "mode": "cli"})

    # Two distinct failed URLs under blocked.com -> meets default threshold of 2.
    append_event(output_dir=tmp_path, event={"event_type": "source_failed", "status": "failed", "query_id": "q1", "url": "https://blocked.com/a", "mode": "http"})
    append_event(output_dir=tmp_path, event={"event_type": "source_failed", "status": "failed", "query_id": "q2", "url": "https://blocked.com/b", "mode": "cli"})

    # Subdomains collapse to the same root domain (facebook.com).
    append_event(output_dir=tmp_path, event={"event_type": "source_failed", "status": "failed", "query_id": "q1", "url": "https://www.facebook.com/x", "mode": "http"})
    append_event(output_dir=tmp_path, event={"event_type": "source_failed", "status": "failed", "query_id": "q2", "url": "https://m.facebook.com/y", "mode": "http"})

    # Single failure -> below threshold of 2.
    append_event(output_dir=tmp_path, event={"event_type": "source_failed", "status": "failed", "query_id": "q1", "url": "https://once.com/1", "mode": "http"})

    blocked = failed_domains(output_dir=tmp_path, lookback_seconds=604800, min_failures=2)
    assert blocked == {"blocked.com", "facebook.com"}

    relaxed = failed_domains(output_dir=tmp_path, lookback_seconds=604800, min_failures=1)
    assert "once.com" in relaxed
    assert "recovers.com" not in relaxed


def test_query_searxng_filters_blocklist_and_pages_to_refill(monkeypatch):
    pages: dict[int, list[dict]] = {
        1: [
            {"title": "x", "url": "https://blocked.com/a", "content": "no"},
            {"title": "g1", "url": "https://good1.com/x", "content": "yes"},
            {"title": "x", "url": "https://blocked.com/b", "content": "no"},
            {"title": "g2", "url": "https://good2.com/y", "content": "yes"},
        ],
        2: [
            {"title": "g1dup", "url": "https://good1.com/x", "content": "dup"},
            {"title": "g3", "url": "https://good3.com/z", "content": "yes"},
        ],
    }
    fetched_pages: list[int] = []

    async def fake_fetch(_endpoint: str, params: dict):
        page = int(params.get("pageno", 1))
        fetched_pages.append(page)
        return {"results": pages.get(page, [])}

    def fake_config(path: str, default):
        mapping = {
            "search.max_results": 3,
            "search.blocklist_max_pages": 3,
        }
        return mapping.get(path, default)

    monkeypatch.setattr(pipeline, "_searxng_fetch", fake_fetch)
    monkeypatch.setattr(pipeline, "get_config_value", fake_config)

    results = asyncio.run(pipeline._query_searxng("hello", blocklist={"blocked.com"}))

    urls = [item["url"] for item in results]
    assert urls == ["https://good1.com/x", "https://good2.com/y", "https://good3.com/z"]
    # Page 1 left us one short after filtering, so page 2 was fetched to refill.
    assert fetched_pages == [1, 2]


def test_query_searxng_single_page_without_blocklist(monkeypatch):
    fetched_pages: list[int] = []

    async def fake_fetch(_endpoint: str, params: dict):
        fetched_pages.append(int(params.get("pageno", 1)))
        return {"results": [{"title": "g1", "url": "https://good1.com/x", "content": "yes"}]}

    def fake_config(path: str, default):
        return {"search.max_results": 3, "search.blocklist_max_pages": 3}.get(path, default)

    monkeypatch.setattr(pipeline, "_searxng_fetch", fake_fetch)
    monkeypatch.setattr(pipeline, "get_config_value", fake_config)

    results = asyncio.run(pipeline._query_searxng("hello"))

    assert [item["url"] for item in results] == ["https://good1.com/x"]
    # No blocklist -> never paginates past the first response.
    assert fetched_pages == [1]
