#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
TEST_URL="${TEST_URL:-https://example.com}"
TX_HASH="${TX_HASH:-}"

echo "=== Payment info ==="
curl -s "${BASE_URL}/payment-info" | jq .
echo

echo "=== Step 1: /shorten without tx_hash (expect 402 when payment enabled) ==="
curl -s -o /tmp/shorten_no_tx.json -w "HTTP %{http_code}\n" \
  -X POST "${BASE_URL}/shorten" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"${TEST_URL}\"}"
cat /tmp/shorten_no_tx.json | jq .
echo

if [[ -z "${TX_HASH}" ]]; then
  echo "Set TX_HASH to continue payment verification and replay checks."
  echo "Example:"
  echo "  TX_HASH=0x... ./scripts/test-payment-flow.sh"
  exit 0
fi

echo "=== Step 2: /shorten with tx_hash (expect 201 on first successful payment) ==="
curl -s -o /tmp/shorten_with_tx.json -w "HTTP %{http_code}\n" \
  -X POST "${BASE_URL}/shorten" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"${TEST_URL}\",\"tx_hash\":\"${TX_HASH}\"}"
cat /tmp/shorten_with_tx.json | jq .
echo

echo "=== Step 3: replay same tx_hash (expect 409) ==="
curl -s -o /tmp/shorten_replay.json -w "HTTP %{http_code}\n" \
  -X POST "${BASE_URL}/shorten" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"${TEST_URL}-replay\",\"tx_hash\":\"${TX_HASH}\"}"
cat /tmp/shorten_replay.json | jq .
