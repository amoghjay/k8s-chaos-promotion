# k8s-chaos-promotion

![GKE](https://img.shields.io/badge/GKE-Standard-4285F4?logo=googlecloud&logoColor=white)
![Terraform](https://img.shields.io/badge/Terraform-1.6+-7B42BC?logo=terraform&logoColor=white)
![ArgoCD](https://img.shields.io/badge/ArgoCD-App--of--Apps-EF7B4D?logo=argo&logoColor=white)
![Kargo](https://img.shields.io/badge/Kargo-v1.9-00ADD8?logoColor=white)
![Chaos Mesh](https://img.shields.io/badge/Chaos_Mesh-CNCF-FF6B6B?logoColor=white)
![k6](https://img.shields.io/badge/k6-Load_Testing-7D64FF?logo=k6&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Radius](https://img.shields.io/badge/Radius-Testnet-000000?logoColor=white)

> **Staging can't promote to production unless the app survives chaos.**

A GitOps promotion pipeline on GKE where every staging → prod promotion is gated by live chaos experiments — pod kills, network latency, RPC failures — measured quantitatively on Grafana. The app is a pay-per-use URL shortener that settles micropayments on the **Radius testnet** (EVM, chain 72344) on every request.

Built to answer a question that matters in production: *how do you know a deployment is actually safe before it reaches users?*

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
| 6 | Chaos Mesh experiments | 🔄 In Progress |
| 7 | Chaos as Kargo verification gate | 🔜 |
| 8 | Demo script + write-up | 🔜 |

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
  jobs/                     k6 load generator Job + ConfigMap
scripts/
  fund-test-wallet.sh       Faucet utility — drip SBC, verify on-chain balance
  loadgen.js                k6 load generator source
  test-payment-flow.sh      Manual end-to-end payment verification
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
- **`cast call` output format** — `balanceOf` returns `"500000 [5e5]"` — decimal, not hex. Parsed with `awk '{print $1}'`, never `int(..., 16)`.
- **Faucet rate limit** — 60 req/60s per address. The k6 load generator at VUS=2 approaches this limit (~44 req/min). Handled gracefully — VUs fall back to no-payment mode and `payment_402_rate` spikes in Grafana as a measurable signal.
- **`signature_required` degradation** — If the faucet re-enables signed mode, the bash faucet script exits cleanly with the web faucet URL. k6 VUs flip to no-payment mode permanently for that run. No private key for `SERVICE_WALLET` is ever stored in scripts.
- **Address-only faucet flow** — The load generator calls `POST /drip` with `SERVICE_WALLET_ADDRESS` and uses the returned `tx_hash` directly as payment. No Ethereum signing required — the faucet sends SBC *to* the service wallet, so `verify_payment()` accepts it as a valid Transfer.

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

## Why this project

During my co-op at Radius I built the payment settlement infrastructure — RPC endpoints, on-chain transaction flows, service accounts. This project builds on top of that work. The chaos gate is the interesting part: a deployment that can't prove resilience under fault injection doesn't reach production. Chaos is the gate, not an afterthought.
