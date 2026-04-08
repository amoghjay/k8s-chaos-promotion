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

echo ""
echo "=== Requesting ${TOKEN} drip (unsigned) ==="
DRIP=$(curl -s -X POST "${FAUCET_URL}/drip" \
  -H "Content-Type: application/json" \
  -d "{\"address\":\"${ADDRESS}\",\"token\":\"${TOKEN}\"}")
echo "Drip response: $DRIP"

ERROR=$(echo "$DRIP" | jq -r '.error // empty')

if [[ "$ERROR" == "signature_required" ]]; then
  echo ""
  echo "ERROR: Faucet requires a signature but this script uses address-only flow." >&2
  echo "We do not hold SERVICE_WALLET's private key in scripts." >&2
  echo "Use the web faucet instead: https://testnet.radiustech.xyz/wallet" >&2
  exit 1
fi

if [[ "$ERROR" == "rate_limited" ]]; then
  RETRY_MS=$(echo "$DRIP" | jq -r '.retry_after_ms // 60000')
  echo "Rate limited. Retry after ${RETRY_MS}ms" >&2
  exit 1
fi

SUCCESS=$(echo "$DRIP" | jq -r '.success')
if [[ "$SUCCESS" != "true" ]]; then
  echo "Drip failed: $ERROR — $(echo "$DRIP" | jq -r '.message // empty')" >&2
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
