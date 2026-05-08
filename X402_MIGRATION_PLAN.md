# x402 Migration Plan for `chaos-promotion-project`

Last updated: May 8, 2026

## Goal

Evaluate whether the current Radius `tx_hash` payment flow should be replaced with a proper `x402` flow, and define a safe migration path if we choose to do it.

This document is intentionally a plan, not an implementation commitment.

## Current state

Today the app uses a custom two-step payment flow:

1. `POST /shorten` without `tx_hash` returns `402` plus payment instructions.
2. Client sends an SBC ERC-20 transfer on Radius testnet.
3. Client retries `POST /shorten` with `tx_hash`.
4. The app verifies the tx receipt and the ERC-20 `Transfer` event on-chain.

Relevant code:

- [app/main.py](./app/main.py)
- [app/payment.py](./app/payment.py)

The verifier in [app/payment.py](./app/payment.py) currently accepts a payment if:

- the receipt exists and `status == 1`
- `receipt.to == SBC_CONTRACT`
- the logs contain an SBC `Transfer` event
- the transfer recipient matches `SERVICE_WALLET_ADDRESS`
- the amount is at least `SHORTEN_FEE`

This flow works conceptually and has already been validated with manual `cast send` transactions.

## Why consider x402

`x402` is a stronger protocol story than the current custom `tx_hash` flow:

- it is an open payment standard rather than an app-specific convention
- it fits the HTTP `402 Payment Required` semantics directly
- it is more convincing for the "agentic payments" story
- it can move payment verification and settlement concerns into a facilitator
- it reduces blockchain-specific logic inside the resource server

For this project, that would make the final architecture feel more intentional and more modern than "send tx, then paste tx hash into an API".

## What current docs say

As of May 8, 2026:

- x402 docs explicitly list `Radius` and `Radius Testnet` in network/token support.
- Radius docs explicitly call out `eth_sendRawTransactionSync` as useful for latency-sensitive flows such as x402.
- The default `x402.org` facilitator docs still do not list Radius support.
- x402 docs say any EVM network can work if the facilitator supports it or if we self-host a facilitator.

Sources:

- https://docs.x402.org/core-concepts/network-and-token-support
- https://docs.x402.org/core-concepts/facilitator
- https://docs.radiustech.xyz/developer-resources/json-rpc-overview/
- https://docs.radiustech.xyz/developer-resources/json-rpc-api/

## Important architectural implication

Moving to x402 is not a small swap inside the current load generator.

It changes the payment model:

- Current model: direct ERC-20 transfer to the service wallet, then app verifies `tx_hash`
- x402 model: client sends an x402 payment payload, server verifies it locally or via facilitator, and settlement happens through the x402 mechanism

That means x402 is best treated as a new payment integration, not as a patch to the current `xk6-ethereum` loadgen.

## Recommendation

Use x402 as a **Phase 6.5 / follow-on architecture upgrade**, not as the immediate unblock for Phase 5.5.

Why:

- Phase 5.5 only needs reliable real payment traffic during chaos.
- We already know the current app payment verifier works with valid Radius transactions.
- The present blocker is the `xk6-ethereum` send path, not the business logic.
- x402 is better as a deliberate architecture improvement than as a rushed workaround.

## Recommended migration target

### Target shape

The long-term target should be:

1. `POST /shorten` returns a standards-based x402 `402 Payment Required` response.
2. A buyer/client creates an x402 payment payload for Radius testnet.
3. The app verifies and settles via a facilitator.
4. On success, the app returns `201` with the shortened URL and a settlement response header.

### Preferred deployment model

For this repo, the most realistic path is:

- keep the URL shortener as the resource server
- self-host a Radius-capable x402 facilitator
- use Radius testnet with its supported EVM/x402 path
- keep Grafana/Argo/Kargo/Chaos Mesh exactly as the resilience platform around it

Reason:

- the default public facilitator is not the safest assumption for Radius right now
- self-hosting keeps the story reproducible and under your control
- it also becomes a stronger project narrative

## Migration options

### Option A: Keep current `tx_hash` model

Pros:

- already implemented
- app verifier is simple
- easiest to reason about

Cons:

- non-standard
- weaker story than x402
- current k6 extension path is unreliable

### Option B: Replace only the loadgen tx submission

Shape:

- keep current app API
- replace `xk6-ethereum` with a signer service or another supported sender

Pros:

- quickest way to unblock chaos phases
- minimal app changes

Cons:

- still not x402
- leaves custom `tx_hash` API in place

### Option C: Full x402 migration

Pros:

- strongest architecture story
- aligns naturally with Radius and agentic payments
- reduces bespoke payment protocol logic in the app

Cons:

- bigger app/API change
- likely requires a self-hosted facilitator
- requires a new testing strategy and a new loadgen path

## Proposed phased plan

### Phase A: Unblock chaos load first

Goal:

- get a reliable payment-backed chaos load path working without relying on `xk6-ethereum`

Recommended implementation:

- keep current app verifier
- replace k6 transaction submission with a tiny signer/relay service

Outcome:

- Phase 5.5 and Phase 6 stay unblocked
- Grafana and chaos story continue

### Phase B: x402 spike

Goal:

- prove Radius+x402 works end-to-end in a small isolated prototype

Success criteria:

- one protected endpoint returns a valid x402 `402`
- a client can pay using Radius testnet
- the app gets a valid settlement result
- the endpoint responds successfully after payment

Deliverables:

- small spike app or branch
- notes on facilitator setup
- notes on Radius-specific config

### Phase C: App integration design

Goal:

- decide how to evolve the current API and data model

Design questions:

1. Does `/shorten` remain the main endpoint, or do we add an x402-native sibling route first?
2. What replaces the `tx_hash` uniqueness/replay protection model?
3. What payment metadata should be stored in Postgres after settlement?
4. Which Prometheus metrics should replace `payment_verifications_total{status=...}`?

### Phase D: Incremental rollout

Recommended rollout:

1. Add an x402-backed experimental route in `dev`
2. Keep the legacy `tx_hash` route in parallel
3. Prove x402 flow in `dev`
4. Move x402 route to `staging`
5. Update chaos loadgen to exercise the x402 route
6. Retire the old path only after the new one is stable

## What would need to change in this repo

### App layer

Likely changes:

- [app/main.py](./app/main.py): replace custom `tx_hash` contract with x402 request/response handling
- [app/payment.py](./app/payment.py): either remove or dramatically reduce direct receipt verification logic
- request/response models: add x402 header-aware behavior
- metrics: add x402 verify/settle metrics

### Data model

Likely changes:

- current replay protection is based on `tx_hash UNIQUE`
- x402 may need a different unique payment identity depending on the final settlement response shape

### Secrets/config

Likely additions:

- facilitator URL
- facilitator auth if used
- facilitator signer / sponsor wallet if self-hosted
- Radius RPC for facilitator

### Load generation

Current k6 script shape assumes:

- get or create `tx_hash`
- call `/shorten` with body `{ url, tx_hash }`

An x402 test client would instead need to:

1. call the protected resource
2. parse the x402 payment requirements
3. create a payment payload
4. retry with x402 payment headers

This likely means:

- either a new client tool instead of k6 for payment generation
- or a small sidecar/payment client service that k6 can drive

## Risks

### Risk 1: Facilitator support on Radius

The biggest unknown is not Radius itself, but the cleanest facilitator path for Radius.

Mitigation:

- assume self-hosted facilitator unless proven otherwise

### Risk 2: x402 load generation may not fit k6 naturally

k6 is great for HTTP load, but x402 client SDK support is stronger in TypeScript, Go, and Python than in k6's JS runtime.

Mitigation:

- keep k6 only as the outer load driver if helpful
- let a small service/client library handle x402 payload generation

### Risk 3: Scope creep

It is easy for x402 to turn from "better payment story" into a major re-platforming detour.

Mitigation:

- keep chaos/promotion milestones separate from x402 migration milestones

## Decision gate

Choose x402 only if these are true after the spike:

1. Radius+x402 works reliably in a self-hosted or clearly supported facilitator setup.
2. The new flow is easier or more robust than the current custom `tx_hash` flow plus signer-service fallback.
3. We can explain the architecture more clearly, not less clearly, in the final demo.

If the spike fails any of those, keep the custom verifier and replace only the loadgen tx sender.

## Suggested next steps

1. Keep Phase 5.5/6 moving with a signer-service fallback.
2. Create a small `x402-spike` branch or subdirectory.
3. Test a single Radius x402 payment flow outside the main app first.
4. Only then decide whether to migrate the real `/shorten` route.

## Bottom line

x402 is a strong option and likely a better final story than the current custom `tx_hash` protocol.

But it should be approached as:

- a planned architecture upgrade

not as:

- an emergency fix for the failing `xk6-ethereum` load generator.
