# Websearch (API-Only, JSON-Only)

Single Docker service with:
- Local SearXNG process (internal only)
- Local Crawl4AI installation (`crwl` CLI) with optional MCP crawler mode
- One HTTP API endpoint: `POST /search`
- Always JSON response (optional markdown/json package file paths in JSON)

## Structure
- `main.py`: thin FastAPI app + CLI wrapper
- `schema/`: request/response models
- `utils/`: config, cleanup, crawling pipeline, packaging
- `prompt/body_cleanup_prompt.j2`: cleanup prompt used by cleanup logic
- `config/config.yaml`: websearch runtime config
- `config/searxng-settings.yml`: SearXNG runtime config
- `config/.env.example`: compose env defaults

## Start
```bash
cd /Users/ryan_chua/Desktop/websearch
cp config/.env.example .env
python -c "import secrets; print(f'SEARXNG_SECRET_KEY={secrets.token_hex(32)}')" >> .env
docker compose up -d --build
```

## Start load-balanced setup
```bash
docker compose -f docker-compose.multi.yml up -d --build --scale websearch=3 --remove-orphans
```
This exposes only `localhost:9000` through nginx and round-robins across the scaled `websearch` containers.

## Multi-container commands
Start or recreate with 3 websearch containers:
```bash
docker compose -f docker-compose.multi.yml up -d --build --scale websearch=3 --remove-orphans
```

Increase to 5 websearch containers:
```bash
./scripts/scale_websearch.sh 5
```

Increase to 8 websearch containers for agentic/deep-research bursts:
```bash
./scripts/scale_websearch.sh 8
```

Decrease to 2 websearch containers:
```bash
./scripts/scale_websearch.sh 2
```

Decrease to 1 websearch container:
```bash
./scripts/scale_websearch.sh 1
```

Run the load-balancer UAT:
```bash
python scripts/uat_lb_live_test.py
```

Check running containers:
```bash
docker compose -f docker-compose.multi.yml ps
```

Watch dashboard logs:
```bash
docker compose -f docker-compose.multi.yml logs -f dashboard_logs
```

Equivalent raw Compose commands for manual scaling:
```bash
docker compose -f docker-compose.multi.yml up -d --scale websearch=5 websearch
docker compose -f docker-compose.multi.yml up -d --no-deps --force-recreate nginx
```

The nginx recreate step makes nginx re-resolve the current set of `websearch` replica IPs.

Recommended container counts:
- Light use: 3 containers
- Agentic use: 5-8 containers
- Deep research: 8-10 containers
- Heavy local max: 10-12 containers, only if CPU/RAM stay healthy

SearXNG timeout and retry tuning lives in `config/config.yaml`:
```yaml
search:
  searxng_timeout_seconds: 30.0
  searxng_max_retries: 2
  searxng_retry_backoff_seconds: 1.0
```

Search admission control also lives in `config/config.yaml`:
```yaml
server:
  max_concurrent_requests: 8
  queue_timeout_seconds: 2.0
  request_timeout_seconds: 120.0
```

Crawler mode selection (onboarding-friendly MCP option):
```yaml
crawler:
  mode: "mcp"   # cli | library | mcp
  mcp_endpoint: "http://127.0.0.1:8001/mcp/call"
  mcp_tool_name: "crawl_url"
  mcp_timeout_seconds: 45.0
  use_library_fallback: true
```

API:
- `http://localhost:9000/health`
- `http://localhost:9000/search`

## Secret scanning hooks
Install pre-commit and enable hooks:
```bash
pip install pre-commit
pre-commit install
```

Run a full secret scan before pushing:
```bash
pre-commit run --all-files
```

## Tests
Run local tests:
```bash
pytest -q
```

Live endpoint tests are skipped by default. Enable them against a running endpoint:
```bash
WEBSEARCH_LIVE_TEST_URL=http://localhost:9000 pytest -q -m live
```

## API example
```bash
curl -sS -X POST http://localhost:9000/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "latest autonomous shipping regulations",
    "write_markdown_package": true
  }'
```

Disable package output files per request:
```bash
curl -sS -X POST http://localhost:9000/search \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "latest autonomous shipping regulations",
    "write_markdown_package": false
  }'
```

## CLI example
CLI is an API caller wrapper around `/search`:
```bash
python main.py search --query "latest autonomous shipping regulations" --write-markdown-package
```

Docker logs-friendly dashboard snapshots (JSON lines):
```bash
python main.py dashboard-logs --output-dir /app/output --interval-seconds 10 --window-seconds 86400 --limit 10
```
In multi-instance compose:
```bash
docker compose -f docker-compose.multi.yml logs -f dashboard_logs
```

## Output Retention
- Markdown package files (`output/**/result.md`) are pruned automatically once per 24 hours on request traffic.
- JSON files are kept.
- Event logs are retained by policy (query failures 24h, source failures 7d by default).
- Manual prune command:
```bash
python scripts/prune_output_markdown.py --older-than-hours 24
```

## DeerFlow backend integration
Use API-only tool config (no extract/language/engines keys in backend tool config):
```yaml
tools:
  - name: web_search
    group: web
    use: src.community.websearch.tools:web_search_tool
    api_base_url: http://localhost:9000
    api_path: /search
```
