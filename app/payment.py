"""x402 facilitator client for the URL shortener.

The app never touches the chain. We:
  1. Decode the base64 PAYMENT-SIGNATURE header the client sent.
  2. POST {x402Version, paymentPayload, paymentRequirements} to facilitator /verify.
  3. On isValid=true, POST the same body to /settle.
  4. Return a SettlementResult — main.py writes the settlement tx hash to Postgres.

paymentRequirements is rebuilt from our own env config, NOT echoed from the client,
so a client can't talk us into a cheaper price or a different recipient.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from enum import Enum

import httpx

logger = logging.getLogger("payment")

# ---- Config ---------------------------------------------------------------
FACILITATOR_URL = os.getenv(
    "FACILITATOR_URL", "https://facilitator.testnet.radiustech.xyz"
).rstrip("/")
SERVICE_WALLET = os.getenv("SERVICE_WALLET_ADDRESS", "").strip()
SBC_CONTRACT = os.getenv(
    "SBC_CONTRACT_ADDRESS", "0x33ad9e4bd16b69b5bfded37d8b5d9ff9aba014fb"
).lower()
SHORTEN_FEE = int(os.getenv("SHORTEN_FEE", "1000"))  # raw 6-decimal units
RADIUS_CHAIN_ID = int(os.getenv("RADIUS_CHAIN_ID", "72344"))
NETWORK_CAIP2 = os.getenv("NETWORK_CAIP2", f"eip155:{RADIUS_CHAIN_ID}")
FACILITATOR_TIMEOUT_S = float(os.getenv("FACILITATOR_TIMEOUT_SECONDS", "30"))
MAX_TIMEOUT_SECONDS = int(os.getenv("PAYMENT_MAX_TIMEOUT_SECONDS", "300"))


# ---- Result types ---------------------------------------------------------
class SettlementStatus(Enum):
    SETTLED = "settled"
    SIGNATURE_INVALID = "signature_invalid"
    FACILITATOR_VERIFY_FAILED = "facilitator_verify_failed"
    FACILITATOR_SETTLE_FAILED = "facilitator_settle_failed"
    FACILITATOR_UNREACHABLE = "facilitator_unreachable"
    HEADER_MALFORMED = "header_malformed"


@dataclass
class SettlementResult:
    status: SettlementStatus
    message: str
    settlement_tx_hash: str = ""
    payer: str = ""


# ---- Server-side payment requirements (NOT from client input) -------------
def _payment_requirements() -> dict:
    return {
        "scheme": "exact",
        "network": NETWORK_CAIP2,
        "amount": str(SHORTEN_FEE),
        "asset": SBC_CONTRACT,
        "payTo": SERVICE_WALLET,
        "maxTimeoutSeconds": MAX_TIMEOUT_SECONDS,
        "extra": {"name": "Stable Coin", "version": "1"},
    }


def payment_required_descriptor(resource_url: str) -> dict:
    """The dict that gets base64'd into the PAYMENT-REQUIRED response header.

    Sent on 402 responses so the client knows what payment shape we'll accept.
    """
    return {
        "x402Version": 2,
        "error": "PAYMENT-SIGNATURE header is required",
        "resource": {
            "url": resource_url,
            "description": "Access to /shorten",
            "mimeType": "application/json",
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": NETWORK_CAIP2,
                "amount": str(SHORTEN_FEE),
                "asset": SBC_CONTRACT,
                "payTo": SERVICE_WALLET,
                "maxTimeoutSeconds": MAX_TIMEOUT_SECONDS,
                "extra": {
                    "assetTransferMethod": "permit2",
                    "name": "Stable Coin",
                    "version": "1",
                },
            }
        ],
    }


def encode_header(payload: dict) -> str:
    """Base64-encode a JSON payload for an x402 header (PAYMENT-REQUIRED / PAYMENT-RESPONSE)."""
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


# ---- Core settlement -----------------------------------------------------
async def settle_payment(
    payment_signature_header: str, client: httpx.AsyncClient
) -> SettlementResult:
    """Decode the client header, verify and settle with the facilitator."""
    # 1. Decode the header (base64 → JSON).
    try:
        decoded = base64.b64decode(payment_signature_header, validate=True)
        payment_payload = json.loads(decoded)
    except Exception as exc:
        logger.warning("PAYMENT-SIGNATURE header malformed: %s", exc)
        return SettlementResult(
            SettlementStatus.HEADER_MALFORMED,
            f"PAYMENT-SIGNATURE header could not be decoded: {exc}",
        )

    body = {
        "x402Version": 2,
        "paymentPayload": payment_payload,
        "paymentRequirements": _payment_requirements(),
    }

    # 2. /verify.
    try:
        verify_resp = await client.post(
            f"{FACILITATOR_URL}/verify", json=body, timeout=FACILITATOR_TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        logger.warning("facilitator /verify unreachable: %s", exc)
        return SettlementResult(
            SettlementStatus.FACILITATOR_UNREACHABLE, f"facilitator /verify: {exc}"
        )
    if verify_resp.status_code >= 400:
        return SettlementResult(
            SettlementStatus.FACILITATOR_UNREACHABLE,
            f"facilitator /verify returned HTTP {verify_resp.status_code}: {verify_resp.text[:200]}",
        )
    verify_body = verify_resp.json()
    if not verify_body.get("isValid", False):
        reason = (verify_body.get("invalidReason") or "").lower()
        # invalidReason is free-form prose (M1 finding) — coarse-bucket signature
        # failures and treat everything else as a generic verify failure.
        if "signature" in reason:
            status = SettlementStatus.SIGNATURE_INVALID
        else:
            status = SettlementStatus.FACILITATOR_VERIFY_FAILED
        return SettlementResult(
            status,
            verify_body.get("invalidReason") or verify_body.get("invalidMessage", "verify rejected"),
            payer=verify_body.get("payer", ""),
        )

    # 3. /settle.
    try:
        settle_resp = await client.post(
            f"{FACILITATOR_URL}/settle", json=body, timeout=FACILITATOR_TIMEOUT_S
        )
    except httpx.HTTPError as exc:
        logger.warning("facilitator /settle unreachable: %s", exc)
        return SettlementResult(
            SettlementStatus.FACILITATOR_UNREACHABLE, f"facilitator /settle: {exc}"
        )
    if settle_resp.status_code >= 400:
        return SettlementResult(
            SettlementStatus.FACILITATOR_UNREACHABLE,
            f"facilitator /settle returned HTTP {settle_resp.status_code}: {settle_resp.text[:200]}",
        )
    settle_body = settle_resp.json()
    if not settle_body.get("success", False):
        return SettlementResult(
            SettlementStatus.FACILITATOR_SETTLE_FAILED,
            settle_body.get("errorReason") or settle_body.get("errorMessage", "settle failed"),
            payer=settle_body.get("payer", ""),
        )

    return SettlementResult(
        SettlementStatus.SETTLED,
        "payment settled",
        settlement_tx_hash=settle_body.get("transaction", ""),
        payer=settle_body.get("payer", ""),
    )


def settled_response_header(result: SettlementResult, network: str = NETWORK_CAIP2) -> str:
    """Base64-encode the PAYMENT-RESPONSE header for a successful settlement."""
    return encode_header({
        "success": True,
        "transaction": result.settlement_tx_hash,
        "payer": result.payer,
        "network": network,
    })
