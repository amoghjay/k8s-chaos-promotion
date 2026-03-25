# Scripts

## test-payment-flow.sh
Manual end-to-end payment flow test. Covers:
- Step 1: POST /shorten without tx_hash → expect 402
- Step 2: POST /shorten with tx_hash → expect 201
- Step 3: Replay same tx_hash → expect 409 (replay protection)

Usage:
```bash
# Test 402 only
BASE_URL=http://localhost:8081 ./scripts/test-payment-flow.sh

# Full flow
TX_HASH=0x... BASE_URL=http://localhost:8081 ./scripts/test-payment-flow.sh
```

Validated manually on Mar 25, 2026 against url-shortener-dev with Radius testnet.

---

## TODO — Phase 6 (load generator)

Need a k6 script (`loadgen.js`) that automates continuous traffic during chaos experiments:

- Each virtual user: `cast send` SBC to service wallet → POST /shorten with tx_hash → GET /{code} (redirect)
- Parameterise: VU count, duration, BASE_URL, RPC_URL, PRIVATE_KEY, SERVICE_WALLET
- Track: p95 latency, error rate, 402 rate, 409 rate — these feed into Grafana chaos dashboard
- The private key used here needs a funded testnet wallet — separate from the service wallet
- Run as a k8s Job during chaos experiments (not external) so it's subject to the same network conditions
