# Websearch: Current Sequence Diagrams

## 1. End-to-End Request Flow (Current)

```text
Client            nginx             FastAPI(websearchX)       Pipeline            SearXNG        Extractors
  |                 |                        |                    |                    |                |
  | POST /search    |                        |                    |                    |                |
  |---------------->|                        |                    |                    |                |
  |                 | least_conn upstream    |                    |                    |                |
  |                 |----------------------->| run_query()        |                    |                |
  |                 |                        |------------------->| _query_searxng()   |                |
  |                 |                        |                    |------------------->| /search?json   |
  |                 |                        |                    |<-------------------| results        |
  |                 |                        |                    | _enrich_results()  |                |
  |                 |                        |                    |  top_k parallel    |                |
  |                 |                        |                    |  per URL:          |                |
  |                 |                        |                    |  - HTTP fast-path  |                |
  |                 |                        |                    |  - CLI/library     |                |
  |                 |                        |                    |  - quality score   |                |
  |                 |                        |                    |  - best candidate  |                |
  |                 |                        |<-------------------| payload            |                |
  |                 |<-----------------------| JSON response       |                    |                |
  |<----------------|                        |                    |                    |                |
```

## 2. Per-URL Extraction Decision Flow

```text
_extract_best_content(url)
    |
    +--> log source_attempted(mode=http)
    +--> HTTP extract
    |      |
    |      +--> success & score >= http_fast_path_threshold => return immediately
    |      +--> else candidate kept or source_failed logged
    |
    +--> mode == cli ?
           |
           +--> try CLI extract
           |      |
           |      +--> success & score >= crawler_fast_path_threshold => return
           |      +--> else candidate kept
           |      +--> on fail: log source_failed(cli)
           |
           +--> if use_library_fallback: try library extract
                   |
                   +--> success => candidate kept
                   +--> fail => log source_failed(library)

    +--> if mode == library: try library first with same threshold logic
    +--> if candidates exist: return highest quality
    +--> else return empty
```

## 3. Event and Retention Flow

```text
run_query()
  |
  +--> maybe_prune_markdown_daily(24h)
  +--> maybe_prune_event_logs_daily(throttled)
  |
  +--> append query_started
  +--> append source_* events during extraction
  +--> append query_succeeded/query_failed
```

Retention defaults:
- query failures: 24h
- source failures: 7d
- success events: 7d
- markdown files (`result.md`): 24h

## 4. Dashboard Logs Sidecar Flow

```text
output/events/*.ndjson (shared volume)
            |
            v
dashboard_logs container
  command: python /app/main.py dashboard-logs ...
            |
            v
stdout dashboard snapshots (table or JSON) every N seconds
            |
            v
docker compose -f docker-compose.multi.yml logs -f dashboard_logs
```

## 5. LM Studio Local LLM Flow (Integration Path)

```text
External agent/orchestrator
  -> reads config/config.yaml
  -> OpenAI-compatible base URL (LM Studio): http://localhost:1234/v1
  -> model: mlx-community/qwen3.6-35b-a3b
  -> receives LLM response
```

Note: the `/search` runtime path in this repo does not depend on LLM inference.
