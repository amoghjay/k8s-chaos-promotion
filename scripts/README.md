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

## fund-test-wallet.sh

Address-only faucet utility. Drips SBC to a wallet address on Radius testnet, then verifies
the on-chain balance with `cast call`. **No private key required** — unsigned flow only.
If the faucet re-enables `signature_required`, the script stops and prints the web faucet URL.

Prerequisites: `curl`, `jq`, `cast` (Foundry), `bc`

Usage:
```bash
# Fund the SERVICE_WALLET from the staging secret
SERVICE_WALLET=$(kubectl get secret url-shortener-secrets -n url-shortener-staging \
  -o jsonpath='{.data.SERVICE_WALLET_ADDRESS}' | base64 -d)
./scripts/fund-test-wallet.sh "$SERVICE_WALLET"

# Custom faucet endpoint
FAUCET_URL=https://testnet.radiustech.xyz/api/v1/faucet ./scripts/fund-test-wallet.sh 0x...
```

Expected output:
- `Drip response: {"success":true,"tx_hash":"0x..."}` — faucet confirmed
- `SBC balance: 0.500000 SBC` — on-chain ground truth via `cast call balanceOf`

Error cases:
- `signature_required` — exits with web faucet URL; script does not hold SERVICE_WALLET key
- `rate_limited` — exits with retry_after_ms; wait and retry (60 req/60s limit per address)

---

## loadgen.js

k6 load generator for continuous payment traffic. Designed to run as a Kubernetes Job
inside `url-shortener-staging` so it's subject to the same Chaos Mesh network experiments
as the app.

**Address-only faucet flow**: calls `POST /drip` with `SERVICE_WALLET_ADDRESS` and uses the
returned `tx_hash` directly as payment — no Ethereum signing needed.

Per-VU iteration:
1. `POST /drip` → get `tx_hash`  (falls back to no-payment mode on `signature_required` or `rate_limited`)
2. `POST /shorten` with `tx_hash` → expect 201
3. `GET /{code}` with `redirects: 0` → expect 302

Custom metrics tracked:
| Metric | Description |
|--------|-------------|
| `payment_success_rate` | Rate of 201 on /shorten |
| `payment_402_rate` | Rate of 402 (faucet skipped or payment disabled) |
| `payment_409_rate` | Rate of 409 (replay — should be 0) |
| `redirect_ok_rate` | Rate of 302 on /{code} |
| `faucet_ok_rate` | Rate of successful faucet drips |

Phase 5 thresholds (baseline):
- `http_req_duration p(95) < 500ms`
- `http_req_failed rate < 0.05`
- `payment_success_rate rate > 0.95`

Run locally (requires k6):
```bash
SERVICE_WALLET=$(kubectl get secret url-shortener-secrets -n url-shortener-staging \
  -o jsonpath='{.data.SERVICE_WALLET_ADDRESS}' | base64 -d)

BASE_URL=http://localhost:8000 \
SERVICE_WALLET_ADDRESS="$SERVICE_WALLET" \
VUS=2 DURATION=1m \
k6 run scripts/loadgen.js
```

Run as k8s Job (Phase 5+):
```bash
kubectl apply -f kubernetes/jobs/loadgen-job.yaml
kubectl logs -f job/loadgen -n url-shortener-staging
```

Rate limit note: VUS=2 at ~3s/iteration ≈ 40 req/min — within the 60 req/60s faucet limit.
VUS=5 pushes ~100 req/min; some VUs will hit `rate_limited` and fall back to no-payment mode
(expected, measured via `payment_402_rate` in Grafana).
