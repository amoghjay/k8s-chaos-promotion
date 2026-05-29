# Design: x402 payment migration

**Status:** Approved — implementation pending at M0
**Author:** Amogh
**Date:** 2026-05-28
**Supersedes:** [`X402_MIGRATION_PLAN.md`](../../X402_MIGRATION_PLAN.md) (May 8, 2026 — written before facilitator support and signer-backed loadgen landed)

---

## 1. Problem

The current `/shorten` payment path is a bespoke "send tx, then paste tx_hash into an API" protocol. `app/payment.py` is ~250 lines of web3.py receipt polling, manual Transfer-event decoding, and replay tracking against `urls.tx_hash UNIQUE`. Each new client (k6 today, an agent or a CLI tomorrow) has to learn that custom shape.

x402 is the standard HTTP-native version of the same idea. The Radius ecosystem has endorsed facilitators (Stablecoin.xyz, FareSide, Middlebit per `references/micropayments.md`) that handle on-chain verification and settlement out of band. Switching means:

- the app stops touching Radius RPC at all
- clients send a single signed EIP-2612 permit in an HTTP header — no tx_hash plumbing
- replay protection moves into the SBC token's per-owner nonce counter, not our Postgres table
- any x402-aware client can pay; the API stops being snowflake-shaped

**Important nomenclature:** x402's protocol field is named `"assetTransferMethod": "permit2"` but the on-chain mechanism on Radius is **EIP-2612 permit + transferFrom**, not Uniswap's Permit2 contract. The facilitator submits two on-chain txs: `permit(owner, spender, value, deadline, v, r, s)` sets allowance, then `transferFrom(owner, serviceWallet, value)` moves tokens. The settlement wallet (facilitator-owned) is the `spender`; our service wallet receives the SBC.

This document is the design for that migration. It is the source of truth that replaces the May 8 plan.

## 2. Decisions (load-bearing)

These three decisions shape every section below. Each was confirmed with the user on 2026-05-28.

| # | Decision | Rationale |
|---|---|---|
| **D1** | **Goal: learning depth + cleaner engineering.** Not portfolio polish. | The user is targeting Platform Engineering / SRE roles. Decisions favor "looks like a real platform engineer built it" over "ships fastest." |
| **D2** | **Timing: do x402 *before* Phase 6.** Not after. | Writing chaos experiments against payment code we're about to delete is wasteful. The Phase 5.5 baseline gets re-measured against the new flow anyway; better to do it once. |
| **D3** | **`radius-signer` is repurposed as an EIP-2612 permit signer, not deleted.** | Preserves secret-boundary discipline (keys in one pod, never in the load runner), keeps a meaningful chaos target with realistic blast radius, and mirrors real-world x402 topology (signing happens at the wallet, not the load driver). |

D1 means: take the time to self-explain Permit2/EIP-712 mechanics in code and docs. Do not paper over them with library magic.
D2 means: Phase 6 chaos work waits until x402 is live in dev + staging.
D3 means: signer keeps a process boundary; it stops talking to RPC on the hot path.

## 3. The new flow

### 3.1 Sequence

```
┌─────────┐   ┌──────────────┐   ┌──────────┐   ┌─────────────┐   ┌────────────┐
│   k6    │   │radius-signer │   │   app    │   │ facilitator │   │  Permit2   │
│ loadgen │   │ (signs only) │   │/shorten  │   │  (Radius)   │   │  (on-chain)│
└────┬────┘   └──────┬───────┘   └────┬─────┘   └──────┬──────┘   └─────┬──────┘
     │               │                │                │                 │
     │ 1. POST /sign-permit           │                │                 │
     ├──────────────►│                │                │                 │
     │   signature   │                │                │                 │
     │◄──────────────┤                │                │                 │
     │                                │                │                 │
     │ 2. POST /shorten                                │                 │
     │    header: PAYMENT-SIGNATURE   │                │                 │
     ├───────────────────────────────►│                │                 │
     │                                │ 3a. POST /verify                │
     │                                ├───────────────►│                 │
     │                                │  ok            │                 │
     │                                │◄───────────────┤                 │
     │                                │ 3b. POST /settle                │
     │                                ├───────────────►│ permitTransfer  │
     │                                │                ├────────────────►│
     │                                │                │  receipt        │
     │                                │                │◄────────────────┤
     │                                │ {settled, tx}  │                 │
     │                                │◄───────────────┤                 │
     │ 201 Created                    │                │                 │
     │  header: PAYMENT-RESPONSE      │                │                 │
     │◄───────────────────────────────┤                │                 │
```

### 3.2 What's load-bearing in each step

- **Step 1** — the signer constructs an EIP-712 PermitTransferFrom struct (see §4.2), signs it with the wallet's private key, returns `{signature, payload}`. No RPC call. Should complete in <10ms.
- **Step 2** — k6 base64-encodes `{x402Version: 2, resource, accepted, payload: {signature, permit2Authorization}}` and sends it as the `PAYMENT-SIGNATURE` header on `POST /shorten`.
- **Step 3a** — the app decodes the header, builds the `paymentRequirements` from its own config (so a client can't talk its way into a cheaper price), and POSTs to facilitator `/verify`. The facilitator checks: signature recovers to the signer address, amount matches, nonce is unused, deadline is in the future.
- **Step 3b** — on verify-ok, the app POSTs to facilitator `/settle`. The facilitator submits `Permit2.permitTransferFrom` on-chain, pays its own gas, and returns the settlement tx hash.
- **Response** — the app creates the short URL, embeds settlement details in the base64 `PAYMENT-RESPONSE` header, returns 201. If `/verify` or `/settle` fail, return 402 with the failure reason.

### 3.3 What dies

| Code that goes away | Why |
|---|---|
| `app/payment.py`: `init_web3`, `verify_payment`, `_get_receipt_with_retry`, `_find_transfer_event`, `_decode_indexed_address`, `_decode_uint256`, `_to_hex` | App no longer touches RPC. Facilitator does verification. |
| `web3>=7.0.0` from `app/requirements.txt` | No more Web3 client in the app. |
| `urls.tx_hash UNIQUE` constraint + `PAYMENT_REPLAY_ATTEMPTS` counter (in current form) | Permit2 nonces handle replay. We still track replay attempts but via a different signal — see §6. |
| `signer/main.py`: tx submission, receipt waiting, gas estimation, Turnstile pre-flight, `eth_sendRawTransaction` | Signer no longer submits txs. |

## 4. Component changes

### 4.1 App (`app/`)

`payment.py` shrinks to roughly:

```python
import base64, json, httpx
from dataclasses import dataclass
from enum import Enum

@dataclass
class SettlementResult:
    status: "SettlementStatus"
    message: str
    settlement_tx_hash: str = ""
    payer: str = ""

class SettlementStatus(Enum):
    SETTLED = "settled"
    SIGNATURE_INVALID = "signature_invalid"
    FACILITATOR_VERIFY_FAILED = "facilitator_verify_failed"
    FACILITATOR_SETTLE_FAILED = "facilitator_settle_failed"
    FACILITATOR_UNREACHABLE = "facilitator_unreachable"
    HEADER_MALFORMED = "header_malformed"

async def settle_payment(payment_signature_header: str) -> SettlementResult:
    # 1. decode base64 → JSON payload
    # 2. build paymentRequirements from our own env (not client-supplied)
    # 3. httpx.post(facilitator + "/verify")  → bail on failure
    # 4. httpx.post(facilitator + "/settle")  → bail on failure
    # 5. return SettlementResult
```

Estimated size: ~80 lines, ~⅓ of today's `payment.py`.

`main.py` `/shorten` handler changes:
- Replace `body.tx_hash` field with the `PAYMENT-SIGNATURE` header (FastAPI `Header` dependency).
- 402 response gains the `PAYMENT-REQUIRED` header (base64 JSON of `accepts[]`).
- 201 response gains the `PAYMENT-RESPONSE` header.
- The `ON CONFLICT (url) DO NOTHING` branch and tx_hash uniqueness path are simplified (see §5).

### 4.2 Signer (`signer/`)

The EIP-2612 permit the signer builds and signs. **The verifying contract is the SBC token itself**, not a separate Permit2 deploy. Domain values come from `references/gotchas.md#8` and must match exactly or `recoverTypedDataAddress` silently fails.

```python
# Domain — verifying contract is the SBC token; "Stable Coin" is the literal token name
EIP2612_DOMAIN = {
    "name": "Stable Coin",                                  # NOT "SBC", NOT "Permit2"
    "version": "1",                                         # string, not number
    "chainId": 72344,                                       # Radius testnet
    "verifyingContract": SBC_CONTRACT,                      # 0x33ad...014Fb
}

EIP2612_TYPES = {
    "Permit": [
        {"name": "owner",    "type": "address"},
        {"name": "spender",  "type": "address"},
        {"name": "value",    "type": "uint256"},
        {"name": "nonce",    "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
    ],
}

# Nonce is NOT random — it's a sequential per-owner counter on the SBC token.
# Read via token.nonces(owner) (selector 0x7ecebe00) at "pending" tag.
nonce = await asyncio.to_thread(
    token.functions.nonces(slot.address).call,
    block_identifier="pending",
)

message = {
    "owner":    slot.address,
    "spender":  facilitator_settlement_wallet,              # from facilitator /supported
    "value":    amount,                                     # SBC raw units (6 decimals)
    "nonce":    nonce,
    "deadline": int(time.time()) + DEADLINE_SECONDS,
}

signed = Account.sign_typed_data(
    slot.private_key,
    domain_data=EIP2612_DOMAIN,
    message_types=EIP2612_TYPES,
    message_data=message,
)

# v-value normalization — hardware wallets return v=0|1 and recovery silently fails
# (per references/gotchas.md#9). eth-account's local sign produces v=27|28 already,
# but normalize defensively for future-proofing if we ever accept external signatures.
r = signed.signature[:32]
s = signed.signature[32:64]
v = signed.signature[64]
if v < 27:
    v += 27
signature = (r + s + bytes([v])).hex()
```

New endpoint shape:

```
POST /sign-permit
  body: { wallet_index, amount, deadline_seconds? }
  → 200 { signature, payload, nonce, spender }    # client sends signature + payload in PAYMENT-SIGNATURE header
  → 400 wallet_index out of range
  → 502 nonce read failed (RPC error)             # nonce reads must hit the chain
  → 503 facilitator /supported unreachable        # spender lookup failed
```

`/health` and `/wallets` stay. SBC balance gauge stays — still load-bearing for "do my wallets have enough SBC for the facilitator to transferFrom out of." RUSD balance gauge stays but de-emphasized (signer wallets no longer pay gas; the facilitator's settlement wallet does).

**The signer keeps a Web3 RPC dependency** (smaller than before but not gone), because EIP-2612 nonces must be read from the chain before each sign. This is a *read* path (`eth_call` to `token.nonces(owner)`), not a write path. Add a small Redis cache + per-wallet lock to deduplicate concurrent nonce reads under load — see §10.

`signer/requirements.txt` keeps `eth-account` (does EIP-712) and `web3` (for `nonces()` reads). Drops nothing.

### 4.2.1 The facilitator's `spender` address

The EIP-2612 `spender` field is the facilitator's settlement wallet, not the SBC service wallet. The signer must discover this at startup via `GET {FACILITATOR_URL}/supported`, which returns the facilitator's signer addresses per network (`references/micropayments.md:1162`). Cache it; refresh on signer pod restart.

If the facilitator rotates its settlement wallet, signatures generated with the old `spender` will fail at `/verify`. Surface this in the dashboard as a sharp uptick in `payment_facilitator_total{outcome="facilitator_verify_failed"}` — operationally, that's the "facilitator rotated; restart signer" signal.

### 4.3 Loadgen (`kubernetes/jobs/scripts/loadgen.js`)

Hot loop changes:

```js
// before
signer.POST(/pay) → {tx_hash}
app.POST(/shorten, body: {url, tx_hash}) → 201

// after
signer.POST(/sign-permit) → {signature, payload}
header = base64(JSON.stringify({x402Version: 2, ..., payload: {signature, ...}}))
app.POST(/shorten, body: {url}, header: PAYMENT-SIGNATURE) → 201
```

The base64 wrap is plain JS — no xk6 extension work needed. xk6-ethereum stays installed but is unused on the hot path. We could remove the custom k6 image entirely once the migration is done, but I'd defer that to a follow-on cleanup.

## 5. Data model

### 5.1 Replay protection

Today: `urls.tx_hash VARCHAR(66) UNIQUE` — if a client retries `/shorten` with the same tx_hash, Postgres throws and we increment `payment_replay_attempts_total`.

Under x402, replay protection moves to **two layers**:

1. **Permit2 contract layer (authoritative).** Each Permit2 nonce is one-shot per wallet. If a client tries to settle the same signature twice, the facilitator's second call to `permitTransferFrom` reverts. The facilitator returns the failure, and we surface it as `payment_facilitator_total{outcome="settle_replay"}`.
2. **Application layer (idempotency).** Store the **settlement tx hash** returned by the facilitator as `urls.settlement_tx_hash VARCHAR(66) UNIQUE`. This catches the (very narrow) case where a malicious client tries to use a single settled payment for two different `/shorten` calls — though Permit2 already makes that impossible by burning the nonce.

Net: we replace `tx_hash` with `settlement_tx_hash`. The schema change:

```sql
ALTER TABLE urls
  RENAME COLUMN tx_hash TO settlement_tx_hash;
-- UNIQUE constraint is preserved.
```

### 5.2 What else to store

For observability and post-hoc debugging:
- `settlement_tx_hash` (existing column, renamed)
- `payer_address` (returned by facilitator `/settle`) — new column, no index
- `settled_at` (timestamp, defaults to `NOW()`) — new column

Nice-to-have. Not blocking the migration.

## 6. Observability (first-class — required by user)

The whole point of metric-by-metric mapping is that **the existing Grafana dashboards must keep working** with minimal panel rewrites. Some labels change; the panel queries do too. Below is the full mapping.

### 6.1 App-side metrics

| Today | After | Notes |
|---|---|---|
| `payment_verifications_total{status=success\|invalid_tx_hash\|tx_not_found\|tx_failed\|wrong_contract\|wrong_recipient\|insufficient_amount\|no_transfer_event\|rpc_error}` | `payment_facilitator_total{outcome=settled\|signature_invalid\|facilitator_verify_failed\|facilitator_settle_failed\|facilitator_unreachable\|header_malformed}` | Rename metric so Prometheus drops the old time series cleanly; remap the dashboard "Payment outcomes" panel to the new label set. Fewer labels, more meaningful (each one is an actual failure mode of the facilitator-mediated flow). |
| `payment_verification_duration_seconds` | `payment_settlement_duration_seconds` | Histogram. Now measures app-perceived end-to-end: header decode + verify + settle. Buckets stay the same. |
| `payment_402_responses_total` | unchanged | Still emit on missing/invalid header. |
| `payment_replay_attempts_total` | unchanged (semantics shifted) | Now incremented on `urls.settlement_tx_hash UNIQUE` violation, which should be ~0 if Permit2 is doing its job. Useful as a "is Permit2 nonce hygiene working?" signal. |
| (new) | `payment_facilitator_call_duration_seconds{op=verify\|settle}` | Histogram. Lets us separate "facilitator is slow" from "we're slow" during chaos. Critical for the new chaos experiment (see §7). |

### 6.2 Signer-side metrics

| Today | After | Notes |
|---|---|---|
| `signer_pay_total{outcome=success\|insufficient_balance\|preflight_revert\|send_error\|receipt_timeout\|on_chain_failure\|unexpected_error, wallet_index}` | `signer_sign_total{outcome=success\|wallet_unknown\|signing_error, wallet_index}` | Most failure modes disappear (no submission = no `send_error`, `receipt_timeout`, etc). Dashboard panel "Signer outcomes" gets simpler. |
| `signer_pay_duration_seconds{outcome}` | `signer_sign_duration_seconds{outcome}` | Histogram. Will be ~10× faster — EIP-712 signing is cheap, no RPC round-trip. Adjust buckets: `(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5)`. |
| `signer_tx_submit_seconds` | **dropped** | No more tx submission. |
| `signer_tx_receipt_wait_seconds` | **dropped** | No more receipts. |
| `signer_tx_gas_used` | **dropped** | Facilitator pays gas, not us. |
| `signer_wallet_sbc_balance_units{wallet_index, address}` | unchanged | Still load-bearing for "do my wallets have enough SBC to keep signing." |
| `signer_wallet_rusd_balance_wei{wallet_index, address}` | **kept but de-emphasized** | The wallet no longer pays gas (facilitator does), so RUSD balance stops being relevant for liveness. Keep the gauge — useful for diagnostics — but remove from the main dashboard. |
| (new) | `signer_nonce_read_total{outcome=ok\|rpc_error\|stale_cache, wallet_index}` | Counter. The signer reads `token.nonces(owner)` on the SBC contract before each sign (EIP-2612 requirement). Tracks RPC-read outcomes per wallet. |
| (new) | `signer_nonce_read_duration_seconds{outcome}` | Histogram, buckets `(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)`. p95 spikes here = Radius RPC degraded; correlate with chaos experiment #6. |
| (new) | `signer_facilitator_spender_cache_total{outcome=hit\|miss\|refresh_ok\|refresh_failed}` | Counter. Tracks the cached facilitator-`spender` address lookups (see §4.2.1). A `refresh_failed` spike means the facilitator's `/supported` endpoint is unreachable. |

### 6.3 Loadgen metrics (k6)

| Today | After | Notes |
|---|---|---|
| `tx_submit_success_rate` | **dropped** | k6 no longer submits a tx; it asks for a signature. |
| `tx_receipt_success_rate` | **dropped** | No receipts. |
| `tx_confirmation_ms` | **dropped** | No confirmations. |
| (new) | `sign_success_rate` | Rate. Did the signer return a valid signature? |
| (new) | `payment_settled_rate` | Rate. Did `/shorten` return 201 with `PAYMENT-RESPONSE` header populated? Distinguishes "shorten failed because payment failed" from "shorten failed because app is broken." |
| `shorten_201_rate`, `shorten_402_rate`, `shorten_409_rate`, `shorten_5xx_rate` | unchanged | Status code rates stay identical — only their *meaning* shifts (`409` now means duplicate URL, not duplicate tx_hash). |
| `redirect_ok_rate` | unchanged | Cache and redirect path are untouched by the migration. |
| `http_req_duration{endpoint:pay}` | `http_req_duration{endpoint:sign}` | Rename the k6 tag so the threshold makes sense (sign is fast, was a real on-chain call before). Update threshold from `p(95)<2500` to `p(95)<100`. |

### 6.4 Dashboard panels

Two panels need their PromQL rewritten (the `payment_verifications_total` panel and the signer outcomes panel). All other panels (Request Rate by Handler, P95 Latency by Handler, Response Codes by Handler, cache hit/miss, etc.) keep working because they query `http_requests_total` and `http_request_duration_seconds`, which are unchanged.

I'll commit the updated dashboard JSON in the same PR that does the metric rename.

## 7. Phase 6 chaos experiments — revised

The user's framing is correct: **chaos targets are our pods and in-cluster network, not third-party RPC.** The Phase 6 experiment list under x402:

| # | Experiment | Target | Expected signal |
|---|---|---|---|
| 1 | FastAPI pod kill | `url-shortener` deployment | `shorten_201_rate` dips briefly during pod restart, recovers within readiness probe interval. |
| 2 | Redis pod kill | `redis` statefulset | `cache_hits_total` drops to zero; `cache_misses_total` rises; redirect rate stays at 100% via Postgres fallback. App stays healthy (the `_started` flag in `main.py` keeps the pod in the service after first ready). |
| 3 | Redis ↔ app latency | NetworkChaos between app and redis | p95 redirect latency rises while p99 of `/shorten` (which doesn't touch Redis on success) stays flat. |
| 4 | **Facilitator egress latency/failure** (was: Radius RPC) | NetworkChaos on egress from `url-shortener` to the chosen facilitator host (default `x402.stablecoin.xyz`) | `payment_facilitator_call_duration_seconds{op=verify}` p95 spikes; `payment_facilitator_total{outcome="facilitator_unreachable"}` increments; `shorten_5xx_rate` rises proportionally. App pod itself stays healthy — the failure is correctly attributed to the external dependency. |
| 5 | **Signer pod kill** (new, enabled by D3) | `radius-signer` deployment | k6 sees `sign_success_rate` drop to 0 during signer restart. Distinguishes "auth service down" failure mode from "app down" or "facilitator down" — three different chaos signatures in Grafana, three different root causes. |
| 6 | **Signer → Radius RPC latency** (new, EIP-2612 nonce read) | NetworkChaos on egress from `radius-signer` to `rpc.testnet.radiustech.xyz` | `signer_nonce_read_duration_seconds` p95 spikes; `signer_sign_duration_seconds` rises proportionally; `sign_success_rate` drops if RPC fully timeouts. Tests the signer's RPC dependency for the permit-nonce read — a small but real dependency we can't eliminate under EIP-2612 (vs Permit2 which would have removed it). |

Experiment 4 is the one that "moves out of your pod" — and that's correct platform-engineering reasoning. You can't fault-inject into infrastructure you don't own; you can fault-inject into your *dependency on* that infrastructure. Same SLO shape, more honest test.

## 8. Phase 5.5 baseline migration

`LEARNINGS.md` records the Phase 5.5 baseline: "243 iter, 82% payment success, p95=515ms." That number measures *the old payment path* (signer submits SBC transfer → app polls receipt). Under x402, that number is gone forever.

Plan:
1. Keep the existing baseline as a historical reference in `LEARNINGS.md` ("Phase 5.5 baseline — pre-x402 flow").
2. After x402 lands in staging, run the same loadgen config and capture a new baseline ("Phase 5.5b baseline — post-x402 flow"). Expect this to be *faster* because no on-chain submission is in the load path — the chain work happens out-of-band via the facilitator.
3. Phase 6 chaos experiments compare against the post-x402 baseline.

Don't try to make the two numbers comparable. They're not measuring the same thing.

## 9. Rollback and rollout

**Recommendation: hard cut in dev, parallel-route in staging, hard cut in prod.**

- **Dev** — hard cut. Replace `/shorten` outright. If anything's wrong, fix forward. Dev's only consumer is the loadgen, which we're updating in the same change.
- **Staging** — add a new `/shorten` (x402) alongside the legacy route at `/shorten/legacy` for **one Kargo promotion cycle**. Loadgen exercises both for ~24h. If x402 metrics are healthy, delete `/shorten/legacy` in the next promotion.
- **Prod** — by the time x402 reaches prod via Kargo, it's been baked in staging. Hard cut. Don't carry parallel routes into prod.

Why not parallel-route everywhere: it doubles the surface area for chaos testing (Phase 6) for the entire duration, and the parallel route adds no information after staging is green. Staging is where the bake happens.

## 10. Open questions (not blocking, but flag during implementation)

1. ~~**Permit2 contract address on Radius testnet.**~~ **Resolved**: Radius x402 uses EIP-2612 permit on the SBC token itself, not Uniswap's Permit2. `verifyingContract` is `0x33ad9e4BD16B69B5BFdED37D8B5D9fF9aba014Fb` (the SBC contract). Domain values per `references/gotchas.md#8`.
2. **Which facilitator to default to.** Three endorsed options per `references/micropayments.md`:
   - **Stablecoin.xyz** (`https://x402.stablecoin.xyz`) — primary, supports both testnet+mainnet, absorbs gas. Best default for portfolio reproducibility.
   - **FareSide** (`https://facilitator.x402.rs`) — testnet only, free for testing. Good fallback if Stablecoin.xyz rate-limits during a chaos demo.
   - **Middlebit** (`https://middlebit.com`) — mainnet only, multi-facilitator routing. Not relevant for testnet.

   **Proposed default**: `x402.stablecoin.xyz`. Override via `FACILITATOR_URL` env var so we can swap without redeploying app code.
3. **Nonce read caching.** EIP-2612 nonces are a sequential per-owner counter. Reading from chain on every sign costs ~1 round-trip to Radius RPC (~10-50ms). For ~60 req/min across 3 wallets, that's tolerable, but we could cache `last_known_nonce` per wallet in Redis or in-memory and increment optimistically. Risk: if the cache desyncs from chain, all subsequent signatures fail until refresh. Recommend: **start without caching** (read every time), add caching only if `signer_nonce_read_duration_seconds` p95 becomes a real bottleneck under load.
4. **Loadgen base64 helper.** k6 JS has no native base64 — use [k6's `encoding` module](https://k6.io/docs/javascript-api/k6-encoding/) (`encoding.b64encode`). No xk6 extension needed.
5. **v-value normalization on signature output.** `eth-account` local signing produces v=27|28, but `references/gotchas.md#9` flags this as a silent-failure mode for any signature from external sources. Defensive normalization in the signer is one line — add it from day 1.
6. **Facilitator failure modes during real chaos.** What HTTP status does the facilitator return for verify-fail vs settle-fail vs network-error? Need to characterize during M1 so dashboard alerts distinguish them. Worth recording in `LEARNINGS.md` once observed.

## 11. Out of scope

- Self-hosting the facilitator. Deferred — Radius's hosted facilitator is fine for a portfolio project. Revisit only if it goes down during a demo.
- Removing the custom k6 image. Possible follow-on once xk6-ethereum is unused on the hot path. Not blocking.
- The "agent pays" demo flow (curl + CLI signer). Out of scope for the migration itself but cheap to add later because the signer's contract is now clean.

## 12. Implementation sequencing

Phases here are *implementation* phases (within this migration), not the project's Phase 6 / 7 phases.

| Step | Deliverable | Bake-out |
|---|---|---|
| **M1** | Spike: stand-alone Python script proves **EIP-2612 permit + EIP-712 signing + facilitator `/verify`+`/settle`** on Radius testnet. Verify domain values (`name: "Stable Coin"`, `version: "1"`), confirm `token.nonces()` read, capture facilitator HTTP responses for happy path + verify-fail + settle-fail. | Manual `cast` verification of resulting on-chain `permit()`+`transferFrom()` pair. |
| **M2** | Repurpose `signer/` to `/sign-permit`. Add nonce-read via `token.nonces(owner)`. Add facilitator `/supported` cache for `spender` discovery. Add v-value normalization. Keep `/health`, `/wallets`. Delete `/pay`. | Unit tests: signature validity (recover address from sig, compare to wallet), nonce-read correctness, v-normalization for v=0|1 inputs. |
| **M3** | Rewrite `app/payment.py` as facilitator client. Update `main.py` to use `PAYMENT-SIGNATURE` header. Schema migration for `tx_hash → settlement_tx_hash`. | docker-compose locally: full payment flow end-to-end against testnet facilitator. |
| **M4** | Update `loadgen.js`: drop tx submission, add `/sign-permit` call + base64 header. Update thresholds and metric names. | k6 run in `url-shortener-dev` namespace. |
| **M5** | Update Grafana dashboard ConfigMap (PromQL for the two changed panels). | Visual check in staging Grafana. |
| **M6** | Deploy to staging. Run for 24h alongside legacy route. | Phase 5.5b baseline captured; LEARNINGS.md updated. |
| **M7** | Delete legacy `/shorten` from app, delete `tx_hash` column from schema, drop `web3` from app `requirements.txt`. | Verify no callers reference the legacy path. |

Estimated calendar time: ~1 week if focused. If it sprawls past 10 days, fall back to "do Phase 6 first, x402 second" (the original X402_MIGRATION_PLAN.md recommendation).

## 13. References

- Live x402 integration doc: `https://docs.radiustech.xyz/developer-resources/x402-integration.md`
- radius-dev skill `references/micropayments.md` — endorsed facilitator list + x402 v2 protocol summary
- radius-dev skill `references/gotchas.md#8` — EIP-2612 domain values (load-bearing)
- radius-dev skill `references/gotchas.md#9` — v-value normalization
- radius-dev skill `references/gotchas.md#10` — nonce-read pattern
- radius-dev skill `references/gotchas.md#11` — settlement uses `permit()` + `transferFrom()` (two on-chain txs)
- Old plan (superseded): [`X402_MIGRATION_PLAN.md`](../../X402_MIGRATION_PLAN.md)
- Phase 5.5 baseline: [`LEARNINGS.md`](../../LEARNINGS.md) (search "Phase 5.5")
- Current payment code: [`app/payment.py`](../../app/payment.py), [`signer/main.py`](../../signer/main.py)
- Current dashboards: [`helm/observability/templates/dashboard-cm.yaml`](../../helm/observability/templates/dashboard-cm.yaml)
