from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from main import app
from schema.models import SearchRequest
from utils.cleanup import assess_content_quality
from utils import pipeline


@pytest.mark.parametrize("query", ["q", "search this"])
def test_schema_accepts_query(query: str):
    payload = SearchRequest(query=query)
    assert payload.query == query


def test_schema_rejects_empty_query():
    with pytest.raises(Exception):
        SearchRequest(query="")


def test_cleanup_quality_has_prompt_flag():
    quality = assess_content_quality("# Title\n\nUseful body text with details and context.")
    assert "quality_score" in quality
    assert "cleanup_prompt_used" in quality


def test_search_endpoint_returns_json(monkeypatch: pytest.MonkeyPatch):
    async def fake_run_query(*, query: str, write_markdown_package: bool = False, package_name: str | None = None):
        return {
            "query": query,
            "generated_at": datetime.now(timezone.utc),
            "total_results": 1,
            "results": [{"title": "A", "url": "https://a", "snippet": "aa"}],
        }

    monkeypatch.setattr("main.run_query", fake_run_query)
    client = TestClient(app)

    response = client.post("/search", json={"query": "hello"})
    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "hello"
    assert isinstance(body["results"], list)


def test_search_endpoint_package_paths(monkeypatch: pytest.MonkeyPatch):
    async def fake_run_query(*, query: str, write_markdown_package: bool = False, package_name: str | None = None):
        return {
            "query": query,
            "generated_at": datetime.now(timezone.utc),
            "total_results": 1,
            "results": [{"title": "A", "url": "https://a", "snippet": "aa"}],
            "package": {
                "dir": "/tmp/pkg",
                "json_path": "/tmp/pkg/result.json",
                "markdown_path": "/tmp/pkg/result.md",
            },
        }

    monkeypatch.setattr("main.run_query", fake_run_query)
    client = TestClient(app)

    response = client.post("/search", json={"query": "hello", "write_markdown_package": True})
    assert response.status_code == 200
    package = response.json().get("package")
    assert package is not None
    assert package["markdown_path"].endswith(".md")
    assert package["json_path"].endswith(".json")


def test_dashboard_route_not_mounted():
    client = TestClient(app)
    assert client.get("/dashboard").status_code == 404


def test_searxng_query_retries_transient_timeout(monkeypatch: pytest.MonkeyPatch):
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "results": [
                    {
                        "title": "Recovered",
                        "url": "https://example.com/recovered",
                        "content": "ok",
                    }
                ]
            }

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get(self, endpoint: str, params: dict):
            self.calls += 1
            if self.calls == 1:
                raise pipeline.httpx.ReadTimeout("temporary searxng timeout")
            return FakeResponse()

    config = {
        "service.searxng_base_url": "http://searxng.test",
        "search.language": "en-US",
        "search.safesearch": 1,
        "search.categories": [],
        "search.engines": [],
        "search.searxng_max_retries": 1,
        "search.searxng_retry_backoff_seconds": 0,
        "search.max_results": 5,
    }
    fake_client = FakeClient()

    monkeypatch.setattr(pipeline, "_SEARX_CLIENT", fake_client)
    monkeypatch.setattr(pipeline, "get_config_value", lambda key, default=None: config.get(key, default))

    results = asyncio.run(pipeline._query_searxng("hello"))

    assert fake_client.calls == 2
    assert results == [
        {
            "title": "Recovered",
            "url": "https://example.com/recovered",
            "snippet": "ok",
        }
    ]
