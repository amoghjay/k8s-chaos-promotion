"""
Payment verification module for Radius SBC payments.
Verifies ERC-20 Transfer events on-chain via standard EVM JSON-RPC.
"""

import os
import logging
import re
from dataclasses import dataclass
from enum import Enum

from web3 import Web3
from web3.exceptions import TransactionNotFound

logger = logging.getLogger("payment")

# ---------------------------------------------------------------------------
# Config (from environment)
# ---------------------------------------------------------------------------
RADIUS_RPC_URL = os.getenv("RADIUS_RPC_URL", "")
SERVICE_WALLET = os.getenv("SERVICE_WALLET_ADDRESS", "").lower()
SBC_CONTRACT = os.getenv("SBC_CONTRACT_ADDRESS", "0x33ad9e4bd16b69b5bfded37d8b5d9ff9aba014fb").lower()
SHORTEN_FEE = int(os.getenv("SHORTEN_FEE", "1000"))
RADIUS_CHAIN_ID = int(os.getenv("RADIUS_CHAIN_ID", "72344"))
RPC_TIMEOUT_SECONDS = int(os.getenv("RPC_TIMEOUT_SECONDS", "10"))

# ERC-20 Transfer event signature: Transfer(address,address,uint256)
TRANSFER_EVENT_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex().lower()
if not TRANSFER_EVENT_TOPIC.startswith("0x"):
    TRANSFER_EVENT_TOPIC = f"0x{TRANSFER_EVENT_TOPIC}"
TX_HASH_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")

# ---------------------------------------------------------------------------
# Web3 client (module-level, reused across requests)
# ---------------------------------------------------------------------------
w3: Web3 | None = None


def init_web3():
    """Initialize the Web3 client. Call once at app startup."""
    global w3

    if not RADIUS_RPC_URL:
        w3 = None
        logger.warning("RADIUS_RPC_URL not set; payment verification unavailable")
        return

    try:
        client = Web3(Web3.HTTPProvider(
            RADIUS_RPC_URL,
            request_kwargs={"timeout": RPC_TIMEOUT_SECONDS}
        ))
        if client.is_connected():
            w3 = client
            logger.info("Connected to Radius RPC")
            return
        w3 = None
        logger.error("Failed to connect to Radius RPC")
    except Exception as exc:
        w3 = None
        logger.error("Failed to initialize Radius RPC client: %s", exc)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
class PaymentStatus(Enum):
    SUCCESS = "success"
    TX_NOT_FOUND = "tx_not_found"
    WRONG_CONTRACT = "wrong_contract"
    WRONG_RECIPIENT = "wrong_recipient"
    INSUFFICIENT_AMOUNT = "insufficient_amount"
    NO_TRANSFER_EVENT = "no_transfer_event"
    RPC_ERROR = "rpc_error"


@dataclass
class PaymentResult:
    status: PaymentStatus
    message: str
    amount: int = 0        # actual SBC amount transferred
    sender: str = ""       # who paid


# ---------------------------------------------------------------------------
# Core verification
# ---------------------------------------------------------------------------
def verify_payment(tx_hash: str) -> PaymentResult:
    """
    Verify that tx_hash represents a valid SBC payment to the service wallet.

    Steps:
    1. Get the transaction receipt from Radius RPC
    2. Check the receipt exists (tx confirmed)
    3. Check the receipt's `to` field is the SBC contract address
    4. Find the Transfer event log in the receipt's logs
    5. Decode the Transfer event: from, to, value
    6. Verify `to` matches SERVICE_WALLET
    7. Verify `value` >= SHORTEN_FEE
    8. Return PaymentResult with status and details
    """
    if w3 is None:
        return PaymentResult(PaymentStatus.RPC_ERROR, "Radius RPC client is not initialized")

    if not TX_HASH_RE.fullmatch(tx_hash):
        return PaymentResult(PaymentStatus.TX_NOT_FOUND, "Invalid transaction hash format")

    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except TransactionNotFound:
        return PaymentResult(PaymentStatus.TX_NOT_FOUND, "Transaction not found or not yet confirmed")
    except Exception as exc:
        logger.error("RPC error while fetching tx receipt %s: %s", tx_hash, exc)
        return PaymentResult(PaymentStatus.RPC_ERROR, "RPC error while fetching transaction receipt")

    if not receipt:
        return PaymentResult(PaymentStatus.TX_NOT_FOUND, "Transaction receipt not found")

    if receipt.get("status") != 1:
        return PaymentResult(PaymentStatus.TX_NOT_FOUND, "Transaction failed on-chain (status != 1)")

    receipt_to = (receipt.get("to") or "").lower()
    if receipt_to != SBC_CONTRACT:
        return PaymentResult(PaymentStatus.WRONG_CONTRACT, "Transaction target is not the SBC contract")

    transfer = _find_transfer_event(receipt.get("logs", []), SBC_CONTRACT)
    if transfer is None:
        return PaymentResult(PaymentStatus.NO_TRANSFER_EVENT, "No valid SBC Transfer event found")

    recipient = transfer["recipient"].lower()
    if recipient != SERVICE_WALLET:
        return PaymentResult(PaymentStatus.WRONG_RECIPIENT, "Transfer recipient does not match service wallet")

    amount = transfer["amount"]
    if amount < SHORTEN_FEE:
        return PaymentResult(
            PaymentStatus.INSUFFICIENT_AMOUNT,
            f"Transferred amount {amount} is below required fee {SHORTEN_FEE}",
            amount=amount,
            sender=transfer["sender"],
        )

    return PaymentResult(
        PaymentStatus.SUCCESS,
        "Payment verified successfully",
        amount=amount,
        sender=transfer["sender"],
    )


def _find_transfer_event(logs, stablecoin_contract: str) -> dict | None:
    """
    Search through transaction logs for an ERC-20 Transfer event
    that sends tokens to the SERVICE_WALLET.

    Transfer event log structure:
    - topics[0]: event signature hash (TRANSFER_EVENT_TOPIC)
    - topics[1]: from address (padded to 32 bytes)
    - topics[2]: to address (padded to 32 bytes)
    - data: uint256 amount

    Returns dict with {sender, recipient, amount} or None if not found.
    """
    for log in logs or []:
        try:
            topics = log.get("topics", [])
            if len(topics) < 3:
                continue

            topic0 = _to_hex(topics[0]).lower()
            if topic0 != TRANSFER_EVENT_TOPIC:
                continue

            emitter = (log.get("address") or "").lower()
            if emitter != SBC_CONTRACT:
                continue

            sender = _decode_indexed_address(topics[1])
            recipient = _decode_indexed_address(topics[2])
            amount = _decode_uint256(log.get("data"))
            return {"sender": sender, "recipient": recipient, "amount": amount}
        except Exception:
            continue

    return None


def _to_hex(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value if value.startswith("0x") else f"0x{value}"
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if hasattr(value, "hex"):
        hx = value.hex()
        return hx if str(hx).startswith("0x") else f"0x{hx}"
    return str(value)


def _decode_indexed_address(topic) -> str:
    topic_hex = _to_hex(topic).lower()
    if not topic_hex.startswith("0x"):
        raise ValueError("Invalid topic format")
    raw = topic_hex[2:].rjust(64, "0")
    return "0x" + raw[-40:]


def _decode_uint256(data) -> int:
    data_hex = _to_hex(data).lower()
    if not data_hex.startswith("0x"):
        raise ValueError("Invalid uint256 data format")
    return int(data_hex, 16)


# ---------------------------------------------------------------------------
# Payment info (returned in 402 responses)
# ---------------------------------------------------------------------------
def get_payment_info() -> dict:
    """Return payment details for 402 responses."""
    return {
        "pay_to": SERVICE_WALLET,
        "amount": str(SHORTEN_FEE),
        "token": "SBC",
        "decimals": 6,
        "chain_id": RADIUS_CHAIN_ID,
        "sbc_contract": SBC_CONTRACT,
        "rpc_url": RADIUS_RPC_URL,
    }
