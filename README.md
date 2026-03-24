# k8s-chaos-promotion

> A Kargo promotion pipeline on GKE where staging **can't promote to production** unless the app survives chaos testing — pod kills, node drains, and network latency — with resilience measured quantitatively on a Grafana dashboard.

---

## Why I built this

During my co-op at Radius I built the payment settlement infrastructure — the RPC endpoints, the on-chain transaction flow, the service accounts. This project builds on top of that work to answer a question that matters in production: **how do you know a deployment is actually safe before it reaches users?**

The standard answer is "run tests in staging." But staging tests don't tell you what happens when a pod gets killed mid-transaction, when network latency spikes during RPC calls, or when a node drains while a payment is in-flight. For a pay-per-use service where every shortened URL costs a real on-chain transaction, a bad deployment doesn't just mean downtime — it means lost payments and broken user trust.

So I'm building a promotion pipeline where **chaos is the gate, not an afterthought**. Staging can only promote to production if the app survives Chaos Mesh fault injection — pod kills, network latency, simulated RPC failures — and two metrics stay above threshold throughout: HTTP error rate and on-chain payment success rate. Kargo orchestrates the multi-stage promotion, Argo Rollouts AnalysisTemplates enforce the quantitative gate, and Grafana makes the resilience story visible at a glance.

The goal is a system where the path from a git push to a production deployment is fully automated, observable, and provably resilient — not just "it worked in staging."

---

## Architecture

```
GitHub Actions (CI)
  └─ Build → Sign (Cosign) → Push to GAR (keyless OIDC — no stored secrets)
       │
       ▼
  Kargo Warehouse  ──watches──▶  new image tags in GAR
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
| **1** | URL shortener app (FastAPI + Redis + Postgres) | ✅ Done |
| **2** | Terraform — GKE cluster, GAR, IAM, Workload Identity | ✅ Done |
| **3** | Helm chart — multi-env values, ESO-ready secrets | ✅ Done |
| **4** | CI update + ArgoCD App-of-Apps + Kargo promotion pipeline | 🔄 In Progress |
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

## Phase 2 — Terraform (GKE + GCP Infrastructure)

All cloud infrastructure is managed as code in `gke_terraform/`.

### What gets provisioned

| Resource | Details |
|----------|---------|
| GKE Cluster | Zonal (`us-central1-a`), Workload Identity enabled |
| Default node pool | 2x `e2-medium`, auto-repair/upgrade on |
| Chaos node pool | Spot `e2-medium`, autoscaling 0→1, tainted `chaos-workload=true` |
| VPC + Subnet | Dedicated network with secondary ranges for pods/services |
| Artifact Registry | `us-central1-docker.pkg.dev/amoghdevops/k8s-chaos-demo` |
| Workload Identity Pool | Scoped to this GitHub repo — no static credentials |
| GitHub Actions SA | `artifactregistry.writer` — pushes images from CI |
| ESO SA | `secretmanager.secretAccessor` — reads secrets from GCP Secret Manager |
| GKE Node SA | Least-privilege: logging + monitoring + `artifactregistry.reader` |

### Cost optimisation

Scale nodes to zero between sessions — only the free-tier control plane runs:

```bash
# End of session
gcloud container clusters resize chaos-promotion \
  --node-pool default-pool --num-nodes 0 \
  --zone us-central1-a -q

# Start of session
gcloud container clusters resize chaos-promotion \
  --node-pool default-pool --num-nodes 2 \
  --zone us-central1-a -q
```

---

## Phase 3 — Helm Chart

The app is packaged as a Helm chart in `helm/url-shortener/` with per-environment value overlays.

### Value overlays

| File | Environment | Key differences |
|------|-------------|-----------------|
| `values.yaml` | Base defaults | All environments inherit this |
| `values-dev.yaml` | Dev | 1 replica, lighter resources, PDB off |
| `values-staging.yaml` | Staging | 2 replicas, PDB on, anti-affinity |
| `values-staging-fragile.yaml` | Staging (chaos demo) | 1 replica, 2s RPC timeout, no persistence |
| `values-prod.yaml` | Prod | 2 replicas, 4 workers, 5Gi DB |

### Templates

| Template | K8s Resource | Purpose |
|----------|-------------|---------|
| `deployment.yaml` | Deployment | Runs the FastAPI app pods |
| `service.yaml` | Service | ClusterIP routing to pods |
| `configmap.yaml` | ConfigMap | Non-secret env vars |
| `secret.yaml` | Secret | Populated by ESO from GCP Secret Manager |
| `serviceaccount.yaml` | ServiceAccount | Pod identity for Workload Identity |
| `pdb.yaml` | PodDisruptionBudget | Ensures min 1 pod up during node drains |

### Dry run

```bash
helm lint ./helm/url-shortener \
  -f ./helm/url-shortener/values-dev.yaml \
  --set postgresql.auth.password=testpassword \
  --set radius.rpcUrl="http://fake" \
  --set radius.serviceWalletAddress="0x000"
```

---

## Phase 4 — CI + GitOps (In Progress)

### CI — GitHub Actions

Every push to `main` triggers `build-push.yaml`:

1. **Authenticate** to GCP via Workload Identity Federation (keyless — no stored secrets)
2. **Build** — multi-platform (`linux/amd64` + `linux/arm64`) via Docker Buildx
3. **Push** to Google Artifact Registry
4. **Sign** with Cosign (keyless, OIDC-based)
5. **Smoke test** — pulls the pushed image, verifies `/health` returns 200

No GitHub secrets required — authentication is handled entirely via OIDC token exchange.

### GitOps — App-of-Apps (ArgoCD + Kargo)

Two tools, two different responsibilities:

**ArgoCD** — keeps the cluster in sync with Git (platform tools):
```
kubernetes/bootstrap/root-app.yaml   ← single entry point
  ├── eso.yaml           → External Secrets Operator
  ├── observability.yaml → Prometheus + Grafana + Loki
  └── chaos-mesh.yaml    → Chaos Mesh
```

**Kargo** — promotes image tags across environments (app):
```
GAR new tag detected
  └─▶ values-dev.yaml updated → ArgoCD deploys to dev
        └─▶ health check passes → values-staging.yaml updated → ArgoCD deploys to staging
              └─▶ chaos gate passes → values-prod.yaml updated → ArgoCD deploys to prod
```

---

## Repository Structure

```
k8s-chaos-promotion/
├── app/
│   ├── main.py                        ← FastAPI app
│   ├── requirements.txt
│   └── Dockerfile
├── gke_terraform/                     ← Phase 2: GCP infrastructure
│   ├── main.tf
│   ├── variables.tf
│   ├── gke.tf
│   ├── registry.tf
│   ├── github-oidc.tf
│   ├── eso.tf
│   └── iam.tf
├── helm/                              ← Phase 3: Helm chart
│   └── url-shortener/
│       ├── Chart.yaml
│       ├── values.yaml
│       ├── values-dev.yaml
│       ├── values-staging.yaml
│       ├── values-staging-fragile.yaml
│       ├── values-prod.yaml
│       └── templates/
├── kubernetes/                        ← Phase 4: ArgoCD App-of-Apps (coming)
│   └── bootstrap/
├── docker-compose.yml                 ← Local dev
├── .github/
│   └── workflows/
│       └── build-push.yaml           ← CI: build → push to GAR (keyless OIDC)
└── README.md
```

---

## Quick Start (local)

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or any Docker + Compose v2 setup)

### Run

```bash
git clone https://github.com/amoghjay/k8s-chaos-promotion.git
cd k8s-chaos-promotion

# Build and start all three services (app + Redis + Postgres)
docker compose up --build
```

The app will be available at **http://localhost:8000** once all health checks pass (~20s).

### Try it

```bash
# 1. Shorten a URL
curl -s -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://chaos-mesh.org/docs"}' | jq .

# 2. Follow the redirect
curl -v http://localhost:8000/aB3kR9mZ

# 3. Check health
curl http://localhost:8000/health | jq .
# {"status": "ok", "postgres": "ok", "redis": "ok"}

# 4. Prometheus metrics
curl http://localhost:8000/metrics | grep url_shortener
```

### Interactive API docs

FastAPI auto-generates Swagger UI: **http://localhost:8000/docs**

---

## Image

```
us-central1-docker.pkg.dev/amoghdevops/k8s-chaos-demo/url-shortener:<tag>
```

Tags follow semver: `v1.0.0`, `1.0`, `1`, `latest`, `sha-<short>`.

---

## Tech Stack

| Component | Choice | Why |
|-----------|--------|-----|
| API framework | FastAPI + Uvicorn | Async-native; auto-generates OpenAPI docs |
| Cache | Redis 7 | Fast in-memory cache; kill it to test Postgres fallback |
| Database | Postgres 16 | Source of truth; kill it to test error handling |
| Infra-as-Code | Terraform | GKE, GAR, IAM, Workload Identity |
| Packaging | Helm | Multi-env value overlays; Bitnami Redis + Postgres subcharts |
| GitOps | ArgoCD | App-of-Apps for platform tools |
| Promotion | Kargo | Image tag promotion with verification gates |
| Secrets | External Secrets Operator + GCP Secret Manager | No plaintext secrets in Git |
| Observability | kube-prometheus-stack + Loki | Metrics + logs for chaos analysis |
| Chaos | Chaos Mesh | Pod kill, network latency, node drain experiments |
| Image signing | Cosign (keyless) | Supply chain security via Sigstore |
