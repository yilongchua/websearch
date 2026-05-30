from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .cleanup import assess_content_quality
from .config import get_config_value
from .events import append_event, failed_domains, registrable_domain
from .output_retention import maybe_prune_event_logs_daily, maybe_prune_markdown_daily
from .packaging import write_package

_SEARX_CLIENT: httpx.AsyncClient | None = None


def _searxng_timeout_seconds() -> float:
    return float(get_config_value("search.searxng_timeout_seconds", 30.0))


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _error_payload(exc: Exception) -> dict[str, str]:
    return {
        "error_type": exc.__class__.__name__,
        "error_message": str(exc),
    }


def _is_retryable_searxng_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        return status_code == 408 or status_code == 429 or status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.TransportError))


def _record_source_event(
    *,
    output_dir: str,
    query_id: str,
    url: str,
    mode: str,
    event_type: str,
    duration_ms: float,
    status: str,
    quality_score: float | None = None,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_type": event_type,
        "query_id": query_id,
        "url": url,
        "mode": mode,
        "status": status,
        "duration_ms": round(duration_ms, 2),
    }
    if quality_score is not None:
        payload["quality_score"] = round(float(quality_score), 4)
    if error:
        payload.update(error)
    return append_event(output_dir=output_dir, event=payload)


async def _crawl_with_crwl_cli(url: str) -> str:
    command = [str(get_config_value("crawler.crwl_binary", "crwl")), url, "-o", "markdown"]
    deep_mode = str(get_config_value("crawler.deep_crawl", "none")).strip().lower()
    if deep_mode in {"bfs", "dfs"}:
        command.extend(["--deep-crawl", deep_mode, "--max-pages", str(int(get_config_value("crawler.deep_max_pages", 10)))])

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timeout_seconds = float(get_config_value("search.extract_timeout_seconds", 45.0)) + 15
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError("crwl command timed out") from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"crwl exited with status {process.returncode}")

    payload = stdout.decode("utf-8", errors="replace").strip()
    if not payload:
        raise RuntimeError("crwl returned empty output")
    return payload[: int(get_config_value("search.extract_max_chars", 6000))]


async def _crawl_with_library(url: str) -> str:
    from crawl4ai import AsyncWebCrawler

    max_attempts = int(get_config_value("search.extract_max_retries", 1)) + 1
    timeout_seconds = float(get_config_value("search.extract_timeout_seconds", 45.0))
    retry_backoff = float(get_config_value("search.extract_retry_backoff_seconds", 1.5))
    extract_max_chars = int(get_config_value("search.extract_max_chars", 6000))

    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            async with AsyncWebCrawler() as crawler:
                result = await asyncio.wait_for(crawler.arun(url=url), timeout=timeout_seconds)

            if not result.success:
                raise RuntimeError(result.error_message or "crawl4ai fetch failed")

            title = (result.metadata or {}).get("title", "Untitled")
            markdown = str(result.markdown) if result.markdown else ""
            if not markdown:
                markdown = (result.cleaned_html or result.html or "")[:extract_max_chars]
            else:
                markdown = markdown[:extract_max_chars]
            return f"# {title}\n\n{markdown}"
        except Exception as exc:
            last_error = exc
            if attempt < (max_attempts - 1):
                await asyncio.sleep(retry_backoff * (attempt + 1))

    if last_error is None:
        raise RuntimeError("crawl4ai library mode failed")
    raise RuntimeError(f"crawl4ai library mode failed after {max_attempts} attempt(s): {last_error}") from last_error


async def _http_fallback_extract(url: str) -> tuple[str, dict[str, str] | None]:
    if not bool(get_config_value("crawler.http_fallback_enabled", True)):
        return "", {"error_type": "Disabled", "error_message": "http_fallback_disabled"}

    try:
        timeout_seconds = float(get_config_value("search.extract_timeout_seconds", 45.0))
        user_agent = str(get_config_value("service.user_agent", "Mozilla/5.0"))
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": user_agent})
            response.raise_for_status()

        html = response.text
        title_match = re.search(r"(?is)<title>(.*?)</title>", html)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else url
        body = _html_to_text(html)[: int(get_config_value("search.extract_max_chars", 6000))]
        if not body:
            return "", {"error_type": "EmptyContent", "error_message": "http_empty_content"}
        return f"# {title}\n\n{body}", None
    except Exception as exc:
        return "", _error_payload(exc)


async def _extract_best_content(
    url: str,
    *,
    query_id: str,
    output_dir: str,
    source_statuses: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    candidates: list[tuple[str, dict[str, Any]]] = []
    mode = str(get_config_value("crawler.mode", "cli")).lower().strip()
    use_library_fallback = bool(get_config_value("crawler.use_library_fallback", True))
    http_threshold = float(get_config_value("search.http_fast_path_threshold", 0.6))
    crawler_threshold = float(get_config_value("search.crawler_fast_path_threshold", 0.7))

    start = time.perf_counter()
    append_event(output_dir=output_dir, event={"event_type": "source_attempted", "query_id": query_id, "url": url, "mode": "http", "status": "attempted"})
    http_result, http_error = await _http_fallback_extract(url)
    http_ms = (time.perf_counter() - start) * 1000
    if http_result:
        http_quality = assess_content_quality(http_result)
        http_score = float(http_quality.get("quality_score") or 0.0)
        source_statuses.append({"url": url, "mode": "http", "status": "succeeded", "duration_ms": round(http_ms, 2), "quality_score": http_score})
        _record_source_event(
            output_dir=output_dir,
            query_id=query_id,
            url=url,
            mode="http",
            event_type="source_succeeded",
            duration_ms=http_ms,
            status="succeeded",
            quality_score=http_score,
        )
        if http_score >= http_threshold:
            cleaned = str(http_quality.get("cleaned_text") or "").strip()
            return cleaned or http_result, http_quality
        candidates.append((http_result, http_quality))
    else:
        source_statuses.append({"url": url, "mode": "http", "status": "failed", "duration_ms": round(http_ms, 2), **(http_error or {})})
        _record_source_event(
            output_dir=output_dir,
            query_id=query_id,
            url=url,
            mode="http",
            event_type="source_failed",
            duration_ms=http_ms,
            status="failed",
            error=http_error,
        )

    if mode == "cli":
        cli_start = time.perf_counter()
        append_event(output_dir=output_dir, event={"event_type": "source_attempted", "query_id": query_id, "url": url, "mode": "cli", "status": "attempted"})
        try:
            cli_result = await _crawl_with_crwl_cli(url)
            cli_quality = assess_content_quality(cli_result)
            cli_score = float(cli_quality.get("quality_score") or 0.0)
            cli_ms = (time.perf_counter() - cli_start) * 1000
            source_statuses.append({"url": url, "mode": "cli", "status": "succeeded", "duration_ms": round(cli_ms, 2), "quality_score": cli_score})
            _record_source_event(
                output_dir=output_dir,
                query_id=query_id,
                url=url,
                mode="cli",
                event_type="source_succeeded",
                duration_ms=cli_ms,
                status="succeeded",
                quality_score=cli_score,
            )
            if cli_score >= crawler_threshold:
                cleaned = str(cli_quality.get("cleaned_text") or "").strip()
                return cleaned or cli_result, cli_quality
            candidates.append((cli_result, cli_quality))
        except Exception as exc:
            cli_ms = (time.perf_counter() - cli_start) * 1000
            error = _error_payload(exc)
            source_statuses.append({"url": url, "mode": "cli", "status": "failed", "duration_ms": round(cli_ms, 2), **error})
            _record_source_event(
                output_dir=output_dir,
                query_id=query_id,
                url=url,
                mode="cli",
                event_type="source_failed",
                duration_ms=cli_ms,
                status="failed",
                error=error,
            )
            if use_library_fallback:
                lib_start = time.perf_counter()
                append_event(output_dir=output_dir, event={"event_type": "source_attempted", "query_id": query_id, "url": url, "mode": "library", "status": "attempted"})
                try:
                    lib_result = await _crawl_with_library(url)
                    lib_quality = assess_content_quality(lib_result)
                    lib_score = float(lib_quality.get("quality_score") or 0.0)
                    lib_ms = (time.perf_counter() - lib_start) * 1000
                    source_statuses.append({"url": url, "mode": "library", "status": "succeeded", "duration_ms": round(lib_ms, 2), "quality_score": lib_score})
                    _record_source_event(
                        output_dir=output_dir,
                        query_id=query_id,
                        url=url,
                        mode="library",
                        event_type="source_succeeded",
                        duration_ms=lib_ms,
                        status="succeeded",
                        quality_score=lib_score,
                    )
                    candidates.append((lib_result, lib_quality))
                except Exception as lib_exc:
                    lib_ms = (time.perf_counter() - lib_start) * 1000
                    lib_error = _error_payload(lib_exc)
                    source_statuses.append({"url": url, "mode": "library", "status": "failed", "duration_ms": round(lib_ms, 2), **lib_error})
                    _record_source_event(
                        output_dir=output_dir,
                        query_id=query_id,
                        url=url,
                        mode="library",
                        event_type="source_failed",
                        duration_ms=lib_ms,
                        status="failed",
                        error=lib_error,
                    )
    else:
        lib_start = time.perf_counter()
        append_event(output_dir=output_dir, event={"event_type": "source_attempted", "query_id": query_id, "url": url, "mode": "library", "status": "attempted"})
        try:
            lib_result = await _crawl_with_library(url)
            lib_quality = assess_content_quality(lib_result)
            lib_score = float(lib_quality.get("quality_score") or 0.0)
            lib_ms = (time.perf_counter() - lib_start) * 1000
            source_statuses.append({"url": url, "mode": "library", "status": "succeeded", "duration_ms": round(lib_ms, 2), "quality_score": lib_score})
            _record_source_event(
                output_dir=output_dir,
                query_id=query_id,
                url=url,
                mode="library",
                event_type="source_succeeded",
                duration_ms=lib_ms,
                status="succeeded",
                quality_score=lib_score,
            )
            if lib_score >= crawler_threshold:
                cleaned = str(lib_quality.get("cleaned_text") or "").strip()
                return cleaned or lib_result, lib_quality
            candidates.append((lib_result, lib_quality))
        except Exception as exc:
            lib_ms = (time.perf_counter() - lib_start) * 1000
            error = _error_payload(exc)
            source_statuses.append({"url": url, "mode": "library", "status": "failed", "duration_ms": round(lib_ms, 2), **error})
            _record_source_event(
                output_dir=output_dir,
                query_id=query_id,
                url=url,
                mode="library",
                event_type="source_failed",
                duration_ms=lib_ms,
                status="failed",
                error=error,
            )

    if not candidates:
        return "", {}

    best_text, best_quality = max(candidates, key=lambda item: float(item[1].get("quality_score") or 0.0))
    cleaned_text = str(best_quality.get("cleaned_text") or "").strip()
    return cleaned_text or best_text, best_quality


async def _searxng_fetch(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    max_retries = max(0, int(get_config_value("search.searxng_max_retries", 2)))
    retry_backoff_seconds = max(0.0, float(get_config_value("search.searxng_retry_backoff_seconds", 1.0)))
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            if _SEARX_CLIENT is not None:
                response = await _SEARX_CLIENT.get(endpoint, params=params)
                response.raise_for_status()
                return response.json()
            async with httpx.AsyncClient(timeout=_searxng_timeout_seconds()) as client:
                response = await client.get(endpoint, params=params)
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            last_exc = exc
            if attempt >= max_retries or not _is_retryable_searxng_error(exc):
                raise
            await asyncio.sleep(retry_backoff_seconds * (attempt + 1))

    raise RuntimeError("SearXNG query failed") from last_exc


async def _query_searxng(query: str, *, blocklist: set[str] | None = None) -> list[dict[str, Any]]:
    blocklist = blocklist or set()
    endpoint = f"{str(get_config_value('service.searxng_base_url', 'http://127.0.0.1:8080')).rstrip('/')}/search"
    params: dict[str, Any] = {
        "q": query,
        "format": "json",
    }

    language = get_config_value("search.language", None)
    if language:
        params["language"] = str(language)

    safesearch = get_config_value("search.safesearch", None)
    if safesearch is not None:
        params["safesearch"] = str(safesearch)

    categories = get_config_value("search.categories", [])
    if isinstance(categories, list) and categories:
        params["categories"] = ",".join(str(item).strip() for item in categories if str(item).strip())

    engines = get_config_value("search.engines", [])
    if isinstance(engines, list) and engines:
        params["engines"] = ",".join(str(item).strip() for item in engines if str(item).strip())

    max_results = int(get_config_value("search.max_results", 5))
    # Only page past the first response when the blocklist might thin the pool;
    # this keeps the common (no-blocklist) path to a single request.
    max_pages = max(1, int(get_config_value("search.blocklist_max_pages", 3))) if blocklist else 1

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        page_params = dict(params)
        if page > 1:
            page_params["pageno"] = page

        body = await _searxng_fetch(endpoint, page_params)
        raw_results = body.get("results", []) if isinstance(body, dict) else []
        if not raw_results:
            break

        for item in raw_results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            if blocklist and registrable_domain(url) in blocklist:
                continue
            normalized.append(
                {
                    "title": str(item.get("title") or "").strip(),
                    "url": url,
                    "snippet": str(item.get("content") or item.get("snippet") or item.get("description") or "").strip(),
                }
            )
            if len(normalized) >= max_results:
                break

        if len(normalized) >= max_results:
            break

    return normalized


async def _enrich_results(
    results: list[dict[str, Any]],
    *,
    query_id: str,
    output_dir: str,
    source_statuses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    target_successes = max(0, min(int(get_config_value("search.extract_top_k", 2)), len(results)))
    if target_successes == 0:
        return results

    max_workers = max(1, int(get_config_value("search.extract_max_workers", 2)))
    sem = asyncio.Semaphore(max_workers)

    async def worker(index: int) -> bool:
        async with sem:
            extracted, quality = await _extract_best_content(
                results[index]["url"],
                query_id=query_id,
                output_dir=output_dir,
                source_statuses=source_statuses,
            )
            if extracted:
                results[index]["extracted_content"] = extracted
                results[index]["extracted_content_quality"] = quality
                results[index]["content_quality_score"] = float(quality.get("quality_score") or 0.0)
                results[index]["content_quality_reasons"] = quality.get("quality_reasons", [])
                return True
            return False

    successes = 0
    next_index = 0
    total = len(results)
    while next_index < total and successes < target_successes:
        batch_end = min(total, next_index + max_workers)
        batch_indices = list(range(next_index, batch_end))
        batch_successes = await asyncio.gather(*(worker(i) for i in batch_indices))
        successes += sum(1 for item in batch_successes if item)
        next_index = batch_end
    return results


async def run_query(*, query: str, write_markdown_package: bool = True, package_name: str | None = None) -> dict[str, Any]:
    output_dir = str(get_config_value("service.output_dir", "/app/output"))
    maybe_prune_markdown_daily(output_dir=Path(output_dir), older_than_seconds=86400)
    maybe_prune_event_logs_daily(
        output_dir=Path(output_dir),
        query_failure_ttl_seconds=int(get_config_value("retention.query_failure_seconds", 86400)),
        source_failure_ttl_seconds=int(get_config_value("retention.source_failure_seconds", 7 * 86400)),
        success_ttl_seconds=int(get_config_value("retention.success_event_seconds", 7 * 86400)),
        throttle_seconds=int(get_config_value("retention.event_prune_throttle_seconds", 86400)),
    )

    include_debug = bool(get_config_value("debug.include_query_diagnostics", False))
    query_id = uuid.uuid4().hex
    source_statuses: list[dict[str, Any]] = []
    started = time.perf_counter()

    blocklist: set[str] = set()
    if bool(get_config_value("search.blocklist_enabled", True)):
        blocklist = failed_domains(
            output_dir=output_dir,
            lookback_seconds=int(get_config_value("search.blocklist_lookback_seconds", get_config_value("retention.source_failure_seconds", 604800))),
            min_failures=int(get_config_value("search.blocklist_min_failures", 2)),
        )

    append_event(output_dir=output_dir, event={"event_type": "query_started", "query_id": query_id, "query": query, "status": "started"})

    try:
        searx_start = time.perf_counter()
        results = await _query_searxng(query, blocklist=blocklist)
        searx_ms = (time.perf_counter() - searx_start) * 1000

        enrich_start = time.perf_counter()
        results = await _enrich_results(results, query_id=query_id, output_dir=output_dir, source_statuses=source_statuses)
        enrich_ms = (time.perf_counter() - enrich_start) * 1000

        total_ms = (time.perf_counter() - started) * 1000
        payload: dict[str, Any] = {
            "query": query,
            "generated_at": datetime.now(timezone.utc),
            "total_results": len(results),
            "results": results,
        }

        if write_markdown_package:
            payload["package"] = write_package(
                payload={
                    "query": query,
                    "generated_at": payload["generated_at"].isoformat(),
                    "total_results": payload["total_results"],
                    "results": results,
                },
                output_dir=output_dir,
                package_name=package_name,
            )

        if include_debug:
            payload["query_id"] = query_id
            payload["timings"] = {
                "total_ms": round(total_ms, 2),
                "searx_ms": round(searx_ms, 2),
                "enrich_ms": round(enrich_ms, 2),
            }
            payload["source_statuses"] = source_statuses
            payload["blocked_domains"] = sorted(blocklist)

        append_event(
            output_dir=output_dir,
            event={
                "event_type": "query_succeeded",
                "query_id": query_id,
                "query": query,
                "status": "succeeded",
                "total_ms": round(total_ms, 2),
                "timings": {
                    "searx_ms": round(searx_ms, 2),
                    "enrich_ms": round(enrich_ms, 2),
                },
                "total_results": len(results),
                "source_statuses": source_statuses,
                "package": payload.get("package"),
            },
        )
        return payload
    except Exception as exc:
        total_ms = (time.perf_counter() - started) * 1000
        append_event(
            output_dir=output_dir,
            event={
                "event_type": "query_failed",
                "query_id": query_id,
                "query": query,
                "status": "failed",
                "total_ms": round(total_ms, 2),
                "source_statuses": source_statuses,
                **_error_payload(exc),
            },
        )
        raise


async def initialize_shared_clients() -> None:
    global _SEARX_CLIENT
    if _SEARX_CLIENT is None:
        _SEARX_CLIENT = httpx.AsyncClient(timeout=_searxng_timeout_seconds())


async def shutdown_shared_clients() -> None:
    global _SEARX_CLIENT
    if _SEARX_CLIENT is not None:
        await _SEARX_CLIENT.aclose()
        _SEARX_CLIENT = None
