# Senior Developer Code Review

Date: 2026-05-29

Scope: repo-wide review of the FastAPI websearch service, SearXNG/Crawl4AI pipeline, Docker deployment, multi-instance compose setup, retention/event logging, and tests. The review is focused on whether the implementation is deployable for high-volume agentic tool calls.

## Executive Summary

The service is directionally sound for a local or small internal deployment: the API shape is simple, the pipeline has bounded per-query extraction fan-out, package writes use unique directories, and the multi-instance compose file can distribute requests through nginx.

It is not yet production-ready for mass agentic tool calls. The main blockers are lack of admission control and request cancellation, expensive synchronous retention work on the request path, race-prone shared-file event pruning across instances, fragile live tests, and unpinned build dependencies. These are fixable, but they should be addressed before exposing this as a shared high-throughput tool endpoint.

## Findings

### High: no service-level backpressure or request budget

References: `main.py:37`, `utils/pipeline.py:378`, `utils/pipeline.py:398`, `utils/pipeline.py:402`

Each `/search` request can trigger a SearXNG call plus up to `extract_top_k` extraction flows, and each extraction can perform HTTP plus CLI/library crawling. The only concurrency bound is per request (`search.extract_max_workers`), not global per process or per deployment. Under agentic workloads, many agents can send concurrent searches and multiply crawler subprocesses, HTTP connections, CPU, memory, and outbound traffic.

Impact: latency collapse, container OOM, file descriptor exhaustion, upstream bans, and cascading failures across all colocated SearXNG/Crawl4AI workers.

Recommendation:
- Add a process-wide admission semaphore around `run_query`.
- Add per-client or token-based rate limits at nginx or the app layer.
- Add a queue timeout that returns `429` or `503` when saturated.
- Track active requests, queue depth, and crawler attempts in metrics.

### High: retention/prune work runs synchronously on normal request traffic

References: `utils/pipeline.py:403`, `utils/pipeline.py:404`, `utils/pipeline.py:405`, `utils/output_retention.py:22`, `utils/events.py:98`

Every `run_query` calls markdown and event pruning before doing search work. The throttle reduces frequency after marker files are current, but when the marker expires each instance can traverse `output/**/result.md` and rewrite event files before handling the user request.

Impact: one unlucky request pays filesystem traversal and log rewrite cost; in multi-instance mode, multiple containers can do the same work concurrently against the shared `./output` bind mount.

Recommendation:
- Move retention into a separate scheduled job or the existing `dashboard_logs` companion.
- Use a lock file if retention must remain in-process.
- Never rewrite hot event files from request-serving workers.

### High: event pruning can lose events under concurrent appends

References: `utils/events.py:43`, `utils/events.py:51`, `utils/events.py:98`, `utils/events.py:102`, `utils/events.py:139`, `docker-compose.multi.yml:36`, `docker-compose.multi.yml:69`, `docker-compose.multi.yml:104`

Appending uses `O_APPEND`, which is good for individual writes. Pruning reads an entire NDJSON file, filters it, and writes it back with `Path.write_text`. In multi-instance mode all websearch containers share `./output`; one process can append while another rewrites the file, dropping events written after the prune read but before the rewrite.

Impact: observability gaps and misleading dashboard summaries exactly when load is high.

Recommendation:
- Partition event files by instance and day, then compact offline.
- Or protect pruning with inter-process file locks and skip the current day's event file.
- Prefer stdout structured logs plus an external log collector for high-volume deployments.

### High: request cancellation does not stop in-flight work

References: `main.py:37`, `utils/pipeline.py:398`, `utils/pipeline.py:402`

The endpoint awaits `run_query` directly and does not check client disconnects or enforce an overall request timeout. If an agent client times out or disconnects, extraction work can continue until crawler/http timeouts complete.

Impact: abandoned requests consume crawler slots and subprocess resources, which is especially painful under bursty agent tool-call traffic.

Recommendation:
- Wrap `run_query` in an app-level timeout.
- Check `Request.is_disconnected()` where practical.
- Make worker tasks cancellation-aware and ensure subprocesses are killed on cancellation.

### Medium: build inputs are not reproducible

References: `Dockerfile:3`, `Dockerfile:34`, `Dockerfile:46`, `Dockerfile:49`, `requirements.txt:1`

The Docker build defaults to SearXNG `master`, installs `crawl4ai` with `-U`, and uses broad dependency ranges for API packages. A rebuild can silently change runtime behavior or break the image.

Impact: deployments become hard to reproduce and rollback; mass-agent reliability will vary by build time.

Recommendation:
- Pin SearXNG to a tested commit or release.
- Pin `crawl4ai` and Python dependencies with hashes or a lock file.
- Build and promote immutable images instead of rebuilding from floating refs.

### Medium: `/search` leaks raw exception details and collapses all failures to 500

References: `main.py:46`, `main.py:47`, `utils/pipeline.py:476`, `utils/pipeline.py:487`

Any exception returns `HTTPException(status_code=500, detail=str(exc))`. This exposes raw upstream/crawler error text to clients and does not distinguish invalid input, upstream timeout, saturation, and unexpected server errors.

Impact: agents receive poor retry signals, and internal details may leak in API responses.

Recommendation:
- Map upstream timeouts to `504`, saturation to `429` or `503`, validation/user issues to `400`, and unexpected failures to a generic `500`.
- Return stable error codes suitable for tool clients.
- Log detailed exceptions server-side with `query_id`.

### Medium: input schema is too permissive for automated agents

References: `schema/models.py:9`, `schema/models.py:10`, `schema/models.py:12`

The request accepts any non-empty query and arbitrary package names. `package_name` is slugified later, but there is no query length, package name length, or request body budget.

Impact: very large queries can increase SearXNG/proxy load, produce huge logs, and make package/event records noisy.

Recommendation:
- Add `max_length` constraints for `query` and `package_name`.
- Strip whitespace and reject empty-after-strip queries.
- Consider an explicit `extract_top_k` override only if it is bounded and authorized.

### Medium: HTTP fallback creates a new client for every extracted URL

References: `utils/pipeline.py:126`, `utils/pipeline.py:133`, `utils/pipeline.py:134`

The SearXNG client is shared, but fallback extraction creates a fresh `httpx.AsyncClient` per URL. Under high concurrency, this loses connection pooling and creates avoidable socket churn.

Impact: higher latency and resource use under load.

Recommendation:
- Initialize and reuse a shared extraction HTTP client with connection limits.
- Configure total connect/read/write/pool timeouts.

### Medium: Docker environment variables suggest overrides that are not actually read

References: `Dockerfile:16`, `docker-compose.yml:16`, `docker-compose.multi.yml:34`, `utils/config.py:72`, `utils/config.py:91`

`WEBSEARCH_OUTPUT_DIR` is set in Docker/compose, but `utils.config` only reads `WEBSEARCH_CONFIG_PATH` and YAML values. `service.output_dir` remains whatever is in config unless the YAML file changes.

Impact: operators may believe they changed the output directory while the app continues writing to `/app/output`.

Recommendation:
- Either remove unused env vars or explicitly map environment overrides into config.
- Document the precedence rules.

### Low: single-instance compose has a typo in the SearXNG port variable

Reference: `docker-compose.yml:20`

The compose file uses `WEBSEARCHSEARXNG_BIND_PORT` without an underscore. This likely intended `WEBSEARCH_SEARXNG_BIND_PORT` or `SEARXNG_BIND_PORT`.

Impact: custom port overrides will not work as expected.

Recommendation: rename the variable and update README/env examples.

### Low: live endpoint tests are brittle and run by default

References: `tests/test_live_endpoint.py:7`, `tests/test_live_endpoint.py:10`, `tests/test_live_endpoint.py:18`, `tests/test_live_endpoint.py:36`

The test suite includes live tests pointed at `http://192.168.1.39:9000`. They are not skipped by default and fail when that host is unavailable.

Impact: CI and local verification fail even when unit/integration tests are healthy.

Recommendation:
- Gate live tests behind an environment variable such as `WEBSEARCH_LIVE_TEST_URL`.
- Mark them with `pytest.mark.live`.
- Default to `localhost:9000` only when explicitly enabled.

### Low: generated artifacts are present in the source tree

References: `__pycache__/`, `schema/__pycache__/`, `utils/__pycache__/`, `tests/__pycache__/`, `scripts/__pycache__/`, `output/`

The repo contains Python bytecode caches and sample output packages.

Impact: noisy diffs, larger Docker build contexts, and accidental publication of runtime data.

Recommendation:
- Add `.gitignore` and `.dockerignore`.
- Exclude `__pycache__/`, `.pytest_cache/`, `output/`, and local env files from image build context and version control.

## Deployability Assessment

For local or controlled internal use: acceptable after fixing the live test gating and compose typo.

For mass agentic tool calls: not ready yet. The service needs global concurrency limits, deterministic builds, safer event retention, clearer failure semantics, and operational controls before it can reliably absorb high concurrent tool-call traffic.

## Positive Notes

- The FastAPI surface is intentionally small and easy for agents to call.
- The response schema is JSON-first and avoids requiring clients to scrape generated files.
- Per-query extraction fan-out is bounded by `extract_top_k` and `extract_max_workers`.
- Package output uses unique directory names with timestamp, hostname, and random tokens.
- Event append writes are simple NDJSON and use append mode.
- The multi-instance compose setup has a straightforward nginx `least_conn` fan-out.

## Verification

Command run:

```bash
pytest -q
```

Result:

```text
3 failed, 15 passed in 10.64s
```

Failures:

- `tests/test_live_endpoint.py::test_health` timed out connecting to `http://192.168.1.39:9000`.
- `tests/test_live_endpoint.py::test_search_simple` failed with host down for `http://192.168.1.39:9000`.
- `tests/test_live_endpoint.py::test_search_with_package` failed with host down for `http://192.168.1.39:9000`.

The 15 non-live tests passed.

## Recommended Priority Order

1. Gate live tests and add `.gitignore`/`.dockerignore`.
2. Add app-wide concurrency limits and queue timeout for `/search`.
3. Move retention pruning out of the request path.
4. Make event retention safe for multi-instance shared output.
5. Pin Docker and Python dependency versions.
6. Improve error mapping and client-safe error responses.
7. Add bounded request schema fields and shared extraction HTTP client.
