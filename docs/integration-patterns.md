# SearXNG + Crawl4AI: Integration Patterns & Design Decisions

## 1. Why This Architecture?

### The Problem Space

Building a production-grade web search system faces three fundamental challenges:

1. **Search API costs and limits** — Google/Bing/DuckDuckGo APIs cost money, have rate limits, and require API keys
2. **Content extraction complexity** — Search results only provide titles + snippets, not full content
3. **Anti-bot defenses** — Modern websites use JavaScript rendering, CAPTCHAs, and bot detection

### The Solution: Specialized Tool per Problem

| Layer | Tool | Why This Tool |
|-------|------|---------------|
| **Discovery** | SearXNG | Free, no API keys, 70+ engines aggregated, privacy-preserving |
| **Extraction** | Crawl4AI | Headless browser with JS rendering, anti-bot handling, markdown output |
| **Orchestration** | FastAPI + Python | Type-safe API, async concurrency, easy integration |

### Why Not Alternatives?

| Alternative | Why Not Used |
|-------------|--------------|
| Google Custom Search API | Requires API key, $5/1000 queries, rate limited |
| Bing Web Search API | Requires API key, $3-7/1000 queries |
| DuckDuckGo Instant Answer API | Limited to instant answers, no full URL results |
| Scrapy | No built-in JS rendering, more complex setup |
| Playwright/Puppeteer directly | No markdown extraction, no quality scoring, more boilerplate |
| Single Docker service for SearXNG + separate for crawler | Simpler deployment, lower latency (no network hop), fewer failure modes |

---

## 2. SearXNG Integration Details

### How SearXNG Works in This System

SearXNG is a **meta-search engine** — it doesn't have its own index. Instead, it queries 70+ search engines (Google, Bing, DuckDuckGo, Wikipedia, etc.) and aggregates results.

**In this system:**
- SearXNG runs **inside the same container** as the API (not a separate service)
- It's started via `granite searx.webapp:app` (WSGI interface)
- The API queries it via HTTP GET with JSON format
- Only the `general` category is used by default (can be configured)

### SearXNG Configuration (`searxng-settings.yml`)

```yaml
use_default_settings: true

general:
  debug: false
  instance_name: "Websearch SearXNG"

search:
  safe_search: 1          # 0=off, 1=moderate, 2=strict
  autocomplete: "duckduckgo"
  formats:
    - html
    - json                # JSON is what our API consumes

server:
  secret_key: "searxng-secret"
  limiter: false          # Disable rate limiting for API usage
  image_proxy: true       # Proxy images through SearXNG
  method: "GET"           # Use GET for search queries

valkey:
  url: false              # No caching layer (simpler deployment)
```

### Query Parameter Mapping

The system maps its own config to SearXNG's query parameters:

```python
# Our config                    →  SearXNG parameter    →  Purpose
search.max_results: 5          →  (implicit, client-side cap)
search.language: "en-US"       →  ?language=en-US
search.safesearch: 1           →  ?safesearch=1
search.categories: ["general"] →  ?categories=general
search.engines: ["google"]     →  ?engines=google
```

### Result Normalization Pipeline

SearXNG returns results in its own format. The system normalizes them:

```python
# SearXNG response field    →  Our normalized field
item["title"]              →  "title"
item["url"]                →  "url"
item["content"]            →  "snippet"   (preferred)
item["snippet"]            →  "snippet"   (fallback)
item["description"]        →  "snippet"   (final fallback)
```

Deduplication is done by URL to prevent the same page appearing from multiple engines.

---

## 3. Crawl4AI Integration Details

### How Crawl4AI Works in This System

Crawl4AI is an **open-source AI-ready web crawler** that uses a headless browser to:
- Render JavaScript-heavy pages
- Extract clean markdown from HTML
- Handle anti-bot measures (Cloudflare, CAPTCHAs)
- Support deep crawling (BFS/DFS traversal)

**In this system:** Crawl4AI is used in two ways:
1. **CLI mode** (`crwl` binary) — primary extraction method
2. **Library mode** (`AsyncWebCrawler`) — Python API fallback

### CLI Mode: `crwl` Binary

The `crwl` command-line tool is installed with Crawl4AI:

```bash
# Basic extraction
crwl https://example.com -o markdown

# Deep crawl (BFS traversal)
crwl https://example.com --deep-crawl bfs --max-pages 10 -o markdown

# Deep crawl (DFS traversal)
crwl https://example.com --deep-crawl dfs --max-pages 10 -o markdown
```

**Why CLI over library?**
- Lower overhead (no Python interpreter startup per call)
- Process isolation (crash in one crawl doesn't affect others)
- Easier timeout management (`asyncio.wait_for` on subprocess)

### Library Mode: `AsyncWebCrawler`

```python
from crawl4ai import AsyncWebCrawler

async with AsyncWebCrawler() as crawler:
    result = await crawler.arun(url=url)

# result.markdown     → str (extracted markdown)
# result.metadata     → dict (title, description, etc.)
# result.cleaned_html → str (cleaned HTML)
# result.success      → bool
# result.error_message→ str | None
```

**Retry strategy:** Exponential backoff with configurable max retries:
```python
for attempt in range(max_attempts):
    try:
        result = await crawler.arun(url=url)
        break
    except Exception:
        if attempt < max_attempts - 1:
            await asyncio.sleep(backoff * (attempt + 1))
```

### HTTP Fallback: `httpx`

When both Crawl4AI methods fail, a simple HTTP GET is attempted:

```python
async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
    response = await client.get(url, headers={"User-Agent": "Mozilla/5.0..."})

# Regex-based extraction:
title = re.search(r"<title>(.*?)</title>", html).group(1)
body = strip_html_tags(html)[:6000]
```

**Why include this?** Some simple sites don't need browser automation. The HTTP fallback is fast and reliable for static HTML pages, providing a performance win when Crawl4AI isn't needed.

---

## 4. The Triple-Mode Extraction Strategy

### Design Rationale

Every URL goes through a **cascading extraction pipeline**:

```
Mode 1: crwl CLI (primary, fastest for most pages)
    ↓ on failure
Mode 2: Crawl4AI library (robust, handles JS rendering)
    ↓ always runs in parallel
Mode 3: httpx fallback (fastest for static pages)

Then: Score all candidates, pick the best one
```

### Why Three Modes?

| Failure Scenario | Mode 1 (CLI) | Mode 2 (Library) | Mode 3 (HTTP) |
|-----------------|--------------|-------------------|---------------|
| Normal page | ✅ Works | — | — |
| JS-rendered page | ✅ Works (browser) | ✅ Works (browser) | ❌ No content |
| Cloudflare protected | ✅ May work | ✅ May work (browser) | ❌ Blocked |
| crwl binary missing | ❌ Fails | ✅ Works | — |
| Crawl4AI not installed | ❌ Fails | ❌ Fails | ✅ Works |
| Static HTML page | ✅ Works | ✅ Works | ✅ Fastest |
| Timeout during crawl | ❌ Killed | ❌ Timed out | ✅ May succeed |

### Quality-Based Selection

When multiple modes return content, the system picks the best one:

```python
best = max(candidates, key=lambda item: float(item[1].get("quality_score") or 0.0))
```

This means if the CLI returns a high-quality extraction AND the HTTP fallback returns something, the better one wins.

---

## 5. Content Quality Assessment System

### The Scoring Model

The quality assessment is a **heuristic scoring system** that evaluates extracted content on five dimensions:

```
                    Max Score
                    ┌────────────────────────────────────────┐
  Content Length    │██████████████████░░░░░░░░░░░░░░░░░░░░░░│ 0.35
                    │ (>=1500 chars)                          │
                    ├────────────────────────────────────────┤
  Paragraphs        │███████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░│ 0.25
                    │ (>=5 paragraphs)                        │
                    ├────────────────────────────────────────┤
  Link Density      │███████████████░░░░░░░░░░░░░░░░░░░░░░░░░│ 0.20
                    │ (<=0.08 ratio)                          │
                    ├────────────────────────────────────────┤
  Nav Noise         │███████████████░░░░░░░░░░░░░░░░░░░░░░░░░│ 0.20
                    │ (<=12% nav lines)                       │
                    ├────────────────────────────────────────┤
  Error Penalty     │░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░│ -0.60
                    │ (any error marker)                      │
                    └────────────────────────────────────────┘
                    Total: 1.0 (perfect) / -0.60 (error page)
```

### Why Heuristics Over LLM?

| Approach | Pros | Cons |
|----------|------|------|
| **Heuristics (current)** | Fast, deterministic, no API cost, works offline | Less nuanced, may miss context |
| **LLM scoring** | Contextual understanding, handles edge cases | Slow ($0.01-0.1 per page), requires API key, non-deterministic |

The current system uses heuristics because:
1. **Latency** — LLM scoring would add 2-5 seconds per page
2. **Cost** — At scale, LLM scoring becomes expensive
3. **Determinism** — Heuristics produce consistent results

### The Cleanup Prompt as Configuration

The `body_cleanup_prompt.j2` file is **not sent to an LLM** in the current implementation. Instead, its text is parsed to extract removal rules:

```python
# Prompt says: "Remove headers, navigation, footers, cookie banners"
# Parser extracts: ("headers", "navigation", "footers", "cookie_banners")
# These become boilerplate markers for line-level filtering
```

This is a clever design pattern: the prompt serves as **human-readable configuration** that's both machine-parseable and developer-editable.

---

## 6. Concurrency & Performance

### Parallel Extraction Architecture

```
Top K URLs to enrich (default: 2)
        |
        v
+------------------------+
| asyncio.gather()       |
+------------------------+
        |
   +----+----+
   |         |
Worker 1    Worker 2
(url_0)     (url_1)
   |         |
   v         v
[semaphore = 2]
```

**Configurable limits:**
- `extract_top_k`: How many URLs to enrich (default: 2) — limits total work
- `extract_max_workers`: Concurrent crawlers (default: 2) — limits concurrency
- `extract_timeout_seconds`: Per-crawl timeout (default: 45s) — prevents hangs

### Performance Characteristics

| Metric | Value | Notes |
|--------|-------|-------|
| SearXNG query latency | ~500-2000ms | Depends on engines configured |
| Single crawl (CLI) | ~1-5s | Depends on page complexity |
| Single crawl (library) | ~2-10s | Python overhead + browser startup |
| Single crawl (HTTP) | ~200-1000ms | Fastest for static pages |
| 2 URLs enriched (parallel) | ~3-10s total | Bounded by slowest worker |
| Total request latency | ~4-12s | SearXNG + enrichment |

### Timeout Strategy

| Operation | Timeout | Rationale |
|-----------|---------|-----------|
| SearXNG query | 20s (hardcoded) | Search should be fast; 20s is generous |
| Crawl4AI CLI | 60s (45+15) | Browser rendering can be slow |
| Crawl4AI library | 45s | Same as CLI but without extra buffer |
| HTTP fallback | 45s | Standard web request timeout |

---

## 7. Deployment Architecture

### Single-Container Design

```
┌──────────────────────────────────────────────────────┐
│  Container: websearch                                │
│                                                      │
│  ┌─────────────┐    ┌──────────────┐                │
│  │ SearXNG     │    │ FastAPI      │                │
│  │ :8080       │◄──►│ :9000        │                │
│  │ (granite)   │ HTTP │ (uvicorn)   │                │
│  └─────────────┘    └──────────────┘                │
│        ▲                    ▲                        │
│        │                    │                        │
│        │ internal           │ external               │
│        │ (localhost)        │ (published ports)      │
└────────┴────────────────────┴────────────────────────┘
```

**Why single container?**
- **No network hop** — SearXNG and API communicate via localhost (lower latency)
- **Simpler deployment** — One `docker compose up` instead of multi-service orchestration
- **Shared filesystem** — Output packages written to shared volume
- **Coordinated startup** — Entrypoint waits for SearXNG before starting API

### Why Not Separate Containers?

| Factor | Single Container | Separate Containers |
|--------|-----------------|---------------------|
| Deployment complexity | 1 service | 2+ services (SearXNG, API, possibly DB) |
| Network latency | ~0.1ms (localhost) | ~1-5ms (Docker network) |
| Failure surface | Process crash | Network + service discovery |
| Resource overhead | Shared Python runtime | Separate processes |
| Configuration | Single config file | Multiple configs + service discovery |

---

## 8. Security Considerations

### Current Security Posture

| Concern | Status | Details |
|---------|--------|---------|
| **SearXNG exposure** | Internal only | Port 8080 not published by default (only 9000 is) |
| **Rate limiting** | Disabled | `limiter: false` in settings — intentional for API usage |
| **User-Agent** | Configurable | Default: Chrome 125 on macOS |
| **URL validation** | None explicit | Any URL from SearXNG is crawled — potential SSRF risk |
| **Timeouts** | Configured | Prevents resource exhaustion from slow pages |

### Potential Improvements

1. **URL allowlist/denylist** — Restrict which domains can be crawled
2. **SearXNG rate limiting** — Enable `limiter: true` for public deployments
3. **Request signing** — Authenticate API requests with tokens
4. **Crawl delay** — Add configurable delay between requests for politeness

---

## 9. Extensibility Points

### Adding New Search Engines

Edit `searxng-settings.yml` or configure via API:
```yaml
# Enable specific engines
search:
  engines: ["google", "bing", "wikipedia"]
```

### Adding New Crawler Backends

The `_extract_best_content()` function is the extension point:
```python
async def _extract_with_new_crawler(url: str) -> str:
    # Implement new crawler
    pass

# Add to candidates list in _extract_best_content()
```

### Adding New Quality Dimensions

Extend `assess_content_quality()` in `cleanup.py`:
```python
# Add new dimension scoring:
if some_condition:
    score += 0.10
    reasons.append("new_quality_dimension")
```

### Switching to LLM-Based Cleanup

The prompt template is already in place. To use it:
1. Add LLM endpoint to config (`llm_endpoint`)
2. Call LLM with the prompt + extracted content
3. Use returned `keep_ranges` for precise extraction
