# k8s-chaos-promotion

![GKE](https://img.shields.io/badge/GKE-Standard-4285F4?logo=googlecloud&logoColor=white)
![Terraform](https://img.shields.io/badge/Terraform-1.6+-7B42BC?logo=terraform&logoColor=white)
![ArgoCD](https://img.shields.io/badge/ArgoCD-App--of--Apps-EF7B4D?logo=argo&logoColor=white)
![Kargo](https://img.shields.io/badge/Kargo-v1.9-00ADD8?logoColor=white)
![Chaos Mesh](https://img.shields.io/badge/Chaos_Mesh-CNCF-FF6B6B?logoColor=white)
![k6](https://img.shields.io/badge/k6-Load_Testing-7D64FF?logo=k6&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![x402](https://img.shields.io/badge/x402-v2-success?logoColor=white)
![Radius](https://img.shields.io/badge/Radius-Testnet-000000?logoColor=white)

> Promotion that earns trust, not just passes tests.

A Kubernetes platform where every staging → production promotion is **automatically gated by live chaos engineering**. If the running workload can't survive engineered failure — pod kills, network latency, payment-path degradation — the gate stays closed. No human approval can override it; the metrics either hold under chaos or they don't.

Built on **GKE + Terraform + ArgoCD + Kargo + Chaos Mesh**. The test workload is a paid API: every request settles a real micropayment via **x402** on the Radius testnet. The application is intentionally minimal — the platform underneath it, and how it gates promotions on observed reality, is the deliverable.

---

## Why this exists

Most CD pipelines answer *"did the unit tests pass?"* This one answers *"did the system survive reality?"* Chaos isn't a separate exercise you run on Tuesdays — it's the same gate your code passes through on its way to users.

---

## How it works

```
git push
  └─▶ GitHub Actions builds + pushes image to GAR (keyless OIDC)
        └─▶ Kargo Warehouse detects new sha-* tag
              └─▶ DEV       (auto-promote → /health analysis gate)
                    └─▶ STAGING  (manual promote → chaos gate runs)
                          └─▶ PROD     (manual approve, only after staging survives chaos)
```

In staging, Chaos Mesh injects faults — pod kills, NetworkChaos against external dependencies, Redis outages, latency injection — while a k6 load generator drives continuous paid traffic against the app. Kargo only opens the gate to prod if Prometheus metrics (`payment_facilitator_total{outcome="settled"}`, `shorten_201_rate`, `redirect_ok_rate`, settlement latency p95) stay within thresholds *throughout* the chaos window.

---

## What's worth a closer look

- **Chaos as the deploy gate, not a side experiment.** Same gate every change passes. No "we run chaos on Tuesdays" — the chaos run *is* the verification step Kargo blocks on.
- **Three services with non-overlapping concerns.** The workload is split into a **signer** (holds wallet keys, produces signatures off-chain), an **app** (holds payment policy, no blockchain client), and an external **facilitator** (does the on-chain settlement and pays gas). Each service has its own failure signature in Grafana, which is what makes the chaos experiments diagnostic instead of just stressful.
- **The app has zero blockchain code in it.** Migrated from a bespoke `tx_hash`-verification path to standard **x402 + Permit2** against the [Radius first-party facilitator](https://docs.radiustech.xyz/developer-resources/x402-integration). The app's payment module is two HTTPS calls; the `web3` dependency is gone entirely. End-to-end payment latency dropped ~2.5× as a side effect.
- **Idempotent infrastructure.** Signer's per-wallet `SBC.approve(Permit2, MAX)` boot step is safe to re-run; the app's `tx_hash → settlement_tx_hash` schema migration is `IF EXISTS`-guarded. Restart anything in any order; nothing breaks.
- **Every payment leaves a real audit trail** without the app touching the chain. `urls.settlement_tx_hash` + `urls.payer_address` come straight from the facilitator's response — paste either into a Radius explorer to see the on-chain transfer.

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
| Load testing | k6 (vanilla — no custom extensions) |
| Secrets | External Secrets Operator → GCP Secret Manager |
| CI | GitHub Actions (keyless OIDC, no stored credentials) |
| App | FastAPI + Redis + Postgres |
| Payment protocol | **x402 v2** — HTTP-native, signature-based |
| On-chain settlement | Radius testnet — SBC via **Permit2**, settled by the Radius first-party x402 facilitator (atomic, gas-sponsored) |

---

## The chaos workload — x402 payments on Radius

The test workload pays for itself. Every `POST /shorten` triggers a real micropayment, settled on-chain through the Radius first-party facilitator. The app itself never touches the chain.

```
1. Client → POST /shorten (no header)
     ← 402 with PAYMENT-REQUIRED header
       (base64 JSON: price, network=eip155:72344, payTo, assetTransferMethod=permit2)

2. Client → POST /sign-permit2 to the signer service
     ← {signature, permit2Authorization}      (off-chain EIP-712 sign — no tx)

3. Client → POST /shorten with PAYMENT-SIGNATURE header (base64 x402 envelope)
     app → POST /verify  → facilitator
     app → POST /settle  → facilitator submits ONE atomic on-chain tx via
                           x402ExactPermit2Proxy.settle:
                           Permit2 pulls SBC from payer → service wallet,
                           facilitator pays gas
     ← 201 + PAYMENT-RESPONSE header (settlement tx hash, payer address)
```

Why this matters for chaos: each step touches a different dependency, so a chaos run shows *which* leg degraded. NetworkChaos against the facilitator → `payment_facilitator_call_duration_seconds{op}` spikes. Signer pod kill → `sign_success_rate` drops. Redis outage → cache hit rate plummets but the app stays up. Three distinct chaos signatures, three distinct root causes.

---

## Performance baseline (post-x402, staging)

Measured against `facilitator.testnet.radiustech.xyz` on Radius testnet — 5-minute k6 run, 3 wallets at 60 iter/min, 301 iterations total.

| | Pre-x402 (Phase 5.5) | Post-x402 (Phase 5.5b) |
|---|---|---|
| Sign endpoint p95 | 1.64 s (`/pay` — chain submit + receipt poll) | **19.56 ms** (`/sign-permit2` — EIP-712 sign only) |
| `/shorten` p95 | 157 ms (verify a pre-submitted tx) | 680 ms (full facilitator round-trip incl. on-chain settle) |
| **End-to-end p95** | **~1.8 s** | **~700 ms** |
| Success rate | 100% | 100% |
| App's hot-path RPC calls | yes (receipt fetching) | none |
| Distinct failure modes | 9 (tx-shaped) | 6 (HTTP-shaped) |

End-to-end is ~2.5× faster despite `/shorten` itself being slower — the chain work moved from the client's side of the wire to inside `/shorten` via the facilitator. The architectural simplification is real and measurable. This is the anchor every chaos experiment compares against.

---

## Architectural deep-dives

- [**`docs/design/x402-migration.md`**](docs/design/x402-migration.md) — Full record of the x402 migration: design rationale, the mid-flight pivot from EIP-2612 to Permit2 after discovering the Radius first-party facilitator, M1 spike results with real on-chain settlement hashes, the §15 Phase 5.5b baseline.
- [**`LEARNINGS.md`**](LEARNINGS.md) — Per-phase decisions, gotchas, and "aha" moments. Worth reading for the surprises that only show up under real traffic.
- [**`PROJECT_CONTEXT.md`**](PROJECT_CONTEXT.md) — Durable handoff for new contributors / future-me.

---

## Run it locally

```bash
git clone https://github.com/amoghjay/k8s-chaos-promotion.git
cd k8s-chaos-promotion
docker compose up --build
```

App at `http://localhost:8000` · Swagger UI at `http://localhost:8000/docs`

```bash
# Shorten a URL — payment is disabled locally (no FACILITATOR_URL set)
curl -s -X POST http://localhost:8000/shorten \
  -H "Content-Type: application/json" \
  -d '{"url": "https://chaos-mesh.org"}' | jq .

# Health check
curl http://localhost:8000/health | jq .
```

The full x402 path is exercised in the dev/staging environments on GKE — see [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) for the reconnect checklist and trigger commands.

---

## Repository layout

```
app/                                FastAPI URL shortener — thin x402 facilitator client
signer/                             FastAPI Permit2 signer service (staging only)
  main.py                           /sign-permit2 + boot-time SBC.approve(Permit2)
  permit2.py                        Pure EIP-712 typed-data signing helpers (unit-testable)
helm/
  url-shortener/                    App Helm chart — dev/staging/prod overlays
  observability/                    kube-prometheus-stack + Loki + custom Grafana dashboard
kubernetes/
  bootstrap/                        ArgoCD App-of-Apps (platform tools)
  kargo/                            Stages, Warehouse, AnalysisTemplates
  jobs/                             Kustomize package: signer Deployment + suspended loadgen CronJob + ESO secrets
gke_terraform/                      GCP infrastructure (Terraform)
scripts/                            Wallet funding, manual smoke utilities
docs/design/                        Design docs (x402 migration, future chaos experiment specs)
.github/workflows/                  Keyless OIDC image builds → GAR
```

---

## Gotchas worth remembering

Documented in detail in `LEARNINGS.md`; surfaced here so they're visible up front:

- **`SBC.approve()` on Radius costs ~115k gas**, not vanilla ERC-20 ~46k — Turnstile-related state mutations inflate it. Hardcoding 100k OOG'd on the first attempt. Always `estimate_gas` for SBC writes.
- **The facilitator's validity signal is in the response body, not the HTTP status.** `/verify` returns HTTP 200 with `isValid: false` for bad signatures. 4xx/5xx is reserved for operational errors.
- **`invalidReason` is free-form prose.** Radius returns `"Invalid signature"`; Stablecoin.xyz returned `"invalid_exact_evm_payload_signature"`. Don't pivot Prometheus labels on it — coarse-bucket and log the prose for triage.
- **Facilitator idempotency replaces on-chain replay reverts.** A replayed signature returns the cached `success: true` + original transaction hash. The replay signal at the app layer is the `urls.settlement_tx_hash UNIQUE` constraint, not an on-chain failure.
- **Permit2's EIP-712 domain has no `version` field** (unlike EIP-2612). Three fields only: `{name: "Permit2", chainId, verifyingContract}`. `eth-account.encode_typed_data` handles the missing field correctly if you simply don't pass it.

---

## Status

x402 payment migration shipped. Phase 6 (Chaos Mesh experiments against the post-x402 architecture + Kargo verification gate) is the active workstream.

---

## Background

During my co-op at [Radius](https://radiustech.xyz) I built parts of the payment settlement infrastructure — RPC endpoints, on-chain transaction flows, service accounts. This project builds on top of that work: the chaos-as-promotion-gate mechanic is the platform-engineering layer that turns those primitives into something a deploy pipeline can actually trust.
