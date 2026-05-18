#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FUND_SCRIPT="${ROOT_DIR}/scripts/fund-test-wallet.sh"
SLEEP_BETWEEN_S="${SLEEP_BETWEEN_S:-1}"

if [[ ! -x "${FUND_SCRIPT}" ]]; then
  echo "ERROR: Missing executable helper: ${FUND_SCRIPT}" >&2
  exit 1
fi

if ! command -v cast >/dev/null 2>&1; then
  echo "ERROR: cast is required to derive wallet addresses from WALLET_KEY_*" >&2
  exit 1
fi

fund_wallet() {
  local env_name="$1"
  local key="${!env_name:-}"

  if [[ -z "${key}" ]]; then
    echo "Skipping ${env_name} (not set)"
    return
  fi

  local normalized_key="${key#0x}"
  local address
  address="$(cast wallet address --private-key "0x${normalized_key}")"

  echo ""
  echo "=== Funding ${env_name} (${address}) ==="
  "${FUND_SCRIPT}" "${address}"
}

fund_wallet "WALLET_KEY_1"
sleep "${SLEEP_BETWEEN_S}"
fund_wallet "WALLET_KEY_2"
sleep "${SLEEP_BETWEEN_S}"
fund_wallet "WALLET_KEY_3"
