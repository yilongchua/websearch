# Websearch (API-Only, JSON-Only)

Multi docker instance:
Run it
Recreate stack so nginx config is applied:
- docker compose -f docker-compose.multi.yml up -d --build --scale websearch=3 --remove-orphans
Run UAT:
- python scripts/uat_lb_live_test.py

Single Docker service with:
- Local SearXNG process (internal only)
- Local Crawl4AI installation (`crwl` CLI first)
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
docker compose up -d --build
```

## Start (3-instance local load-balanced setup)
```bash
docker compose -f docker-compose.multi.yml up -d --build --scale websearch=3 --remove-orphans
```
This exposes only `localhost:9000` through nginx and round-robins across the scaled `websearch` containers.

Scale up or down:
```bash
./scripts/scale_websearch.sh 5
./scripts/scale_websearch.sh 2
```

Equivalent raw Compose commands:
```bash
docker compose -f docker-compose.multi.yml up -d --scale websearch=5 websearch
docker compose -f docker-compose.multi.yml up -d --no-deps --force-recreate nginx
```

The nginx recreate step makes nginx re-resolve the current set of `websearch` replica IPs.

API:
- `http://localhost:9000/health`
- `http://localhost:9000/search`

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
