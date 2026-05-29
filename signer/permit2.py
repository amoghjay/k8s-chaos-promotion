"""Pure Permit2 EIP-712 signing helpers. No FastAPI / web3 / RPC.

Kept separate from main.py so unit tests don't need an RPC endpoint to import it.
"""

from __future__ import annotations

import secrets
from typing import TypedDict

from eth_account import Account
from eth_account.messages import encode_typed_data


# Permit2 EIP-712 type tree.
# `PermitWitnessTransferFrom` extends Permit2's `PermitTransferFrom` with the
# x402 Witness pattern — Witness fields sourced from x402ExactPermit2Proxy:
#   "Witness(address to,uint256 validAfter)"
PERMIT2_TYPES = {
    "PermitWitnessTransferFrom": [
        {"name": "permitted", "type": "TokenPermissions"},
        {"name": "spender", "type": "address"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
        {"name": "witness", "type": "Witness"},
    ],
    "TokenPermissions": [
        {"name": "token", "type": "address"},
        {"name": "amount", "type": "uint256"},
    ],
    "Witness": [
        {"name": "to", "type": "address"},
        {"name": "validAfter", "type": "uint256"},
    ],
}


def permit2_domain(chain_id: int, permit2_address: str) -> dict:
    """Permit2 EIP-712 domain. NO `version` field — Uniswap's deliberate omission."""
    return {
        "name": "Permit2",
        "chainId": chain_id,
        "verifyingContract": permit2_address,
    }


def normalize_v(signature: bytes) -> bytes:
    """Map v=0|1 → v=27|28 for signatures from hardware wallets or external signers.

    eth-account's local sign already returns v=27|28; this is a defensive
    one-liner so we don't silently fail facilitator ecrecover for sigs we
    didn't produce in-process. See radius-dev gotchas.md#9.
    """
    if len(signature) != 65:
        raise ValueError(f"signature must be 65 bytes, got {len(signature)}")
    v = signature[64]
    if v < 27:
        return signature[:64] + bytes([v + 27])
    return signature


def sign_permit_witness_transfer(
    account: Account,
    *,
    chain_id: int,
    permit2_address: str,
    token: str,
    amount: int,
    spender: str,
    pay_to: str,
    deadline: int,
    valid_after: int = 0,
    nonce: int | None = None,
) -> tuple[str, dict]:
    """Sign PermitWitnessTransferFrom for x402 Permit2 settlement.

    Returns (signature_hex, permit2_authorization_dict). The authorization dict
    is the payload shape the facilitator's /verify and /settle endpoints expect
    inside paymentPayload.payload.permit2Authorization.
    """
    if nonce is None:
        nonce = int.from_bytes(secrets.token_bytes(32), "big")

    domain = permit2_domain(chain_id, permit2_address)
    message = {
        "permitted": {"token": token, "amount": amount},
        "spender": spender,
        "nonce": nonce,
        "deadline": deadline,
        "witness": {"to": pay_to, "validAfter": valid_after},
    }

    signable = encode_typed_data(
        domain_data=domain,
        message_types=PERMIT2_TYPES,
        message_data=message,
    )
    signed = account.sign_message(signable)
    sig = normalize_v(bytes(signed.signature))
    signature_hex = "0x" + sig.hex()

    authorization = {
        "permitted": {"token": token, "amount": str(amount)},
        "from": account.address,
        "spender": spender,
        "nonce": str(nonce),
        "deadline": str(deadline),
        "witness": {"to": pay_to, "validAfter": str(valid_after)},
    }
    return signature_hex, authorization
