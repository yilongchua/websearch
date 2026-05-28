# Current Setup (May 2026)

## Overview
This project now runs as an API-first search/extraction service with:
- `nginx` reverse proxy in front of `websearch1/2/3`
- FastAPI `POST /search` endpoint per websearch instance
- SearXNG discovery + Crawl4AI/HTTP extraction pipeline
- file-based event logging under `output/events/*.ndjson`
- **logs-only dashboard** via a dedicated `dashboard_logs` container

There is no web dashboard route in FastAPI.

## Runtime Topology (docker-compose.multi.yml)

```text
Client
  |
  | POST /search
  v
nginx:9000 (least_conn)
  |
  +--> websearch1:9000
  +--> websearch2:9000
  +--> websearch3:9000

Shared volume:
  ./output -> /app/output
  ├── result packages (result.json / result.md)
  └── events/*.ndjson

Sidecar:
  dashboard_logs container
  - reads /app/output/events/*.ndjson
  - emits JSON snapshots to stdout
  - consumed with: docker compose -f docker-compose.multi.yml logs -f dashboard_logs
```

## Canvas Flow (Request Handling)

```text
+--------------------------------------------------------------------------------+
|                             SEARCH REQUEST CANVAS                              |
+--------------------------------------------------------------------------------+
| 1) Client -> nginx:9000                                                        |
|    - nginx chooses upstream by least_conn                                      |
|                                                                                |
| 2) nginx -> websearchX FastAPI (/search)                                       |
|    - main.py -> run_query()                                                     |
|    - logs query_started event                                                   |
|                                                                                |
| 3) Discovery phase                                                              |
|    - _query_searxng(query) -> local SearXNG (:8080)                            |
|    - normalize URLs/snippets                                                    |
|                                                                                |
| 4) Enrichment phase (top_k, semaphore bounded)                                 |
|    - _extract_best_content(url):                                                |
|      a) HTTP fallback first (fast-path threshold)                              |
|      b) CLI crawler (or library mode)                                           |
|      c) optional library fallback if CLI fails                                  |
|    - assess_content_quality                                                     |
|    - pick best candidate                                                        |
|    - log source_attempted/succeeded/failed events with timing + errors          |
|                                                                                |
| 5) Response/build artifacts                                                     |
|    - optional package write: result.json + result.md                            |
|    - logs query_succeeded or query_failed event                                 |
|    - returns JSON response                                                      |
|                                                                                |
| 6) Retention on request path                                                    |
|    - prune markdown >24h                                                        |
|    - prune events by policy (query failure 24h, source failure 7d, success 7d) |
+--------------------------------------------------------------------------------+
```

## Dashboard (Docker Logs Only)

The dashboard is intentionally non-web and non-interactive in containers.

- Producer command:
  - `python /app/main.py dashboard-logs --output-dir /app/output --interval-seconds 10 --window-seconds 86400 --limit 10`
- Multi-instance consumer command:
  - `docker compose -f docker-compose.multi.yml logs -f dashboard_logs`

Each log line is a JSON snapshot containing:
- `summary` (query/source success/failure + avg latency)
- `latest_queries` (recent query statuses + markdown availability)

## LM Studio / Local LLM Handling

### What is used by this repo today
The websearch request pipeline itself does **not** call an LLM during `/search` execution.
- extraction quality is heuristic-based (`utils/cleanup.py`)
- no LM inference is required for normal query processing

### How LM Studio fits
`config/config.yaml` includes local OpenAI-compatible endpoints and model settings:
- `llm_endpoint.http: http://localhost:1234/v1`
- model entry uses `langchain_openai:ChatOpenAI`
- `base_url: $LOCAL_LLM_BASE_URL`

This is for external orchestration/integration paths (for example toolchains that consume this config), not for the core `/search` runtime path at present.

### LM Studio request path (when used externally)

```text
External orchestrator / agent runtime
  -> reads config/config.yaml model+endpoint
  -> OpenAI-compatible call to LM Studio (:1234/v1)
  -> model: mlx-community/qwen3.6-35b-a3b
  -> receives LLM response for its own reasoning/task flow
```

## Operational Notes
- `dashboard_logs` relies on shared output volume visibility across all `websearch` instances.
- `docker logs` is the intended dashboard UX; this avoids TTY/UI rendering issues in container logs.
- nginx only proxies API traffic now; no dashboard web routes are expected.
