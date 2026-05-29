# Design: x402 payment migration

**Status:** M0 + M1 complete (2026-05-29). Facilitator switched mid-flight from Stablecoin.xyz (EIP-2612) to Radius first-party (Permit2). M2 next. See §14 for M0 recon, §15 for M1 results and the pivot record.
**Author:** Amogh
**Date:** 2026-05-28
**Supersedes:** [`X402_MIGRATION_PLAN.md`](../../X402_MIGRATION_PLAN.md) (May 8, 2026 — written before facilitator support and signer-backed loadgen landed)

---

## 1. Problem

The current `/shorten` payment path is a bespoke "send tx, then paste tx_hash into an API" protocol. `app/payment.py` is ~250 lines of web3.py receipt polling, manual Transfer-event decoding, and replay tracking against `urls.tx_hash UNIQUE`. Each new client (k6 today, an agent or a CLI tomorrow) has to learn that custom shape.

x402 is the standard HTTP-native version of the same idea. The Radius ecosystem ships a first-party facilitator (`facilitator.testnet.radiustech.xyz` / `facilitator.radiustech.xyz`) that handles on-chain verification and settlement out of band. Switching means:

- the app stops touching Radius RPC at all
- clients send a signed Permit2 `PermitWitnessTransferFrom` in an HTTP header — no tx_hash plumbing
- replay protection moves into Permit2's per-owner bitmap nonces, not our Postgres table
- any x402-aware client can pay; the API stops being snowflake-shaped

**Important nomenclature:** x402's protocol field on the Radius first-party facilitator is `"assetTransferMethod": "permit2"` (confirmed via `/supported`, 2026-05-29 — see §14, §15). The on-chain mechanism is **Uniswap's Permit2** invoked through the canonical `x402ExactPermit2Proxy` (`0x402085c248EeA27D92E8b30b2C58ed07f9E20001`). Settlement is a **single atomic transaction**: the facilitator calls `x402ExactPermit2Proxy.settle(permit, owner, witness, signature)` which validates the payer's Permit2 signature, then Permit2 pulls SBC from the payer's wallet directly to the `witness.to` address (our service wallet). The proxy enforces the destination via the witness pattern — the facilitator can't redirect funds.

> **History note:** an earlier iteration of this doc (M0–early-M1) targeted Stablecoin.xyz's EIP-2612 facilitator. We pivoted to the Radius first-party Permit2 facilitator on 2026-05-29 after discovering it during M0 — see §15 for the full pivot record and the EIP-2612 spike's findings.

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
│   k6    │   │radius-signer │   │   app    │   │ facilitator │   │ x402 proxy │
│ loadgen │   │ (signs only) │   │/shorten  │   │  (Radius)   │   │ + Permit2  │
└────┬────┘   └──────┬───────┘   └────┬─────┘   └──────┬──────┘   └─────┬──────┘
     │               │                │                │                 │
     │ 1. POST /sign-permit2          │                │                 │
     ├──────────────►│                │                │                 │
     │   signature   │                │                │                 │
     │◄──────────────┤                │                │                 │
     │                                │                │                 │
     │ 2. POST /shorten                                │                 │
     │    header: PAYMENT-SIGNATURE   │                │                 │
     ├───────────────────────────────►│                │                 │
     │                                │ 3a. POST /verify                │
     │                                ├───────────────►│                 │
     │                                │  isValid:true  │                 │
     │                                │◄───────────────┤                 │
     │                                │ 3b. POST /settle                │
     │                                ├───────────────►│ proxy.settle    │
     │                                │                ├────────────────►│
     │                                │                │ (atomic 1 tx)   │
     │                                │                │◄────────────────┤
     │                                │ {success, tx}  │                 │
     │                                │◄───────────────┤                 │
     │ 201 Created                    │                │                 │
     │  header: PAYMENT-RESPONSE      │                │                 │
     │◄───────────────────────────────┤                │                 │
```

### 3.2 What's load-bearing in each step

- **Step 1** — the signer constructs an EIP-712 `PermitWitnessTransferFrom` struct against the Permit2 contract's domain (see §4.2), signs it with the wallet's private key, returns `{signature, permit2Authorization}`. **No RPC call on the hot path** — Permit2 uses random bitmap nonces, not on-chain sequential counters. Should complete in <5ms.
- **Step 2** — k6 base64-encodes `{x402Version: 2, resource, accepted, payload: {signature, permit2Authorization}}` and sends it as the `PAYMENT-SIGNATURE` header on `POST /shorten`.
- **Step 3a** — the app decodes the header, builds the `paymentRequirements` from its own config (so a client can't talk its way into a cheaper price), and POSTs to facilitator `/verify`. The facilitator checks: signature recovers to `permit2Authorization.from`, the Permit2 allowance exists, the payer has sufficient balance, the deadline is in the future, the `witness.to` matches the requirements' `payTo`, and the nonce bit is unused. Response: HTTP 200 + `{isValid: bool, payer, invalidReason?}`. Note: validity is signaled by `isValid`, not by HTTP status.
- **Step 3b** — on `isValid: true`, the app POSTs to facilitator `/settle`. The facilitator submits **one atomic on-chain transaction** calling `x402ExactPermit2Proxy.settle(permit, owner, witness, signature)`. The proxy validates the signature against Permit2, asks Permit2 to pull SBC from the payer using its existing allowance, and the witness pattern enforces that funds go to the merchant address. Facilitator pays gas. Returns `{success, transaction, payer, network}` on HTTP 200.
- **Response** — the app creates the short URL, embeds settlement details in the base64 `PAYMENT-RESPONSE` header, returns 201. If `/verify` returns `isValid: false` or `/settle` returns `success: false`, return 402 with the failure reason.

> **Bootstrap (one-time per payer wallet):** before any of the above can succeed, the payer must have called `SBC.approve(Permit2, MAX_UINT256)` once. The signer handles this lazily at boot when allowance is zero (see §4.2). Subsequent payments from that wallet are fully gas-free for the payer.

### 3.3 What dies

| Code that goes away | Why |
|---|---|
| `app/payment.py`: `init_web3`, `verify_payment`, `_get_receipt_with_retry`, `_find_transfer_event`, `_decode_indexed_address`, `_decode_uint256`, `_to_hex` | App no longer touches RPC. Facilitator does verification. |
| `web3>=7.0.0` from `app/requirements.txt` | No more Web3 client in the app. |
| `signer/main.py`: tx submission, receipt waiting, gas estimation, Turnstile pre-flight, `eth_sendRawTransaction` on the hot path | Signer no longer submits payment txs. (Web3 stays — the one-time approval at boot still needs it, see §4.2.) |
| `urls.tx_hash` (replaced by `settlement_tx_hash`) and the bespoke `PAYMENT_REPLAY_ATTEMPTS` counter | Permit2's per-bit nonce consumption + facilitator-side idempotency cache handle on-chain replay. The app keeps a `UNIQUE` constraint on the settlement tx hash as the canonical idempotency guard — see §5.1, §6. |

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

The Permit2 `PermitWitnessTransferFrom` the signer builds and signs. **The verifying contract is the Permit2 contract**, not the SBC token. The Permit2 domain omits the `version` field — Uniswap's deliberate choice. Confirmed against the live testnet facilitator by the M1 spike (see §15).

```python
# Permit2 domain — three fields, NO version. Permit2 is deployed at the same
# canonical CREATE2 address on every EVM chain.
PERMIT2_DOMAIN = {
    "name": "Permit2",
    "chainId": 72344,                                            # Radius testnet
    "verifyingContract": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
}

# Type tree for PermitWitnessTransferFrom + the x402 Witness extension.
# Witness shape sourced from x402ExactPermit2Proxy: "Witness(address to,uint256 validAfter)"
PERMIT2_TYPES = {
    "PermitWitnessTransferFrom": [
        {"name": "permitted", "type": "TokenPermissions"},
        {"name": "spender",   "type": "address"},
        {"name": "nonce",     "type": "uint256"},
        {"name": "deadline",  "type": "uint256"},
        {"name": "witness",   "type": "Witness"},
    ],
    "TokenPermissions": [
        {"name": "token",  "type": "address"},
        {"name": "amount", "type": "uint256"},
    ],
    "Witness": [
        {"name": "to",         "type": "address"},
        {"name": "validAfter", "type": "uint256"},
    ],
}

# Nonce is a random uint256 (Permit2 uses unordered bitmap nonces).
# Collision probability is negligible; no on-chain read required.
nonce = int.from_bytes(secrets.token_bytes(32), "big")

message = {
    "permitted": {"token": SBC_CONTRACT, "amount": amount},
    "spender":   X402_PROXY,                                     # 0x402085...20001 (hardcoded)
    "nonce":     nonce,
    "deadline":  int(time.time()) + DEADLINE_SECONDS,
    "witness":   {"to": service_wallet, "validAfter": 0},
}

signed = Account.sign_typed_data(
    slot.private_key,
    domain_data=PERMIT2_DOMAIN,
    message_types=PERMIT2_TYPES,
    message_data=message,
)

# v-value normalization (gotchas.md#9 — defensive).
sig = bytes(signed.signature)
if sig[64] < 27:
    sig = sig[:64] + bytes([sig[64] + 27])
signature = "0x" + sig.hex()
```

New endpoint shape:

```
POST /sign-permit2
  body: { wallet_index, amount, deadline_seconds? }
  → 200 { signature, permit2Authorization }       # client sends both in PAYMENT-SIGNATURE header
  → 400 wallet_index out of range
  → 503 wallet not bootstrapped (no Permit2 allowance — should be impossible after boot)
```

`/health` and `/wallets` stay. SBC balance gauge stays — still load-bearing for "does this wallet have enough SBC to back its outstanding signed permits." RUSD balance gauge can be dropped from the main dashboard (payer wallets pay zero gas after the one-time approval).

**The signer's Web3 RPC dependency shrinks to boot-time only.** At startup, the signer:
1. Reads `SBC.allowance(payer, Permit2)` for each configured wallet (one RPC call per wallet).
2. If any allowance is below the working threshold, sends `SBC.approve(Permit2, MAX_UINT256)` for that wallet (one tx per wallet, ~115k gas on Radius — gas-estimate driven, not hardcoded, per §15 finding).
3. Caches the spender (hardcoded `x402ExactPermit2Proxy`, but persisted for symmetry with `/wallets` introspection).

After boot, the hot path has **zero RPC calls per request** — `secrets.token_bytes(32)` for the nonce, EIP-712 sign, return.

`signer/requirements.txt` keeps `eth-account` (EIP-712) and `web3` (boot-time allowance check + approve). Drops nothing in the code, but the `web3` calls on the hot path go away entirely.

### 4.2.1 The Permit2 spender — hardcoded, not discovered

Unlike the EIP-2612 path (where the signer had to fetch the facilitator's settlement wallet from `/supported`), Permit2's spender is the canonical `x402ExactPermit2Proxy` at `0x402085c248EeA27D92E8b30b2C58ed07f9E20001` — deployed via CREATE2 at the same address on every EVM chain. The Radius facilitator's `/supported` response returns `signers: {}` (empty) to signal this.

No facilitator-rotation failure mode. No `/supported` cache to invalidate. The signer can ship the proxy address as a constant.

### 4.3 Loadgen (`kubernetes/jobs/scripts/loadgen.js`)

Hot loop changes:

```js
// before
signer.POST(/pay) → {tx_hash}
app.POST(/shorten, body: {url, tx_hash}) → 201

// after
signer.POST(/sign-permit2) → {signature, permit2Authorization}
header = base64(JSON.stringify({x402Version: 2, ..., payload: {signature, permit2Authorization}}))
app.POST(/shorten, body: {url}, header: PAYMENT-SIGNATURE) → 201
```

The base64 wrap is plain JS — no xk6 extension work needed. xk6-ethereum stays installed but is unused on the hot path. We could remove the custom k6 image entirely once the migration is done, but I'd defer that to a follow-on cleanup.

## 5. Data model

### 5.1 Replay protection

Today: `urls.tx_hash VARCHAR(66) UNIQUE` — if a client retries `/shorten` with the same tx_hash, Postgres throws and we increment `payment_replay_attempts_total`.

Under x402, replay protection sits at **three layers**, but M1 surfaced an important nuance about how observable each one is to the app:

1. **Permit2 on-chain layer (authoritative consume).** Permit2 uses unordered bitmap nonces — bit `nonce % 256` of `nonceBitmap[owner][nonce >> 8]` is set on a successful `permitWitnessTransferFrom`. A second on-chain call with the same nonce would revert. This is the cryptographic root of replay safety.
2. **Facilitator idempotency cache.** **M1 finding (§15):** before a duplicate request reaches the chain, the facilitator's own idempotency cache returns the original `{success: true, transaction: <original tx hash>}` for any payload it has already settled. The on-chain `permit()` revert path almost never fires in practice. Documented by Radius: *"Settlements are keyed from the payment payload and signature, so duplicate settlement attempts can return the existing result."*
3. **Application layer (idempotency / observability).** `urls.settlement_tx_hash VARCHAR(66) UNIQUE` catches the duplicate-tx-hash insert that follows from layer 2 returning a cached response. This is the only replay signal the app can observe directly — increment `payment_replay_attempts_total` on the constraint violation.

The original design assumed the on-chain revert would be the surfaced signal; M1 showed it's actually the app-layer UNIQUE constraint. The `payment_facilitator_total{outcome="settle_replay"}` bucket (originally planned in §6.1) is dropped — it would essentially never fire.

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
| `payment_verifications_total{status=success\|invalid_tx_hash\|tx_not_found\|tx_failed\|wrong_contract\|wrong_recipient\|insufficient_amount\|no_transfer_event\|rpc_error}` | `payment_facilitator_total{outcome=settled\|signature_invalid\|facilitator_verify_failed\|facilitator_settle_failed\|facilitator_unreachable\|header_malformed}` | Rename metric so Prometheus drops the old time series cleanly; remap the dashboard "Payment outcomes" panel to the new label set. **M1 caveat (§15):** the facilitator's `invalidReason` field is free-form human prose (e.g. `"Invalid signature"` on Radius), not a stable machine token. Don't pivot label values off it. Coarse-bucket all sig-recovery failures as `signature_invalid` and log the prose alongside for human triage. |
| `payment_verification_duration_seconds` | `payment_settlement_duration_seconds` | Histogram. Now measures app-perceived end-to-end: header decode + verify + settle. Buckets stay the same. |
| `payment_402_responses_total` | unchanged | Still emit on missing/invalid header. |
| `payment_replay_attempts_total` | unchanged (semantics shifted) | Now incremented on `urls.settlement_tx_hash UNIQUE` violation. **M1 finding:** this *is* the replay signal — the facilitator's idempotency cache means we won't see facilitator-side replay rejections. |
| (new) | `payment_facilitator_call_duration_seconds{op=verify\|settle}` | Histogram. Lets us separate "facilitator is slow" from "we're slow" during chaos. Critical for the new chaos experiment (see §7). |

### 6.2 Signer-side metrics

| Today | After | Notes |
|---|---|---|
| `signer_pay_total{outcome=success\|insufficient_balance\|preflight_revert\|send_error\|receipt_timeout\|on_chain_failure\|unexpected_error, wallet_index}` | `signer_sign_total{outcome=success\|wallet_unknown\|signing_error, wallet_index}` | Most failure modes disappear (no submission = no `send_error`, `receipt_timeout`, etc). Dashboard panel "Signer outcomes" gets simpler. |
| `signer_pay_duration_seconds{outcome}` | `signer_sign_duration_seconds{outcome}` | Histogram. **Should be sub-millisecond** — pure EIP-712 signing under Permit2, no RPC round-trip on the hot path. Adjust buckets: `(0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1)`. |
| `signer_tx_submit_seconds` | **dropped** | No tx submission on the hot path. |
| `signer_tx_receipt_wait_seconds` | **dropped** | No receipts on the hot path. |
| `signer_tx_gas_used` | **dropped** | Facilitator pays gas. |
| `signer_wallet_sbc_balance_units{wallet_index, address}` | unchanged | Still load-bearing for "do my wallets have enough SBC to back outstanding signed permits." |
| `signer_wallet_rusd_balance_wei{wallet_index, address}` | **dropped from dashboard** | The payer wallet pays zero gas after the one-time approval. RUSD balance is irrelevant. Drop from main dashboard. |
| (new) | `signer_permit2_approval_total{outcome=already_approved\|approved\|approve_failed, wallet_index}` | Counter, **boot-time only**. Tracks the one-time `SBC.approve(Permit2)` per wallet. If `approve_failed` is non-zero post-boot, the signer can't serve that wallet. |
| (new) | `signer_permit2_approval_gas_used{wallet_index}` | Gauge, boot-time. Records gas burned by the one-time approval. M1 baseline: ~115k on Radius. Lets us alert on drift if the SBC contract's logic changes. |
| ~~`signer_nonce_read_*`~~ | **not added** | Removed from the design — Permit2 uses random bitmap nonces; no on-chain read required at sign time. (The EIP-2612 plan needed this; Permit2 doesn't.) |
| ~~`signer_facilitator_spender_cache_*`~~ | **not added** | Removed from the design — Permit2's spender is the hardcoded `x402ExactPermit2Proxy`. No `/supported` cache to track. |

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
| 4 | **Facilitator egress latency/failure** | NetworkChaos on egress from `url-shortener` to `facilitator.testnet.radiustech.xyz` | `payment_facilitator_call_duration_seconds{op=verify}` p95 spikes; `payment_facilitator_total{outcome="facilitator_unreachable"}` increments; `shorten_5xx_rate` rises proportionally. App pod itself stays healthy — the failure is correctly attributed to the external dependency. |
| 5 | **Signer pod kill** (enabled by D3) | `radius-signer` deployment | k6 sees `sign_success_rate` drop to 0 during signer restart. Distinguishes "auth service down" from "app down" or "facilitator down" — three different chaos signatures, three different root causes. |
| 6 | ~~Signer → Radius RPC latency (EIP-2612 nonce read)~~ | n/a | **Dropped.** Permit2 random nonces removed the signer's hot-path RPC dependency entirely. Only RPC contact is at boot for the one-time `SBC.approve(Permit2)`, which isn't a meaningful chaos surface. Replaced by experiment #6' below. |
| 6' | **Signer boot-time RPC dependency** (replacement) | NetworkChaos against `radius-signer` → Radius RPC, but only during `kubectl rollout restart deploy/radius-signer` | New wallet's approval must complete for the wallet to serve payments. Validates that signer boot is bounded and resilient to slow RPC. Less critical than the dropped #6, but real for cold-start scenarios. |

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

1. ~~**Asset transfer method.**~~ **Resolved (M0+M1, 2026-05-29)**: Radius first-party facilitator uses `assetTransferMethod: "permit2"` (Uniswap Permit2 + `x402ExactPermit2Proxy`, atomic settlement). The earlier-considered Stablecoin.xyz path (EIP-2612, two-tx settlement) was discarded after the Radius facilitator was discovered. See §15.
2. ~~**Which facilitator to default to.**~~ **Resolved**: `facilitator.testnet.radiustech.xyz` (testnet) / `facilitator.radiustech.xyz` (mainnet). Radius first-party, recommended by their docs, atomic Permit2 settlement, gas-sponsored. Override via `FACILITATOR_URL` env var so we can swap without redeploying app code. Fallback option (kept for chaos-demo robustness): Stablecoin.xyz with `assetTransferMethod: "erc2612"` — would require running the EIP-2612 code path which we're not building, so the realistic fallback is "live with the outage."
3. ~~**Nonce read caching.**~~ **Resolved**: not applicable to Permit2 (random bitmap nonces). The signer doesn't read nonces from the chain on the hot path at all.
4. **Loadgen base64 helper.** k6 JS has no native base64 — use [k6's `encoding` module](https://k6.io/docs/javascript-api/k6-encoding/) (`encoding.b64encode`). No xk6 extension needed.
5. **v-value normalization on signature output.** `eth-account` local signing produces v=27|28, but `references/gotchas.md#9` flags this as a silent-failure mode for any signature from external sources. Defensive normalization in the signer is one line — add it from day 1. (Already in the spike, see §15.)
6. **`/settle` failure mode characterization.** M1 never observed a real `/settle` failure — the facilitator's idempotency cache eats everything that would otherwise be a settle-fail. M3 should still build the `facilitator_settle_failed` outcome bucket; just expect it to be near-zero in steady state and only fire under chaos. Worth recording in `LEARNINGS.md` once observed.
7. **One-time approval failure recovery.** If `SBC.approve(Permit2)` fails at signer boot for a wallet (e.g. RPC down), how do we handle that wallet? Options: (a) skip the wallet and serve from the others, (b) hard-fail the signer pod. M2 default: (a) with a Prometheus alert on `signer_permit2_approval_total{outcome="approve_failed"}`. Revisit if (a) creates load-distribution problems under VUS-bound loadgen.

## 11. Out of scope

- Self-hosting the facilitator. Deferred — Radius's hosted facilitator is fine for a portfolio project. Revisit only if it goes down during a demo.
- Removing the custom k6 image. Possible follow-on once xk6-ethereum is unused on the hot path. Not blocking.
- The "agent pays" demo flow (curl + CLI signer). Out of scope for the migration itself but cheap to add later because the signer's contract is now clean.

## 12. Implementation sequencing

Phases here are *implementation* phases (within this migration), not the project's Phase 6 / 7 phases.

| Step | Deliverable | Bake-out |
|---|---|---|
| **M1** ✅ done 2026-05-29 | Spike: stand-alone Python script proves **Permit2 `PermitWitnessTransferFrom` signing + Radius facilitator `/verify`+`/settle`** on Radius testnet. Verify Permit2 domain values, confirm one-time `SBC.approve(Permit2)` bootstrap, capture facilitator HTTP responses for happy path + bad-sig + replay. | M1 settlement tx `0x9714e90a…dcea926` on testnet. See §15 + scratch/x402-m1/. |
| **M2** | Repurpose `signer/` to `/sign-permit2`. Add one-time `SBC.approve(Permit2)` bootstrap at startup (gas-estimate driven, per-wallet, surface via `signer_permit2_approval_total`). Add v-value normalization. Keep `/health`, `/wallets`. Delete `/pay`. | Unit tests: signature validity (recover address from sig, compare to wallet), v-normalization for v=0\|1 inputs, idempotency of approval bootstrap (re-running boot doesn't re-approve). |
| **M3** | Rewrite `app/payment.py` as facilitator client. Update `main.py` to use `PAYMENT-SIGNATURE` header with `permit2Authorization`. Schema migration for `tx_hash → settlement_tx_hash`. | docker-compose locally: full payment flow end-to-end against testnet facilitator. |
| **M4** | Update `loadgen.js`: drop tx submission, add `/sign-permit2` call + base64 header. Update thresholds and metric names. | k6 run in `url-shortener-dev` namespace. |
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

## 14. M0 recon (2026-05-29)

Recon step before M1 — confirm the Stablecoin.xyz facilitator is reachable, supports Radius testnet (chain 72344), and capture the exact response shape. Local-only, no commits to production code, no cluster activity. Time on task: ~5 minutes.

### 14.1 /health

```
$ curl https://x402.stablecoin.xyz/health
{"status":"ok","service":"SBC x402 Facilitator"}
HTTP 200 | 193ms
```

### 14.2 /supported (raw)

```json
{
  "kinds": [
    {"x402Version":2,"scheme":"exact","network":"eip155:8453",   "extra":{"assetTransferMethod":"erc2612","name":"Stable Coin","version":"1"}},
    {"x402Version":1,"scheme":"exact","network":"eip155:8453",   "extra":{"assetTransferMethod":"erc2612","name":"Stable Coin","version":"1"}},
    {"x402Version":2,"scheme":"exact","network":"eip155:8453",   "extra":{"assetTransferMethod":"erc2612","name":"USD Coin","version":"2"}},
    {"x402Version":1,"scheme":"exact","network":"eip155:8453",   "extra":{"assetTransferMethod":"erc2612","name":"USD Coin","version":"2"}},
    {"x402Version":2,"scheme":"exact","network":"eip155:84532",  "extra":{"assetTransferMethod":"erc2612","name":"Stable Coin","version":"1"}},
    {"x402Version":1,"scheme":"exact","network":"eip155:84532",  "extra":{"assetTransferMethod":"erc2612","name":"Stable Coin","version":"1"}},
    {"x402Version":2,"scheme":"exact","network":"eip155:84532",  "extra":{"assetTransferMethod":"erc2612","name":"USD Coin","version":"2"}},
    {"x402Version":1,"scheme":"exact","network":"eip155:84532",  "extra":{"assetTransferMethod":"erc2612","name":"USD Coin","version":"2"}},
    {"x402Version":2,"scheme":"exact","network":"eip155:723487", "extra":{"assetTransferMethod":"erc2612","name":"Stable Coin","version":"1"}},
    {"x402Version":1,"scheme":"exact","network":"eip155:723487", "extra":{"assetTransferMethod":"erc2612","name":"Stable Coin","version":"1"}},
    {"x402Version":2,"scheme":"exact","network":"eip155:72344",  "extra":{"assetTransferMethod":"erc2612","name":"Stable Coin","version":"1"}},
    {"x402Version":1,"scheme":"exact","network":"eip155:72344",  "extra":{"assetTransferMethod":"erc2612","name":"Stable Coin","version":"1"}},
    {"x402Version":2,"scheme":"exact","network":"solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp","extra":{"assetTransferMethod":"delegated-spl","name":"SBC","version":"1"}},
    {"x402Version":1,"scheme":"exact","network":"solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp","extra":{"assetTransferMethod":"delegated-spl","name":"SBC","version":"1"}}
  ],
  "extensions": [],
  "signers": {
    "eip155:*":  ["0xdeE710bB6a3b652C35B5cB74E7bdb03EE1F641E6"],
    "solana:*":  ["2mSjKVjzRGXcipq3DdJCijbepugfNSJCN1yVN2tgdw5K"]
  }
}
```

HTTP 200, 113ms.

### 14.3 Findings

> **Note — superseded direction:** these findings were captured against Stablecoin.xyz before the M1 spike discovered the Radius first-party facilitator. They remain factually correct about Stablecoin.xyz but the design no longer targets it as the primary. See §15 for the pivot record.

1. **Chain 72344 (Radius testnet) is supported.** Two `kinds[]` entries (x402Version 1 and 2), scheme `exact`, asset transfer method `erc2612`, SBC token (`name: "Stable Coin", version: "1"`).
2. **Domain values match prior assumptions** (relevant for the EIP-2612 path on Stablecoin.xyz; the Radius/Permit2 path uses Permit2's own domain instead — see §4.2 + §15).
3. **`assetTransferMethod` is `erc2612`** on Stablecoin.xyz. The Radius first-party facilitator uses `permit2` instead (§15).
4. **Stablecoin.xyz settlement wallet:** `0xdeE710bB6a3b652C35B5cB74E7bdb03EE1F641E6`. Captured here for historical reference; unused in the Permit2 path.
5. **Other supported networks** (informational): Base mainnet `eip155:8453`, Base sepolia `eip155:84532`, Radius mainnet `eip155:723487`, Solana mainnet. SBC and USDC on Base; SBC-only on Radius and Solana.
6. **Not in the response:** no `verify`/`settle` endpoint discovery, no per-call fee schedule, no rate-limit headers.

## 15. M1 spike + pivot to Radius Permit2 (2026-05-29)

M1 started against Stablecoin.xyz/EIP-2612 (the path §14 had set up). Mid-M1, the live Radius x402 integration docs at `docs.radiustech.xyz/developer-resources/x402-integration.md` revealed a first-party Radius facilitator at `facilitator.testnet.radiustech.xyz` using Permit2 (atomic settlement, single tx, gas-sponsored) that wasn't listed in the radius-dev skill's `references/micropayments.md`. After confirming via `/supported` that it advertises `assetTransferMethod: "permit2"` for chain 72344, we pivoted to that path. The EIP-2612 spike that proved the end-to-end mechanics on Stablecoin.xyz is preserved as historical reference (`scratch/x402-m1/spike_eip2612_stablecoinxyz.py` + `FINDINGS_eip2612_stablecoinxyz.md`).

### 15.1 Why pivot

| | Stablecoin.xyz / EIP-2612 | Radius first-party / Permit2 |
|---|---|---|
| Settlement | Two on-chain txs (`permit()` + `transferFrom()`) | **One atomic tx** via `x402ExactPermit2Proxy.settle` |
| Signer hot-path RPC | Required (`token.nonces(owner)` per request) | **None** — Permit2 nonces are client-side random |
| Spender discovery | Required (`/supported.signers["eip155:*"][0]`, can rotate) | **Hardcoded** (canonical `x402ExactPermit2Proxy` at `0x402085…20001`) |
| Operator | Third-party | Radius first-party (recommended in their docs) |
| One-time bootstrap per wallet | None | `SBC.approve(Permit2, MAX)` once, paid in SBC via Turnstile |

Net trade: the Permit2 path adds one-time approval (~115k gas in SBC, once per wallet ever) and removes the hot-path RPC dependency + the facilitator-rotation failure mode. Clean engineering win for our chaos surface.

### 15.2 Spike result — happy path

Settlement tx: `0x9714e90aeca97b5cc91bd8ac44616b6e39a75ae133f115a827c1e8b94dcea926`. Payer balance debited exactly 1000 raw (0.001 SBC). Merchant credited exactly 1000.

- `/health` → `{"status":"ok","pool":{"total":100,"idle":100,"busy":0,"utilization":"0.0%"}}` — note the wallet-pool gauge, useful operational signal for M5.
- `/supported` → one kind: `{network: "eip155:72344", scheme: "exact", assetTransferMethod: "permit2", name: "Stable Coin", version: "1"}` plus `extensions: ["eip2612GasSponsoring"]` and `signers: {}` (empty — Permit2 spender is hardcoded).
- `/verify` accepted → `{"isValid": true, "payer": "0xfd4dc7…"}`. Leaner shape than Stablecoin.xyz (no `remainingSeconds`, no `invalidReason: null`).
- `/settle` accepted → `{"success": true, "transaction": "0x9714…", "network": "eip155:72344", "payer": "0xfd4dc7…"}`.

### 15.3 Failure-mode captures

| Mode | Result | Implication |
|---|---|---|
| `--bad-sig` | `/verify` HTTP 200 + `{"isValid": false, "invalidReason": "Invalid signature", "payer": "0xfd4dc7…"}` | **Validity is in the body, not the HTTP status.** Map `isValid: false` → `signature_invalid` outcome. `invalidReason` is free-form human prose — don't pivot Prometheus labels off it. |
| `--replay` | `/settle` HTTP 200 + `{"success": true, "transaction": "0x9714…" (same as original)}` | The facilitator returns the cached prior settlement. No second on-chain call. **The app's `urls.settlement_tx_hash UNIQUE` constraint is the load-bearing replay guard**, not an expected on-chain revert. |
| Real `/settle` failure | Not observed | The facilitator's idempotency layer ate everything. Build `facilitator_settle_failed` bucket anyway; expect near-zero in steady state, monitor under chaos. |

### 15.4 Load-bearing facts pinned by M1

- **Permit2 domain has no `version` field.** `{name: "Permit2", chainId: 72344, verifyingContract: 0x000…0022D…3A}`. `eth-account.encode_typed_data` handles a domain dict without `version` correctly (produces the right `EIP712Domain` typehash matching Permit2's contract).
- **`SBC.approve()` on Radius uses ~115k gas**, not the vanilla ERC-20 ~46k. Likely Turnstile-related state mutations. M2 signer **must gas-estimate**, not hardcode (we OOG'd at 100k limit on the first attempt).
- **The Permit2 `spender` is the proxy, not the facilitator.** Payers sign for `x402ExactPermit2Proxy` (`0x402085c248EeA27D92E8b30b2C58ed07f9E20001`). The proxy validates the witness and constrains the destination, so the facilitator can't redirect funds — security property worth a sentence in any blog post.
- **No `/supported` discovery loop required.** Radius's `signers: {}` is empty; the spender is a constant.

### 15.5 Artifacts

- `scratch/x402-m1/spike.py` — Permit2 spike (current).
- `scratch/x402-m1/FINDINGS.md` — Permit2 findings + design-doc diff list (this section folded most of it back).
- `scratch/x402-m1/responses.jsonl` — every HTTP exchange (both spikes). M3 references this for outcome-bucket labels.
- `scratch/x402-m1/spike_eip2612_stablecoinxyz.py` + `FINDINGS_eip2612_stablecoinxyz.md` — preserved as historical reference for the EIP-2612 path we discarded.

The scratch dir itself is non-production; expected to be `git rm -r`'d at M2 once the real signer change lands and the spike's role is over.

