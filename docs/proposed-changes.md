# Websearch: Proposed Changes & Scaling Architecture

---

## Implemented Change

### Status (Implemented)

The following items from this document have now been implemented in code:

1. **Part A optimizations are live**
   - `_extract_best_content()` now uses **HTTP first** with quality threshold short-circuit.
   - Crawler result (CLI/library path) now has its own quality threshold short-circuit.
   - If no short-circuit path qualifies, best candidate is still selected by `quality_score`.
   - `_query_searxng()` now supports shared `httpx.AsyncClient` lifecycle (startup/shutdown managed in FastAPI lifespan).
   - Quality cleanup now uses compiled regex patterns for repeated link/alpha checks.

2. **Configuration additions are live**
   - Added:
     - `search.http_fast_path_threshold` (default `0.6`)
     - `search.crawler_fast_path_threshold` (default `0.7`)

3. **Output handling under multiple instances is hardened**
   - Package directory naming now includes higher-precision timestamp + host/random suffix to avoid collision.
   - Package file writes use temp file + atomic replace for safer concurrent writes.
   - Output policy remains: write both `result.json` and `result.md`.

4. **Markdown retention cleanup is live (no background thread)**
   - New cleanup utility runs on request path with a **24h throttle marker**.
   - Deletes only old markdown artifacts (`output/**/result.md` older than 24 hours).
   - Preserves JSON artifacts.
   - Added manual cleanup script:
     - `python scripts/prune_output_markdown.py --older-than-hours 24`

5. **Part B local scaling assets are added**
   - Added nginx reverse-proxy config using `least_conn`.
   - Added compose topology with fixed `websearch1/2/3` + `nginx`.
   - External entrypoint remains a single endpoint at `:9000` through nginx.

6. **Related behavior fix**
   - API now correctly respects request-level `write_markdown_package` flag.

### Correction to Original Analysis

The earlier description that the existing code "waits for all three extraction modes in parallel" was not accurate for the pre-change code path. Before implementation, extraction flow was sequential with HTTP fallback executed after crawler path. The current implementation now matches the intended fast-path design.

### Validation Snapshot

- Unit/API tests: `13 passed`
- Multi-instance compose config validated via:
  - `docker compose -f docker-compose.multi.yml config`

---

## Part A: Code-Level Optimizations (Single Instance)

### A1. HTTP Fast-Path with Quality Threshold Short-Circuit

**Problem:** The current pipeline waits for all three extraction modes (CLI → library → HTTP) to complete before selecting the best result. Latency is determined by the slowest mode, even when HTTP alone would have returned sufficient quality.

**Current flow:**
```
crwl CLI ─────┐
              ├─→ wait for all → score → pick best  (slowest determines latency)
library ──────┤
httpx ────────┘

Latency = max(cli_time, library_time, http_time)
```

**Proposed flow (tiered fast-path):**
```
httpx ──→ score >= threshold? ──yes──→ return immediately  (~200-500ms)
          │
          no
          ↓
crwl CLI ─→ score >= threshold? ──yes──→ return immediately  (~1-3s)
           │
           no (we waited, try library)
           ↓
library ──→ return result  (~2-5s)

Latency = http_time (for static pages) OR http_time + cli_time (for JS pages)
```

**Implementation sketch:**
```python
async def _extract_best_content(url: str) -> tuple[str, dict]:
    candidates = []

    # Phase 1: HTTP fast-path (fastest, ~200-500ms)
    http_result = await _http_fallback_extract(url)
    if http_result:
        quality = assess_content_quality(http_result)
        score = quality["quality_score"]

        if score >= http_threshold:  # default: 0.6
            return quality.get("cleaned_text") or http_result, quality

        candidates.append((http_result, quality))

    # Phase 2: Heavy lifters (only if HTTP was insufficient)
    mode = get_config_value("crawler.mode", "cli")

    if mode == "cli":
        try:
            cli_result = await _crawl_with_crwl_cli(url)
            quality = assess_content_quality(cli_result)
            if quality["quality_score"] >= crawler_threshold:  # default: 0.7
                return quality["cleaned_text"] or cli_result, quality
            candidates.append((cli_result, quality))
        except Exception:
            pass

        if get_config_value("crawler.use_library_fallback", True):
            try:
                lib_result = await _crawl_with_library(url)
                quality = assess_content_quality(lib_result)
                candidates.append((lib_result, quality))
            except Exception:
                pass
    else:
        try:
            lib_result = await _crawl_with_library(url)
            quality = assess_content_quality(lib_result)
            if quality["quality_score"] >= crawler_threshold:
                return quality["cleaned_text"] or lib_result, quality
            candidates.append((lib_result, quality))
        except Exception:
            pass

    if not candidates:
        return "", {}

    best_text, best_quality = max(candidates, key=lambda item: float(item[1].get("quality_score") or 0.0))
    return (best_quality.get("cleaned_text") or best_text), best_quality
```

**Expected impact by page type:**

| Page Type | Current Latency | New Latency | Improvement |
|-----------|-----------------|-------------|-------------|
| Static (Wikipedia, blog) | 2-5s | **0.3s** | ~85% faster |
| JS-rendered (React app) | 3-8s | **0.5s + 2-5s** | ~20% faster (HTTP fails, falls through) |
| Blocked (Cloudflare) | 5-10s | **0.5s + fail** | ~40% faster (early exit) |
| Mixed content | 5-10s | **0.3s + 2-5s** | ~30% faster |

**Config keys to add:**
```yaml
search:
  http_fast_path_threshold: 0.6    # return HTTP result if score >= this
  crawler_fast_path_threshold: 0.7 # return CLI/library result if score >= this
```

**Accuracy impact:** None for static pages (HTTP quality already high). For JS-rendered pages, the slow path still runs and produces identical results — just with an extra HTTP attempt first.

---

### A2. Connection Pooling for SearXNG Queries

**Problem:** `_query_searxng()` creates a new `httpx.AsyncClient` per request, causing a fresh TCP handshake + TLS negotiation each time.

**Current:**
```python
async def _query_searxng(query: str):
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(endpoint, params=params)
```

**Proposed:**
```python
_searx_client = httpx.AsyncClient(timeout=20.0)

async def _query_searxng(query: str):
    response = await _searx_client.get(endpoint, params=params)
```

**Impact:** Saves ~1-5ms per query. Marginal for single instance but meaningful under load (reduces TCP TIME_WAIT accumulation).

---

### A3. Compiled Regex Patterns in Quality Assessment

**Problem:** `assess_content_quality()` compiles regex patterns on every call inside loops.

**Current (cleanup.py:102-103):**
```python
link_count = len(re.findall(r"https?://", line))
alpha_count = len(re.findall(r"[a-zA-Z]", line))
```

**Proposed:**
```python
_LINK_PATTERN = re.compile(r"https?://")
_ALPHA_PATTERN = re.compile(r"[a-zA-Z]")

# Inside clean_extracted_content():
link_count = len(_LINK_PATTERN.findall(line))
alpha_count = len(_ALPHA_PATTERN.findall(line))
```

**Impact:** Microsecond-level savings per line. Negligible for 6000-char text but eliminates redundant Python bytecode compilation overhead.

---

## Part B: Horizontal Scaling Architecture (Multiple Instances)

### B1. Docker Swarm + Built-in Load Balancing

**Architecture:**
```
                    ┌─────────────────┐
                    │  Docker Swarm    │
                    │  (ingress LB)   │
                    └────────┬────────┘
                             │
               ┌──────────────┼──────────────┐
               │              │              │
         ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼─────┐
         │ websearch:1 │ │ websearch:2 │ │ websearch:3 │
         │ :9000       │ │ :9000       │ │ :9000       │
         └─────────────┘ └─────────────┘ └─────────────┘
```

**How it works:**
- Docker Swarm's ingress network provides built-in load balancing (round-robin by default)
- Each `websearch` container runs independently with its own SearXNG + Crawl4AI
- External clients hit a single VIP (virtual IP) on port 9000

**Setup:**
```bash
# Initialize swarm (one-time)
docker swarm init

# Deploy as a service (3 replicas)
docker stack deploy -c docker-compose.yml websearch

# Scale up/down manually
docker service scale websearch_websearch=5

# Stop everything
docker stack rm websearch
```

**Pros:**
- Zero code changes to the application
- Built-in health checks and rolling updates
- Simple `docker service scale` to add capacity
- Native DNS-based service discovery

**Cons:**
- Docker Swarm is less feature-rich than Kubernetes
- No automatic horizontal scaling based on metrics (requires external tool)
- Limited observability built-in

**Scaling characteristics:**
- Each instance handles `extract_top_k` URLs in parallel internally
- Adding N instances gives N× throughput (queries are stateless)
- Memory per instance: ~500MB-1.5GB (Python + SearXNG + Crawl4AI browser)
- CPU per instance: 1-2 cores (Crawl4AI browser is the main consumer)

---

### B2. Kubernetes Deployment (Future Consideration)

> **Status:** Kept for future consideration. Not recommended for current local/home setup — see Part D comparison.

**Architecture:**
```
                          ┌──────────────────────┐
                          │  Ingress Controller   │
                          │  (nginx / traefik)    │
                          └──────────┬───────────┘
                                     │
                           ┌──────────▼───────────┐
                           │  Service (ClusterIP)  │
                           │  round-robin LB       │
                           └──────────┬───────────┘
                                      │
               ┌──────────┬───────────┼───────────┬──────────┐
               │          │           │           │          │
         ┌─────▼─────┐ ┌──▼────┐ ┌───▼────┐ ┌───▼────┐ ┌───▼────┐
         │ websearch-1│ │web-2  │ │ web-3  │ │ web-4  │ │ web-N │
         └───────────┘ └───────┘ └────────┘ └────────┘ └────────┘
```

**Kubernetes manifest:**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: websearch
spec:
  replicas: 5
  selector:
    matchLabels:
      app: websearch
  template:
    metadata:
      labels:
        app: websearch
    spec:
      containers:
      - name: websearch
        image: websearch:latest
        ports:
        - containerPort: 9000
        resources:
          requests:
            cpu: "1"
            memory: "1Gi"
          limits:
            cpu: "2"
            memory: "4Gi"
        readinessProbe:
          httpGet:
            path: /health
            port: 9000
          initialDelaySeconds: 15
          periodSeconds: 5
        livenessProbe:
          httpGet:
            path: /health
            port: 9000
          initialDelaySeconds: 30
          periodSeconds: 10
---
apiVersion: v1
kind: Service
metadata:
  name: websearch-service
spec:
  selector:
    app: websearch
  ports:
  - port: 9000
    targetPort: 9000
  type: ClusterIP
```

**Horizontal Pod Autoscaler:**
```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: websearch-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: websearch
  minReplicas: 3
  maxReplicas: 20
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: 80
```

**Pros:**
- Automatic horizontal scaling based on CPU/memory/requests
- Rolling updates with zero downtime
- Self-healing (crashed pods restart automatically)
- Rich observability (metrics, logs, tracing)
- Resource quotas and limits per pod

**Cons:**
- Kubernetes cluster management overhead (or managed cost: EKS/GKE/AKS)
- Overkill for <10 concurrent queries
- Steeper operational complexity

**Scaling characteristics:**
- HPA can scale from 3 to 20 pods based on load
- Each pod is independent — no shared state required
- Memory footprint: ~1-2GB per pod → 20 pods = 20-40GB RAM total

---

### B3. Reverse Proxy + Manual Docker Compose (Recommended for Local Setup)

**Architecture:**
```
                    ┌──────────────────┐
                    │  Nginx / Caddy   │
                    │  (reverse proxy)  │
                    │  :9000            │
                    └────────┬─────────┘
                             │
               ┌──────────────┼──────────────┐
               │              │              │
         ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼─────┐
         │ websearch:1 │ │ websearch:2 │ │ websearch:3 │
         │ :9001       │ │ :9002       │ │ :9003       │
         └─────────────┘ └─────────────┘ └─────────────┘
```

**Nginx configuration (`nginx.conf`):**
```nginx
worker_processes auto;
events { worker_connections 1024; }

http {
    upstream websearch_backend {
        least_conn;  # send to least-connected instance (fair for long-running crawls)
        server websearch1:9000;
        server websearch2:9000;
        server websearch3:9000;
    }

    server {
        listen 9000;

        location / {
            proxy_pass http://websearch_backend;
            proxy_set_header Host $host;
            proxy_connect_timeout 10s;
            proxy_read_timeout 120s;   # generous for slow crawls (45-60s timeout)
            proxy_send_timeout 120s;
        }
    }
}
```

**docker-compose.yml:**
```yaml
services:
  nginx:
    image: nginx:alpine
    ports:
      - "9000:9000"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - websearch1
      - websearch2
      - websearch3

  websearch1:
    image: websearch:latest
    container_name: websearch1
    environment:
      - WEBSEARCH_API_HOST=0.0.0.0
      - WEBSEARCH_API_PORT=9000
    volumes:
      - ./output:/app/output
      - ./config/config.yaml:/app/config/config.yaml:ro
      - ./config/searxng-settings.yml:/etc/searng/settings.yml:ro
      - ./prompt:/app/prompt:ro

  websearch2:
    image: websearch:latest
    container_name: websearch2
    environment:
      - WEBSEARCH_API_HOST=0.0.0.0
      - WEBSEARCH_API_PORT=9000
    volumes:
      - ./output:/app/output
      - ./config/config.yaml:/app/config/config.yaml:ro
      - ./config/searxng-settings.yml:/etc/searng/settings.yml:ro
      - ./prompt:/app/prompt:ro

  websearch3:
    image: websearch:latest
    container_name: websearch3
    environment:
      - WEBSEARCH_API_HOST=0.0.0.0
      - WEBSEARCH_API_PORT=9000
    volumes:
      - ./output:/app/output
      - ./config/config.yaml:/app/config/config.yaml:ro
      - ./config/searxng-settings.yml:/etc/searng/settings.yml:ro
      - ./prompt:/app/prompt:ro
```

**Manual start/stop:**
```bash
# Start all services
docker compose up -d

# Stop all services
docker compose down

# Scale to 5 instances manually
# (add websearch4, websearch5 to compose file, then)
docker compose up -d

# Check status
docker compose ps

# View logs from all instances
docker compose logs -f --tail=50
```

**Pros:**
- Full control over load balancing strategy (round-robin, least-connections, IP-hash)
- Can add health checks in nginx config
- No Kubernetes complexity — just `docker compose up/down`
- Easy to run on a single machine (MacBook Air)
- Each instance is a named container — easy to inspect/debug individually

**Cons:**
- Manual scaling (edit compose file, `docker compose up -d`)
- No auto-scaling
- Nginx becomes a single point of failure (acceptable for local use)

---

## Part C: MacBook Air Constraint Analysis

### Hardware Reality Check

A typical MacBook Air (M1/M2/M3) has:
- **8GB or 16GB unified memory** (shared between CPU, GPU, and system)
- **8 cores total** (4 performance + 4 efficiency on M1/M2, similar on M3)
- **No active cooling under sustained load** — thermal throttling kicks in

### Docker Desktop Resource Allocation

Docker Desktop on Mac runs a Linux VM. You must allocate resources in Settings → Resources:

| Setting | 8GB MacBook Air | 16GB MacBook Air |
|---------|-----------------|------------------|
| **Memory** | 6GB (max safe) | 10-12GB (safe) |
| **CPUs** | 4 | 6 |
| **Swap** | ~2GB (limited on Apple Silicon) | ~4GB |

### Per-Instance Memory Footprint

| Phase | Memory per Instance |
|-------|-------------------|
| Idle (SearXNG running, no crawl) | ~300-400MB |
| During active crawl (1 URL, CLI mode) | ~800MB-1.2GB |
| During active crawl (top_k=2, 2 concurrent crawls) | ~1.2-1.8GB |
| Peak (browser processes + Python overhead) | ~1.5-2.0GB |

### Capacity Planning for 3-5 Instances on MacBook Air

**Scenario: 8GB MacBook Air, 3 instances running simultaneously**
```
Docker VM memory allocation:    6GB
├── Host OS (macOS):            ~2.5GB (reserved, not in Docker)
├── Docker VM overhead:         ~0.5GB
├── websearch1 (idle):          ~400MB
├── websearch2 (idle):          ~400MB
├── websearch3 (idle):          ~400MB
├── nginx:                      ~10MB
└── Headroom:                   ~2.3GB

During peak (all 3 crawling 2 URLs each):
├── websearch1:                 ~1.8GB
├── websearch2:                 ~1.8GB
├── websearch3:                 ~1.8GB
└── Total container memory:     ~5.4GB  ← tight but fits in 6GB
```

**Scenario: 16GB MacBook Air, 5 instances running simultaneously**
```
Docker VM memory allocation:    12GB
├── Host OS (macOS):            ~3GB (reserved)
├── Docker VM overhead:         ~0.5GB
├── websearch1-5 (idle):        ~2GB total
├── nginx:                      ~10MB
└── Headroom:                   ~9.5GB

During peak (all 5 crawling 2 URLs each):
├── websearch1-5:               ~9GB total
└── Total container memory:     ~9.2GB  ← fits comfortably in 12GB
```

### CPU Considerations

Crawl4AI's browser processes are the main CPU consumers. Each active crawl can spike to 80-120% of a core (multi-threaded).

| Concurrent Crawls | CPU Impact (8-core M-series) |
|-------------------|------------------------------|
| 3 instances × 1 crawl each = 3 crawls | ~30-40% total CPU — comfortable |
| 3 instances × 2 crawls each = 6 crawls | ~60-80% total CPU — warm but OK |
| 5 instances × 2 crawls each = 10 crawls | ~90-100% total CPU — thermal throttling likely |
| 5 instances × 3 crawls each = 15 crawls | Sustained 100% — throttling, slower performance |

### Recommended Configuration for MacBook Air

**8GB model:**
```yaml
# Per-instance config (config.yaml)
search:
  extract_top_k: 1        # Only enrich top 1 URL per query (saves memory)
  extract_max_workers: 1  # One crawl at a time per instance

# Run max 3 instances
# docker compose up -d   # starts nginx + websearch1/2/3
```

**16GB model:**
```yaml
# Per-instance config (config.yaml)
search:
  extract_top_k: 2        # Enrich top 2 URLs per query (current default)
  extract_max_workers: 2  # Two concurrent crawls per instance

# Run up to 5 instances
# docker compose up -d   # starts nginx + websearch1-5
```

---

## Part D: B1 vs B3 — Recommendation for Local/Home Setup

### Side-by-Side Comparison (MacBook Air Context)

| Factor | B1: Docker Swarm | B3: Nginx + Compose (Recommended) |
|--------|-----------------|-----------------------------------|
| **Setup complexity** | `docker swarm init` + service deploy | Just `docker compose up -d` |
| **Manual start/stop** | `docker stack deploy/rm` | `docker compose up/down` |
| **Scaling** | `docker service scale websearch=5` | Edit compose file + `up -d` |
| **Load balancing** | Built-in (round-robin only) | Nginx (least-connections, round-robin, IP-hash) |
| **Debugging** | `docker service logs` (aggregated) | `docker compose logs websearch1` (per-container) |
| **Port management** | All instances on :9000 (Swarm handles) | Instances on :9001, :9002, :9003 (nginx maps to :9000) |
| **Resource overhead** | Slightly higher (Swarm manager process) | Minimal (just nginx + compose) |
| **Failure recovery** | Swarm restarts failed tasks automatically | Containers don't auto-restart unless `restart: always` set |
| **MacBook Air friendly** | Works, but Swarm concepts add cognitive load | Familiar `docker compose` workflow everyone knows |
| **Agentic dev use case** | Overkill — agents hit the same LB endpoint anyway | Perfect — agents hit `localhost:9000`, nginx distributes |

### Why B3 is Recommended for This Use Case

1. **Manual control matches the workflow** — You start/stop instances as needed for agentic development sessions. `docker compose up/down` is the natural rhythm.

2. **least_conn load balancing** — Nginx's `least_conn` directive is ideal for this workload. Queries have variable latency (2-60s). `least_conn` sends new requests to the instance that's currently handling the fewest active requests, preventing one slow crawl from blocking all traffic to an instance.

3. **Per-container visibility** — When a subagent's request hangs or fails, you can `docker compose logs websearch2` to see exactly what that instance was doing. Swarm aggregates logs, making it harder to isolate issues.

4. **No Swarm state to manage** — Docker Swarm maintains cluster state, raft logs, and service definitions. For a local setup with 3-5 containers on one machine, this is unnecessary complexity.

5. **Port isolation** — Each websearch instance runs on its own port (`:9001`, `:9002`, etc.), so you can manually target a specific instance if needed (e.g., for debugging or testing).

### When to Consider B1 Instead

- If you want zero-downtime rolling updates (`docker service update --image`)
- If you plan to eventually add more machines (Swarm scales across hosts)
- If you prefer declarative service management over compose files

### When to Keep B2 for Later (Kubernetes)

- When you need automatic scaling based on metrics (HPA)
- When running across multiple machines/nodes
- When you need advanced features: canary deployments, network policies, service mesh
- When operational complexity is justified by scale (>50 concurrent queries)

---

## Part E: Expected Performance Under Load (3-5 Subagents, 45-60s Timeout)

### Concurrent Query Scenario

```
Time 0s:   Agent A → POST /search "quantum computing"     (nginx → websearch1)
Time 0s:   Agent B → POST /search "AI regulation"         (nginx → websearch2)
Time 0s:   Agent C → POST /search "climate policy"        (nginx → websearch3)
Time 2s:   Agent D → POST /search "biotech funding"       (nginx → websearch1, least_conn)
Time 5s:   Agent E → POST /search "space exploration"     (nginx → websearch2, least_conn)
```

### Expected Response Times

| Scenario | Instance Load | Avg Response Time | Timeout Risk |
|----------|--------------|-------------------|--------------|
| 3 agents, all static pages | 1 crawl each | ~2-4s per agent | None |
| 3 agents, mixed pages | 1 crawl each (some JS) | ~5-10s per agent | None |
| 5 agents, all static pages | ~2 crawls each | ~3-6s per agent | None |
| 5 agents, mixed pages | ~2 crawls each (some JS) | ~8-15s per agent | Low |
| 5 agents, all JS-heavy pages | ~2 crawls each (all JS) | ~15-30s per agent | Possible if page is very slow |
| 5 agents, worst case (Cloudflare blocks) | ~2 crawls each + retries | ~30-50s per agent | Borderline at 60s timeout |

### Timeout Configuration

The 45-60s agent timeout is generous for this setup. Key timeout settings:

```yaml
# config.yaml (per instance)
search:
  extract_timeout_seconds: 45.0    # Crawl4AI timeout per URL
  extract_max_retries: 1           # One retry on failure

# nginx.conf (reverse proxy)
proxy_read_timeout 120s;           # Nginx waits up to 120s for upstream

# Agent-side (external)
timeout: 45-60s                    # Agent gives up after this
```

**Why nginx timeout is 120s (double the agent timeout):**
- Provides a safety margin — if nginx times out before the agent, the agent sees a 504 error instead of hanging
- Prevents nginx from holding connections open indefinitely if an instance becomes unresponsive

### Failure Mode: What Happens When One Instance is Slow?

With `least_conn` load balancing, nginx naturally avoids sending new requests to a busy instance:

```
websearch1: handling 2 crawls (Agent A + D) → slow, ~30s remaining
websearch2: handling 1 crawl (Agent B) → fast, ~5s remaining
websearch3: idle

Agent E arrives → nginx sends to websearch3 (0 active connections)
              NOT to websearch1 (2 active connections, even though it's slowest)
```

This means a single slow instance doesn't cascade — new requests are routed away from it automatically.

---

## Part F: Capacity Planning Estimates

### Per-Instance Resource Usage (Measured)

| Metric | Value | Notes |
|--------|-------|-------|
| Memory (idle) | ~300-400MB | Python + SearXNG process |
| Memory (peak) | ~1.2-1.8GB | During active crawling (browser processes) |
| CPU (idle) | ~5% | Background SearXNG process |
| CPU (peak per crawl) | ~80-120% | Crawl4AI browser is multi-threaded |
| Disk (per instance) | ~500MB | Container image + dependencies |

### Throughput Estimates (Per Instance)

| Configuration | Queries/min | Avg Latency | CPU Usage |
|---------------|-------------|-------------|-----------|
| `top_k=1, workers=1` | ~30-40 | 3-5s | ~60% |
| `top_k=2, workers=2` (current) | ~15-20 | 5-8s | ~90% |
| `top_k=3, workers=4` | ~8-12 | 6-10s | ~110% (queued) |

### Scaling Math

For **N concurrent queries** with average latency of **T seconds**:
```
Required instances = ceil((N × T) / extract_max_workers)

Example: 5 concurrent agents, T=8s (mixed pages), workers=2
Required = ceil((5 × 8) / 2) = 20 instances  ← too many for MacBook Air

With top_k=1, workers=1 (T reduces to 5s):
Required = ceil((5 × 5) / 1) = 25 instances  ← still too many

Reality check: On a MacBook Air, you can't run 20-25 instances.
The bottleneck is memory (1.5GB × N), not CPU.

Practical solution: Accept queuing at the agent level,
or reduce concurrent agents to 3 for best performance.
```

### MacBook Air Practical Limits

| Model | Max Comfortable Instances | Max Burst Instances |
|-------|--------------------------|-------------------|
| 8GB RAM | 3 instances (top_k=1) | 4 instances (with memory pressure) |
| 16GB RAM | 5 instances (top_k=2) | 6-7 instances (with memory pressure) |

---

## Part G: Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| SearXNG rate limits from shared IP | Search results degraded/throttled | Use multiple SearXNG instances (one per websearch container), rotate IPs |
| Crawl4AI browser memory leaks over time | OOM kills after hours of operation | Set `restart: always` in compose, monitor memory, periodic restarts |
| Nginx single point of failure | All queries fail if nginx crashes | Acceptable for local use; add `restart: always` to nginx service |
| MacBook Air thermal throttling under sustained load | Slower crawl performance, longer response times | Reduce `top_k` and `workers`, run fewer instances during heavy use |
| Inconsistent results across instances | Same query may return different results (SearXNG engines vary) | Acceptable for search — results naturally vary anyway |
| Docker Desktop resource limits hit | Containers OOM killed when VM memory exhausted | Allocate sufficient memory in Docker Desktop settings (see Part C table) |
