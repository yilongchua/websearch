from __future__ import annotations

import json
import httpx
import pytest

BASE_URL = "http://192.168.1.39:9000"


def test_health():
    resp = httpx.get(f"{BASE_URL}/health", timeout=10)
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] == "true"
    assert data["service"] == "websearch"


def test_search_simple():
    resp = httpx.post(
        f"{BASE_URL}/search",
        json={"query": "latest ai news"},
        timeout=90,
    )
    assert resp.status_code == 200
    data = resp.json()
    print("\n" + "=" * 60)
    print("test_search_simple - FULL RESPONSE:")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print()
    assert "query" in data
    assert data["query"] == "latest ai news"
    assert "results" in data
    assert isinstance(data["results"], list)


def test_search_with_package():
    resp = httpx.post(
        f"{BASE_URL}/search",
        json={
            "query": "latest news in singapore",
            "write_markdown_package": True,
        },
        timeout=90,
    )
    assert resp.status_code == 200
    import json
    data = resp.json()
    print("\n=== FULL RESPONSE ===")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print("=" * 60)
    assert "query" in data
    assert "results" in data
    package = data.get("package")
    assert package is not None
    assert "json_path" in package
    assert "markdown_path" in package
