#!/usr/bin/env bash
set -euo pipefail

FAUCET_URL="${FAUCET_URL:-https://testnet.radiustech.xyz/api/v1/faucet}"
RPC_URL="${RPC_URL:-https://rpc.testnet.radiustech.xyz}"
SBC_CONTRACT="0x33ad9e4BD16B69B5BFdED37D8B5D9fF9aba014Fb"
ADDRESS="${1:-}"
TOKEN="${TOKEN:-SBC}"

if [[ -z "$ADDRESS" ]]; then
  echo "Usage: $0 <wallet-address>"
  echo "  FAUCET_URL=... $0 0x..."
  exit 1
fi

# Validate address format
if [[ ! "$ADDRESS" =~ ^0x[a-fA-F0-9]{40}$ ]]; then
  echo "ERROR: Invalid address format: $ADDRESS" >&2
  exit 1
fi

echo "=== Checking rate limit for ${ADDRESS} ==="
STATUS=$(curl -s "${FAUCET_URL}/status/${ADDRESS}?token=${TOKEN}")
echo "Status response: $STATUS"

RATE_LIMITED=$(echo "$STATUS" | jq -r '.rate_limited')
if [[ "$RATE_LIMITED" == "true" ]]; then
  RETRY_MS=$(echo "$STATUS" | jq -r '.retry_after_ms // 60000')
  echo "Rate limited. Retry after ${RETRY_MS}ms" >&2
  exit 1
fi

WALLET_KEY="${WALLET_KEY:-}"

drip() {  # $1 = optional ,"signature":"0x..." fragment
  curl -s -X POST "${FAUCET_URL}/drip" \
    -H "Content-Type: application/json" \
    -d "{\"address\":\"${ADDRESS}\",\"token\":\"${TOKEN}\"${1:-}}"
}

# Errors are now objects: .error.code (older API returned a bare string).
err_code() { echo "$1" | jq -r '(.error.code // .error) // empty'; }

echo ""
echo "=== Requesting ${TOKEN} drip ==="
DRIP=$(drip)
echo "Drip response: $DRIP"
CODE=$(err_code "$DRIP")

# Faucet now requires a signed challenge (EIP-191 personal_sign). We hold the
# key, so sign the challenge locally with cast and resubmit — the key never
# leaves the machine; only the signature is sent.
if [[ "$CODE" == "signature_required" ]]; then
  CHALLENGE=$(echo "$DRIP" | jq -r '(.error.details.challenge // .details.challenge) // empty')
  if [[ -z "$WALLET_KEY" ]]; then
    echo "ERROR: faucet requires a signed challenge but WALLET_KEY is not set." >&2
    echo "Run via scripts/fund-loadgen-wallets.sh (exports the key), or use the web faucet: https://testnet.radiustech.xyz/wallet" >&2
    exit 1
  fi
  echo "Signing challenge (EIP-191): ${CHALLENGE}"
  SIG=$(cast wallet sign --private-key "0x${WALLET_KEY#0x}" "${CHALLENGE}")
  DRIP=$(drip ",\"signature\":\"${SIG}\"")
  echo "Drip (signed) response: $DRIP"
  CODE=$(err_code "$DRIP")
fi

if [[ "$CODE" == "rate_limited" ]]; then
  RETRY_MS=$(echo "$DRIP" | jq -r '(.error.retry_after_ms // .retry_after_ms) // 60000')
  echo "Rate limited. Retry after ${RETRY_MS}ms" >&2
  exit 1
fi

SUCCESS=$(echo "$DRIP" | jq -r '.success // empty')
if [[ "$SUCCESS" != "true" ]]; then
  echo "Drip failed: ${CODE:-unknown} — $(echo "$DRIP" | jq -r '(.error.message // .message) // empty')" >&2
  exit 1
fi

TX_HASH=$(echo "$DRIP" | jq -r '.tx_hash')
echo ""
echo "Funded. tx_hash: ${TX_HASH}"

echo ""
echo "=== Verifying on-chain balance ==="
# cast call returns decimal e.g. "500000 [5e5]" — extract first word, divide by 1e6
BALANCE_RAW=$(cast call "$SBC_CONTRACT" \
  "balanceOf(address)(uint256)" "$ADDRESS" \
  --rpc-url "$RPC_URL")
echo "Balance raw: $BALANCE_RAW"
BALANCE_UNITS=$(echo "$BALANCE_RAW" | awk '{print $1}')
echo "SBC balance: $(echo "scale=6; $BALANCE_UNITS / 1000000" | bc) SBC"
