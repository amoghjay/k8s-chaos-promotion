# k8s-chaos-promotion

> A Kargo promotion pipeline on GKE where staging **can't promote to production** unless the app survives chaos testing — pod kills, node drains, and network latency — with resilience measured quantitatively on a Grafana dashboard.

---

## Architecture

```
GitHub Actions (CI)
  └─ Build → Sign (Cosign) → Push to Docker Hub (amoghjay1908/k8s-chaos-demo)
       │
       ▼
  Kargo Warehouse  ──watches──▶  new image tags
       │
       ▼
  DEV ──auto-promote──▶ STAGING ──chaos gate──▶ PROD
                            │
                       Chaos Mesh experiments
                       (pod kill / network latency / node drain)
                            │
                       Prometheus + Grafana
                       (error rate < 5% = gate passes)
```

---

## Project Phases

| Phase | What | Status |
|-------|------|--------|
| **1** | URL shortener app (FastAPI + Redis + Postgres) | ✅ |
| 2 | Terraform + GKE cluster | 🔜 |
| 3 | Helm chart | 🔜 |
| 4 | ArgoCD + Kargo promotion pipeline | 🔜 |
| 5 | Prometheus + Grafana dashboards | 🔜 |
| 6 | Chaos Mesh experiments | 🔜 |
| 7 | Chaos as Kargo verification gate | 🔜 |
| 8 | Polish, demo script, blog post | 🔜 |

---

## Phase 1 — URL Shortener App

### What it does

A minimal FastAPI service with two stateful dependencies — Redis (cache) and Postgres (source of truth). Designed to give chaos testing a **meaningful target** where killing each component produces distinct, observable failures.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/shorten` | POST | Accept a URL → store in Postgres → cache in Redis → return short code |
| `/payment-info` | GET | Returns payment requirements (wallet, token, chain, fee) |
| `/{code}` | GET | Redis hit? redirect. Miss? Postgres fallback + re-warm cache |
| `/health` | GET | Liveness/readiness probe — checks Redis + Postgres connectivity |
| `/metrics` | GET | Prometheus metrics (auto-exposed by instrumentator) |

### Why these endpoints matter for chaos

- `POST /shorten` writes to **both** Redis and Postgres — killing either produces measurable failures on the dashboard.
- `GET /{code}` shows **cache vs database behaviour** — killing Redis increases latency but shouldn't cause errors if the Postgres fallback works.
- `GET /health` gives Kubernetes liveness/readiness probes something meaningful to fail on.
- `GET /metrics` feeds Prometheus for the Grafana dashboards.

### Custom Prometheus metrics (on top of the instrumentator)

| Metric | Type | What it tells you during chaos |
|--------|------|-------------------------------|
| `url_shortener_cache_hits_total` | Counter | Drop here = Redis is down / experiment in progress |
| `url_shortener_cache_misses_total` | Counter | Spike here = Redis kill or network latency experiment |
| `url_shortener_urls_created_total` | Counter | Steady = writes succeeding; drop = Postgres under pressure |

---

## Quick Start (local)

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or any Docker + Compose v2 setup)

### Run

```bash
# Clone the repo
git clone https://github.com/amoghjay1908/k8s-chaos-promotion.git
cd k8s-chaos-promotion

# Build and start all three services (app + Redis + Postgres)
docker compose up --build
```

The app will be available at **http://localhost:8000** once all health checks pass (~20s).

### Payment mode toggle

- Payment is disabled by default in `docker-compose.yml` (`RADIUS_RPC_URL: ""`), so `/shorten` behaves like v2.
- To enable payment flow locally, set:
  - `RADIUS_RPC_URL`
  - `SERVICE_WALLET_ADDRESS`
- When payment is enabled:
  - `POST /shorten` without `tx_hash` returns `402`.
  - replayed `tx_hash` returns `409`.
  - shortening an already-known URL returns `200` with the existing short code.

### Try it

```bash
# 1. Shorten a URL
curl -s -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://chaos-mesh.org/docs"}' | jq .

# Output:
# {
#   "code": "aB3kR9mZ",
#   "short_url": "http://localhost:8000/aB3kR9mZ",
#   "original_url": "https://chaos-mesh.org/docs"
# }

# 2. Follow the redirect
curl -v http://localhost:8000/aB3kR9mZ
# → 302 Location: https://chaos-mesh.org/docs

# 3. Check health
curl http://localhost:8000/health | jq .
# {"status": "ok", "postgres": "ok", "redis": "ok"}

# 4. Payment info (always available)
curl http://localhost:8000/payment-info | jq .

# 5. Prometheus metrics
curl http://localhost:8000/metrics | grep url_shortener
```

### Simulate chaos manually (before the real Chaos Mesh integration)

```bash
# Kill Redis — watch /health return degraded and cache misses spike
docker compose stop redis
curl http://localhost:8000/health
# {"status": "degraded", "postgres": "ok", "redis": "error: ..."}
# GET /{code} still works — Postgres fallback kicks in

# Restore
docker compose start redis

# Kill Postgres — writes and reads (after cache TTL) will fail
docker compose stop postgres
curl -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
# → 503 Database unavailable

# Restore
docker compose start postgres
```

### Interactive API docs

FastAPI auto-generates Swagger UI: **http://localhost:8000/docs**

---

## Repository Structure

```
k8s-chaos-promotion/
├── app/
│   ├── main.py              ← FastAPI app
│   ├── requirements.txt
│   └── Dockerfile
├── docker-compose.yml       ← Local dev: app + Redis + Postgres
├── .github/
│   └── workflows/
│       └── build-push.yaml  ← CI: build → cosign → push to Docker Hub
└── README.md
```

*(Terraform, Helm, Kargo, Chaos Mesh, Grafana directories are added in Phases 2–7)*

---

## CI / CD

Every push to `main` and every semver tag (`v*.*.*`) triggers the GitHub Actions workflow:

1. **Build** — multi-platform (`linux/amd64` + `linux/arm64`) via Docker Buildx
2. **Push** to `amoghjay1908/k8s-chaos-demo` on Docker Hub
3. **Sign** with Cosign (keyless, OIDC-based — same approach as the supply-chain-security project)
4. **Smoke test** — pulls the pushed image and verifies `/health` returns 200

### Required GitHub Secrets

| Secret | Value |
|--------|-------|
| `DOCKERHUB_USERNAME` | `amoghjay1908` |
| `DOCKERHUB_TOKEN` | Docker Hub access token (not password) |

---

## Image

```
docker.io/amoghjay1908/k8s-chaos-demo:<tag>
```

Tags follow semver: `v1.0.0`, `1.0`, `1`, `latest`, `sha-<short>`.

---

## Tech Stack (Phase 1)

| Component | Choice | Why |
|-----------|--------|-----|
| API framework | FastAPI + Uvicorn | Async-native; auto-generates OpenAPI docs |
| Cache | Redis 7 | Fast in-memory cache; kill it to test Postgres fallback |
| Database | Postgres 16 | Source of truth; kill it to test error handling |
| Postgres client | asyncpg | High-performance async driver |
| Redis client | redis-py (redis.asyncio) | Official client with async support |
| Metrics | prometheus-fastapi-instrumentator | Zero-config request metrics; exposes `/metrics` |
| Image signing | Cosign (keyless) | Ties into supply-chain-security project's Kyverno policies |
