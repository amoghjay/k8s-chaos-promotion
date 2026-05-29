# k8s-chaos-promotion

![GKE](https://img.shields.io/badge/GKE-Standard-4285F4?logo=googlecloud&logoColor=white)
![Terraform](https://img.shields.io/badge/Terraform-1.6+-7B42BC?logo=terraform&logoColor=white)
![ArgoCD](https://img.shields.io/badge/ArgoCD-App--of--Apps-EF7B4D?logo=argo&logoColor=white)
![Kargo](https://img.shields.io/badge/Kargo-v1.9-00ADD8?logoColor=white)
![Chaos Mesh](https://img.shields.io/badge/Chaos_Mesh-CNCF-FF6B6B?logoColor=white)
![k6](https://img.shields.io/badge/k6-Load_Testing-7D64FF?logo=k6&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Radius](https://img.shields.io/badge/Radius-Testnet-000000?logoColor=white)

> Promotion that earns trust, not just passes tests.

A Kubernetes platform where every staging→production promotion is **automatically gated by live chaos engineering**. If the running workload can't survive engineered failure — pod kills, network latency, payment-path degradation — the gate stays closed. No human approval can override it; the metrics either hold under chaos or they don't.

Built on **GKE + Terraform + ArgoCD + Kargo + Chaos Mesh**, with the test workload running **x402** — the HTTP-native payment standard for agentic APIs — settling micropayments on Radius testnet on every request. The application is intentionally minimal (a paid URL shortener); the platform underneath it is the deliverable.

---

## Why this exists

Most CD pipelines answer *"did the unit tests pass?"* This one answers *"did the system survive reality?"* Chaos isn't a separate exercise you run on Tuesdays — it's the same gate your code passes through on its way to users.

---

## How it works

```
git push
  └─▶ GitHub Actions builds + pushes image to GAR (keyless OIDC)
        └─▶ Kargo detects new tag
              └─▶ DEV  (auto-promote)
                    └─▶ STAGING  (manual promote → chaos gate runs)
                          └─▶ PROD  (manual approve)
```

Chaos Mesh injects faults into staging while a k6 load generator runs continuous payment traffic. Kargo only opens the gate to prod if Prometheus metrics stay within thresholds throughout.

---

## Stack

| Layer | Tool |
|-------|------|
| Cloud | GKE Standard (GCP) |
| IaC | Terraform |
| GitOps | ArgoCD (App-of-Apps) |
| Promotion | Kargo v1.9 |
| Chaos | Chaos Mesh |
| Observability | kube-prometheus-stack + Loki + Grafana |
| Load testing | k6 |
| Secrets | External Secrets Operator → GCP Secret Manager |
| CI | GitHub Actions (keyless OIDC — no stored credentials) |
| App | FastAPI + Redis + Postgres |
| On-chain settlement | Radius testnet — SBC (ERC-20, 6 decimals) via web3.py |

---

## Status

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | FastAPI app — x402-style payment flow on Radius testnet | ✅ Done |
| 2 | Terraform — GKE, GAR, IAM, Workload Identity | ✅ Done |
| 3 | Helm chart — multi-env overlays, ESO secrets | ✅ Done |
| 4 | ArgoCD App-of-Apps + Kargo promotion pipeline | ✅ Done |
| 5 | Observability — Prometheus, Loki, Grafana + k6 load generator | ✅ Done |
| 5.5 | Signer-backed real-payment loadgen on Radius testnet | ✅ Done |
| 6 | Chaos Mesh experiments | 🔄 In Progress |
| 7 | Chaos as Kargo verification gate | 🔜 |
| 8 | Demo script + write-up | 🔜 |

Current focus after Phase 5.5: bootstrap Chaos Mesh into staging, add repeatable fault experiments against the live signer-backed traffic path, then promote those checks into the staging verification gate before prod.

---

## Payment flow

Every `POST /shorten` requires an on-chain SBC payment:

```
1. Client calls POST /shorten  →  app returns HTTP 402
   { pay_to: "0xSERVICE...", amount: 1000, token: "SBC", chain_id: 72344 }

2. Client sends SBC transfer on Radius testnet  →  gets tx_hash

3. Client retries POST /shorten with tx_hash
   →  app verifies Transfer event via eth_getTransactionReceipt
   →  HTTP 201, short code returned
```

No Radius SDK — uses standard EVM JSON-RPC via web3.py. Works on any EVM chain with a single RPC URL change.

---

## Local development

```bash
git clone https://github.com/amoghjay/k8s-chaos-promotion.git
cd k8s-chaos-promotion
docker compose up --build
```

App at **http://localhost:8000** · Swagger UI at **http://localhost:8000/docs**

```bash
# Shorten a URL (payment disabled locally)
curl -s -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://chaos-mesh.org"}' | jq .

# Health check
curl http://localhost:8000/health | jq .
```

---

## Repository layout

```
app/                        FastAPI application
gke_terraform/              GCP infrastructure (Terraform)
helm/
  url-shortener/            Helm chart — dev / staging / prod value overlays
  observability/            kube-prometheus-stack + Loki + Grafana
kubernetes/
  bootstrap/                ArgoCD App-of-Apps (platform tools)
  kargo/                    Kargo Stages, Warehouse, AnalysisTemplates
  jobs/                     Kustomize package for k6 jobs + radius-signer
signer/                     FastAPI signer service for real SBC transfer submission
scripts/
  fund-test-wallet.sh       Faucet utility — drip SBC, verify on-chain balance
  test-payment-flow.sh      Manual end-to-end payment verification
kubernetes/jobs/scripts/
  loadgen.js                k6 chaos load generator source
  radius-tps-bench.js       k6 Radius TPS benchmark source
.github/workflows/
  build-push.yaml           CI — build → sign → push to GAR (keyless OIDC)
```

---

## Radius Integration

This project uses Radius testnet as the payment settlement layer. Every `POST /shorten` in production requires a real on-chain SBC transfer — verified via `eth_getTransactionReceipt` before the URL is shortened.

**Testnet config used:**

| Parameter | Value |
|-----------|-------|
| Chain ID | `72344` |
| RPC | `https://rpc.testnet.radiustech.xyz` |
| SBC contract | `0x33ad9e4BD16B69B5BFdED37D8B5D9fF9aba014Fb` |
| SBC decimals | `6` |
| Fee per shorten | `0.001 SBC` (1000 raw units) |
| Faucet | `https://testnet.radiustech.xyz/api/v1/faucet` |

**Real gotchas hit during development:**

- **Null receipt lag** — `eth_getTransactionReceipt` returns null for confirmed transactions due to RPC node lag. Added a 200ms retry before surfacing `TX_NOT_FOUND` to the user.
- **Receipt visibility lag tolerance** — the verifier now retries receipt fetches for a short window before returning `tx_not_found`, and `/shorten` surfaces that state as transient so the load generator can retry the same `tx_hash`.
- **`cast call` output format** — `balanceOf` returns `"500000 [5e5]"` — decimal, not hex. Parsed with `awk '{print $1}'`, never `int(..., 16)`.
- **Faucet rate limit** — this was the Phase 5 bottleneck for the old `/drip`-based loadgen. Phase 5.5 moved the chaos path to signer-backed real ERC-20 transfers so the load generator measures the payment flow instead of faucet policy.
- **`signature_required` degradation** — If the faucet re-enables signed mode, the bash faucet script exits cleanly with the web faucet URL. k6 VUs flip to no-payment mode permanently for that run. No private key for `SERVICE_WALLET` is ever stored in scripts.
- **Signer-backed loadgen** — The current Phase 5.5 load generator uses one funded wallet per VU through an internal `radius-signer` service, submits real SBC ERC-20 transfers on Radius testnet, waits for confirmation, then submits the resulting `tx_hash` to `/shorten`.
- **Wallet pre-run guard** — the load generator now checks signer wallet balances in `setup()` and fails fast if the configured VU wallets do not have enough SBC to cover the planned run.
- **Current staging maturity** — The signer-backed path is now mostly healthy in staging: tx submit and receipt success are ~99.7%, shorten 201 success is ~94%, redirect success is 100%, and the main residual issue is verifier-side `tx_not_found` timing.

**Phase 5 baseline — real traffic against Radius testnet:**

243 k6 iterations over 3 minutes, 2 VUs, ~132 on-chain transactions settled:

| Metric | Result |
|--------|--------|
| Redirect success | 100% |
| Payment replay (409) | 0% |
| Payment success rate | 82% (rate-limited near end of run) |
| RPC latency p(95) | 515ms |

The load generator itself is a light stress test of the Radius RPC — `eth_getTransactionReceipt` called ~132 times over 3 minutes from inside a GKE pod.

---

## Background

During my co-op at Radius I built the payment settlement infrastructure — RPC endpoints, on-chain transaction flows, service accounts. This project builds on top of that work; the chaos-as-promotion-gate mechanic is the platform-engineering layer on top.
