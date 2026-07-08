"""x402 facilitator client: decode the client's PAYMENT-SIGNATURE header, POST
it to the facilitator's /verify then /settle, and return a SettlementResult.

paymentRequirements is rebuilt from our own env config, never echoed from the
client, so a client can't negotiate a cheaper price or a different recipient.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from enum import Enum

import httpx
from prometheus_client import Histogram

logger = logging.getLogger("payment")

# Per-endpoint timing separates "facilitator slow" from "app slow" during chaos.
FACILITATOR_CALL_DURATION = Histogram(
    "payment_facilitator_call_duration_seconds",
    "Duration of each facilitator HTTP call (verify, settle), labelled by op.",
    ["op"],
)

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


# Two near-identical shapes on purpose: _payment_requirements() is the body
# sent to /verify and /settle; payment_required_descriptor() (the 402 header)
# additionally carries extra.assetTransferMethod so the client knows to sign
# Permit2, not EIP-2612.
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
    accepts_entry = _payment_requirements() | {
        "extra": {
            "assetTransferMethod": "permit2",
            "name": "Stable Coin",
            "version": "1",
        }
    }
    return {
        "x402Version": 2,
        "error": "PAYMENT-SIGNATURE header is required",
        "resource": {
            "url": resource_url,
            "description": "Access to /shorten",
            "mimeType": "application/json",
        },
        "accepts": [accepts_entry],
    }


def encode_header(payload: dict) -> str:
    """Base64-encode a JSON payload for an x402 header."""
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


async def _post_facilitator(
    client: httpx.AsyncClient, endpoint: str, body: dict
) -> tuple[dict | None, SettlementResult | None]:
    """POST to facilitator/{endpoint}. Returns (parsed_body, None) on HTTP 2xx,
    or (None, unreachable_result) on transport error / 4xx-5xx.

    The facilitator signals validity in the response body (isValid / success),
    not the HTTP status — a 4xx/5xx is operational, never a payment rejection.
    """
    op = endpoint.lstrip("/")
    try:
        with FACILITATOR_CALL_DURATION.labels(op=op).time():
            resp = await client.post(
                f"{FACILITATOR_URL}{endpoint}", json=body, timeout=FACILITATOR_TIMEOUT_S
            )
    except httpx.HTTPError as exc:
        logger.warning("facilitator %s unreachable: %s", endpoint, exc)
        return None, SettlementResult(
            SettlementStatus.FACILITATOR_UNREACHABLE, f"facilitator {endpoint}: {exc}"
        )
    if resp.status_code >= 400:
        return None, SettlementResult(
            SettlementStatus.FACILITATOR_UNREACHABLE,
            f"facilitator {endpoint} returned HTTP {resp.status_code}: {resp.text[:200]}",
        )
    return resp.json(), None


async def settle_payment(
    payment_signature_header: str, client: httpx.AsyncClient
) -> SettlementResult:
    """Decode the client header, verify and settle with the facilitator."""
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

    verify_body, err = await _post_facilitator(client, "/verify", body)
    if err:
        return err
    if not verify_body.get("isValid", False):
        # invalidReason is free-form prose — bucket signature failures;
        # everything else is a generic verify failure.
        reason = (verify_body.get("invalidReason") or "").lower()
        status = (
            SettlementStatus.SIGNATURE_INVALID if "signature" in reason
            else SettlementStatus.FACILITATOR_VERIFY_FAILED
        )
        return SettlementResult(
            status,
            verify_body.get("invalidReason") or verify_body.get("invalidMessage", "verify rejected"),
            payer=verify_body.get("payer", ""),
        )

    settle_body, err = await _post_facilitator(client, "/settle", body)
    if err:
        return err
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


def settled_response_header(settlement_tx_hash: str, payer: str) -> str:
    """Base64-encode the PAYMENT-RESPONSE header for a successful settlement."""
    return encode_header({
        "success": True,
        "transaction": settlement_tx_hash,
        "payer": payer,
        "network": NETWORK_CAIP2,
    })
