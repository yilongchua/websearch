# SearXNG + Crawl4AI Websearch: Comprehensive Architecture Analysis

## 1. System Overview

This project is a **unified web search API** that combines two open-source components into a single Docker container:

| Component | Role | Port |
|-----------|------|------|
| **SearXNG** | Meta-search engine (queries 70+ engines, returns results) | 8080 |
| **Crawl4AI** | Web crawler/extractor (fetches & cleans page content) | N/A (CLI + library) |
| **FastAPI** | API gateway that orchestrates both, exposes `POST /search` | 9000 |

The system implements a **two-phase retrieval pipeline**:
1. **Discovery** (SearXNG) — find relevant URLs for a query
2. **Extraction & Enrichment** (Crawl4AI) — fetch, clean, and score content from the top results

---

## 2. High-Level Data Flow

```
Client Request (query)
        |
        v
  +----------+
  | FastAPI  |  POST /search
  | (main.py)|
  +----------+
        |
        v
  +------------------+
  |   run_query()    |  utils/pipeline.py:215
  +------------------+
        |
        +-----> _query_searxng() --------> SearXNG /search (JSON)
        |         |                           |
        |         v                           v
        |    [URL, title, snippet]     HTTP GET with params
        |                                    |
        v                                    v
  +------------------+              [raw results]
  | _enrich_results()|
  +------------------+
        |
        v (top_k URLs, parallel)
  +---------------------+
  | _extract_best_content|
  | () per URL          |
  +---------------------+
        |
        +----> _crawl_with_crwl_cli()   --> subprocess: crwl <url> -o markdown
        |         |
        |         v (on failure)
        +----> _crawl_with_library()    --> crawl4ai.AsyncWebCrawler.arun(url)
        |         |
        |         v (always, parallel fallback)
        +----> _http_fallback_extract() --> httpx.AsyncClient.get(url)
        |
        v (all candidates scored)
  +------------------+
  | assess_content_  |  utils/cleanup.py:132
  | quality()        |  returns {quality_score, cleaned_text, reasons}
  +------------------+
        |
        v (best candidate selected)
  [extracted_content attached to result]
        |
        v
  +------------------+
  | write_package()  |  utils/packaging.py:40
  +------------------+
        |
        v
  JSON Response (+ optional .json/.md files)
```

---

## 3. Component Deep-Dive

### 3.1 SearXNG Integration (`pipeline.py:142-192`)

**How it works:**
- SearXNG is **installed locally inside the container** (Dockerfile:34), not run as a separate service
- The entrypoint starts SearXNG via `granite searx.webapp:app` (entrypoint.sh:20)
- The API queries SearXNG's JSON endpoint at `/search?q=<query>&format=json`

**Query parameters passed to SearXNG:**
| Parameter | Config Key | Default | Purpose |
|-----------|------------|---------|---------|
| `q` | — | (from request) | Search query |
| `format` | — | `json` | Response format |
| `language` | `search.language` | `en-US` | Language filter |
| `safesearch` | `search.safesearch` | `1` | Safe search level (0/1/2) |
| `categories` | `search.categories` | `[]` | SearXNG categories (e.g. "general", "images") |
| `engines` | `search.engines` | `[]` | Specific engines (e.g. "google", "bing") |

**Result normalization:**
- Deduplicates by URL using a `seen` set
- Extracts `title`, `url`, and `snippet` (tries `content`, `snippet`, then `description`)
- Caps at `search.max_results` (default: 5)

**Key insight:** SearXNG provides the **discovery layer** — it aggregates results from 70+ search engines without API keys, rate limits, or CAPTCHAs. The system only uses SearXNG for URL discovery, not for content extraction.

---

### 3.2 Crawl4AI Integration (`pipeline.py:22-83`)

Crawl4AI is used in **three modes** with a cascading fallback strategy:

#### Mode 1: CLI (`crwl` binary) — Primary
```bash
crwl <url> -o markdown [--deep-crawl bfs|dfs --max-pages N]
```
- Invoked via `asyncio.create_subprocess_exec` (pipeline.py:28)
- Outputs markdown directly
- Supports **deep crawling** (BFS/DFS) for multi-page extraction
- Timeout: `extract_timeout_seconds + 15` (default: 60s)
- Output capped at `extract_max_chars` (default: 6,000)

#### Mode 2: Python Library — Fallback
```python
async with AsyncWebCrawler() as crawler:
    result = await crawler.arun(url=url)
```
- Uses Crawl4AI's native async Python API (pipeline.py:51-82)
- Automatic retry with exponential backoff (`extract_max_retries`, default: 1)
- Extracts title from metadata, falls back to cleaned HTML if no markdown

#### Mode 3: HTTP Fallback — Last Resort
```python
async with httpx.AsyncClient() as client:
    response = await client.get(url, headers={"User-Agent": ...})
```
- Simple `httpx` GET request (pipeline.py:85-104)
- Regex-based title extraction and HTML-to-text conversion
- Always runs in parallel as a safety net

**Fallback orchestration (`_extract_best_content`, pipeline.py:107-139):**
```
mode == "cli":
  try crwl CLI → on failure, try library (if enabled)
mode == "library":
  try library directly

# Always runs in parallel:
try http fallback → always included if it returns content

# Selection:
best = max(candidates, key=quality_score)
```

**Key insight:** The triple-mode approach ensures **maximum reliability**. Even if both Crawl4AI methods fail (e.g., JavaScript-rendered pages, anti-bot protection), the raw HTTP fallback can still extract basic content.

---

### 3.3 Content Quality Assessment (`cleanup.py:132-205`)

Every extracted content candidate is scored on a **0.0–1.0 scale** using five weighted dimensions:

| Dimension | Weight | Criteria |
|-----------|--------|----------|
| **Content Length** | 0.35 | >=1500 chars = full score; >=700 = partial; <700 = penalty |
| **Paragraph Structure** | 0.25 | >=5 paragraphs = full; >=3 = partial; <3 = none |
| **Link Density** | 0.20 | <=0.08 = full; <=0.18 = partial; >0.55 = penalty |
| **Navigation Noise** | 0.20 | <=12% nav lines = full; <=25% = partial; >25% = none |
| **Error Detection** | -0.60 | Any error marker (403, captcha, cloudflare, etc.) = heavy penalty |

**Error markers detected:** `technical difficulties`, `forbidden`, `captcha`, `cloudflare`, `access denied`, `service unavailable`, `error 403/404/500`

**Boilerplate markers stripped:** `skip to content`, `all rights reserved`, `privacy policy`, `cookie policy`, `sign in`, `subscribe`, social media links

**Scoring formula:**
```python
score = length_score + paragraph_score + link_density_score + nav_noise_score - error_penalty
score = max(0.0, min(1.0, score))  # Clamped to [0, 1]
```

**Cleanup process (`clean_extracted_content`, pipeline.py:84-129):**
1. Normalize line endings
2. Strip markdown links to plain text
3. Filter lines by multiple heuristics:
   - Lines matching boilerplate markers (if <220 chars)
   - Bullet list items starting with `* [` or `- [`
   - Pure separator lines (`|`, `-`)
   - Link-heavy lines (>=3 URLs, <120 alpha chars)
   - Pipe/separator rows with many tokens but no punctuation
   - Ultra-short lines (<=2 tokens, not headings)
4. Collapse triple+ newlines to double
5. Strip remaining HTML if >30 `<` and `>` characters
6. Cap at `cleanup.max_chars` (default: 20,000)

---

### 3.4 Configuration System (`config.py`)

**Three-layer configuration with deep merge:**
1. **Defaults** — hardcoded in `DEFAULT_CONFIG` (config.py:10-48)
2. **File override** — `config/config.yaml` (YAML format, deep-merged)
3. **Environment** — `WEBSEARCH_CONFIG_PATH` env var to point to alternate config

**Config key path format:** `section.subsection.key` (e.g., `crawler.mode`)

**Key configuration sections:**
```yaml
server:          # FastAPI server settings
  api_host: "0.0.0.0"
  api_port: 9000

service:         # Service integration settings
  searxng_base_url: "http://127.0.0.1:8080"
  output_dir: "/app/output"
  user_agent: "Mozilla/5.0 ..."

search:          # Search behavior settings
  max_results: 5           # URLs to discover via SearXNG
  extract_top_k: 2         # Top K URLs to enrich with full content
  extract_max_chars: 6000  # Max chars per extraction
  extract_timeout_seconds: 45.0
  extract_max_retries: 1
  extract_retry_backoff_seconds: 1.5
  extract_max_workers: 2   # Parallel extraction workers (asyncio.Semaphore)

crawler:           # Crawler mode settings
  mode: "cli"            # "cli" or "library"
  crwl_binary: "crwl"    # Path to crwl binary
  deep_crawl: "none"     # "none", "bfs", or "dfs"
  deep_max_pages: 10     # Max pages for deep crawl
  use_library_fallback: true
  http_fallback_enabled: true

cleanup:           # Content cleanup settings
  prompt_path: "/app/prompt/body_cleanup_prompt.j2"
  max_chars: 20000
```

---

## 4. API Surface

### Endpoints

#### `GET /health`
```json
{
  "ok": "true",
  "service": "websearch",
  "crawler_mode": "cli"
}
```

#### `POST /search`
**Request body:**
```json
{
  "query": "quantum computing breakthroughs 2025",
  "write_markdown_package": true,
  "package_name": null
}
```

**Response:**
```json
{
  "query": "quantum computing breakthroughs 2025",
  "generated_at": "2025-01-15T10:30:00+00:00",
  "total_results": 5,
  "results": [
    {
      "title": "Quantum Breakthrough...",
      "url": "https://example.com/article",
      "snippet": "Researchers achieved...",
      "extracted_content": "# Quantum Breakthrough...\n\nFull article text...",
      "content_quality_score": 0.85,
      "extracted_content_quality": {
        "quality_score": 0.85,
        "quality_reasons": ["sufficient_content_length", "strong_paragraph_structure"],
        "cleaned_text": "...",
        "chars": 3200,
        "paragraph_count": 8,
        "link_density": 0.05,
        "nav_line_ratio": 0.08,
        "error_like": false
      }
    }
  ],
  "package": {
    "dir": "/app/output/20250115T103000Z_quantum-computing",
    "json_path": "/app/output/20250115T103000Z_quantum-computing/result.json",
    "markdown_path": "/app/output/20250115T103000Z_quantum-computing/result.md"
  }
}
```

---

## 5. Docker Architecture

### Container Layout

The entire stack runs in **a single container**:

```
┌─────────────────────────────────────────────┐
│  python:3.12-slim-bookworm                  │
│                                             │
│  /opt/searxng/          ← SearXNG (venv)    │
│  /opt/searxng-venv/     ← SearXNG Python    │
│  /app/                  ← Websearch API      │
│                                             │
│  Port 8080: SearXNG (granite WSGI)          │
│  Port 9000: FastAPI (uvicorn)               │
└─────────────────────────────────────────────┘
```

### Entrypoint Orchestration (`entrypoint.sh`)

1. Starts SearXNG as background process via `granite searx.webapp:app`
2. Polls SearXNG health endpoint (30 attempts, 1s interval)
3. Starts FastAPI server as background process
4. `wait -n` blocks until either process exits (with cleanup trap)

### Build Arguments

| Arg | Default | Purpose |
|-----|---------|---------|
| `SEARXNG_GIT_REF` | `master` | SearXNG git branch/tag to clone |
| `CRAWL4AI_PRE_RELEASE` | `false` | Use `pip install crawl4ai --pre` if true |

### Volumes

| Mount | Purpose |
|-------|---------|
| `./output:/app/output` | Persist search result packages |
| `./config/config.yaml:/app/config/config.yaml:ro` | Runtime config |
| `./config/searxng-settings.yml:/etc/searxng/settings.yml:ro` | SearXNG settings |
| `./prompt:/app/prompt:ro` | Cleanup prompt templates |

---

## 6. Concurrency Model

### Parallel Extraction (`_enrich_results`, pipeline.py:195-212)

```python
sem = asyncio.Semaphore(extract_max_workers)  # default: 2

async def worker(index):
    async with sem:
        extracted, quality = await _extract_best_content(url)

await asyncio.gather(*(worker(i) for i in range(top_k)))
```

- Uses `asyncio.Semaphore` to limit concurrent crawlers to `extract_max_workers` (default: 2)
- All top_k URLs are processed in parallel (bounded by semaphore)
- Each URL triggers the triple-mode extraction with internal fallbacks

### HTTP Client Timeouts

| Operation | Timeout |
|-----------|---------|
| SearXNG query | 20s (hardcoded) |
| Crawl4AI extraction | `extract_timeout_seconds` + 15s (CLI) or hardcoded (library) |
| HTTP fallback | `extract_timeout_seconds` |

---

## 7. Output Packaging (`packaging.py`)

Each search generates a timestamped directory:

```
output/
  └── 20250115T103000Z_quantum-computing/
      ├── result.json    ← Full structured response
      └── result.md      ← Human-readable markdown summary
```

**Markdown rendering format:**
```markdown
# Websearch Results: quantum computing breakthroughs 2025

Generated at: 2025-01-15T10:30:00+00:00

## 1. Quantum Breakthrough in Error Correction
- URL: https://example.com/article
- Snippet: Researchers demonstrated...
- Quality score: 0.85

# Quantum Breakthrough in Error Correction

Full extracted markdown content here...
```

**Naming convention:** `{timestamp}_{slugified_query}` where slug is lowercase, alphanumeric-only with hyphens.

---

## 8. SearXNG + Crawl4AI Integration Pattern: Key Design Decisions

### Why This Combination?

| Problem | SearXNG Solves | Crawl4AI Solves |
|---------|---------------|-----------------|
| Search without API keys | ✅ 70+ engines, no key needed | — |
| Anti-bot protection | ❌ Returns URLs only | ✅ Headless browser, JS rendering |
| Content extraction | ❌ Only snippets | ✅ Full page → clean markdown |
| Rate limiting | ✅ Aggregates, no per-engine limits | ✅ Retry + fallback strategy |
| CAPTCHA handling | ✅ Some engines bypass it | ✅ Browser automation handles some |

### Architectural Pattern: **Discover → Extract → Score → Select**

1. **SearXNG discovers** — finds URLs without needing individual search engine API keys
2. **Crawl4AI extracts** — fetches full page content with browser automation (handles JS rendering, anti-bot)
3. **Quality assessment scores** — evaluates extracted content on multiple dimensions
4. **Best candidate selected** — picks the highest-quality extraction per URL

### Why SearXNG Instead of Direct Search APIs?

- **No API keys required** — SearXNG aggregates free search engine results
- **No rate limits** — SearXNG handles the distribution across engines
- **Multi-engine aggregation** — results from Google, Bing, DuckDuckGo, Wikipedia, etc. combined
- **Privacy-focused** — no query logging, no tracking
- **Configurable** — can enable/disable specific engines and categories via `searxng-settings.yml`

### Why Crawl4AI Instead of Simple HTTP Fetching?

- **JavaScript rendering** — many modern sites require JS execution to display content
- **Anti-bot bypass** — headless browser with realistic browser fingerprinting
- **Clean output** — native markdown extraction, removes ads/navigation
- **Deep crawling** — BFS/DFS modes for multi-page article extraction
- **Multiple output formats** — markdown, HTML, images, links

---

## 9. Prompt Engineering (`body_cleanup_prompt.j2`)

The cleanup prompt defines rules for an LLM-based content cleaner:

```
- Remove headers, navigation, footers, cookie banners, social/share sections
- Keep title, publication date/byline, and substantive body content
- Do not summarize, rewrite, or add facts
- Return JSON with keep_ranges / remove_ranges
```

**Note:** The current implementation **parses the prompt text** to extract removal markers (pipeline.py:48-65) rather than calling an LLM. The prompt serves as a **configuration specification** — the `_extract_prompt_markers()` function parses "remove X, Y, Z" patterns to build the boilerplate detection list.

---

## 10. Deployment Options

### Docker Compose (Recommended)
```bash
cp .env.example .env
docker compose up -d --build
```

### CLI Mode (Local, no Docker)
```bash
pip install -r requirements.txt
# Ensure crwl binary is available on PATH
python main.py serve --host 0.0.0.0 --port 9000
```

### CLI Search (One-shot)
```bash
python main.py search --query "your query" --write-markdown-package
```

---

## 11. Testing Strategy (`tests/`)

| Test | What It Validates |
|------|-------------------|
| `test_schema_accepts_query` | Pydantic model accepts valid queries |
| `test_schema_rejects_empty_query` | Pydantic rejects empty strings (min_length=1) |
| `test_cleanup_quality_has_prompt_flag` | Quality assessment returns expected keys |
| `test_search_endpoint_returns_json` | `/search` returns valid JSON response structure |
| `test_search_endpoint_package_paths` | Package output includes correct file paths |

Uses FastAPI's `TestClient` with monkeypatching to mock `run_query`.

---

## 12. Integration with DeerFlow Backend

The README documents integration with a DeerFlow AI agent backend:

```yaml
tools:
  - name: web_search
    group: web
    use: src.community.websearch.tools:web_search_tool
    api_base_url: http://localhost:9000
    api_path: /search
```

This allows an AI agent to call the websearch API as a tool during reasoning chains.

---

## 13. Summary: Complete Request Lifecycle

```
1. Client sends POST /search with query "AI regulation"
2. FastAPI receives request, validates via SearchRequest schema
3. run_query() calls _query_searxng("AI regulation")
4. SearXNG queries 70+ engines, returns ~20 results
5. System normalizes to top 5 unique URLs (max_results)
6. _enrich_results() launches up to 2 parallel crawlers for top 2 URLs (extract_top_k)
7. For each URL:
   a. Try crwl CLI → if fails, try Crawl4AI library → always also try httpx fallback
   b. Score all candidates by quality assessment
   c. Select best extraction, attach to result
8. Write JSON + Markdown package to output/{timestamp}_{slug}/
9. Return structured response with results, quality scores, and package paths
```
