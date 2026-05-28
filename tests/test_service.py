from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from main import app
from schema.models import SearchRequest
from utils.cleanup import assess_content_quality


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
