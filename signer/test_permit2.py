"""Unit tests for signer/permit2.py (pure functions, no RPC needed).

Run from repo root:
    python -m pytest signer/test_permit2.py -v

Or directly:
    python signer/test_permit2.py
"""

from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_typed_data

from permit2 import (
    PERMIT2_TYPES,
    normalize_v,
    permit2_domain,
    sign_permit_witness_transfer,
)

# Deterministic test key (Anvil test account #0). Never load real funds onto it.
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
CHAIN_ID = 72344
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
SBC = "0x33ad9e4bd16b69b5bfded37d8b5d9ff9aba014fb"
X402_PROXY = "0x402085c248EeA27D92E8b30b2C58ed07f9E20001"
MERCHANT = "0xbD5fdCde255Abb883cB0C3137037cAef28ed10ac"


def test_permit2_domain_omits_version():
    """Permit2's deploy uses domain without `version`. Our helper must match."""
    domain = permit2_domain(CHAIN_ID, PERMIT2)
    assert set(domain.keys()) == {"name", "chainId", "verifyingContract"}
    assert "version" not in domain


def test_normalize_v_maps_zero_to_27():
    sig = bytes(64) + bytes([0])
    out = normalize_v(sig)
    assert out[64] == 27
    assert out[:64] == sig[:64]


def test_normalize_v_maps_one_to_28():
    sig = bytes(64) + bytes([1])
    out = normalize_v(sig)
    assert out[64] == 28


def test_normalize_v_passes_through_27_and_28():
    for v in (27, 28):
        sig = bytes(64) + bytes([v])
        out = normalize_v(sig)
        assert out[64] == v


def test_normalize_v_rejects_wrong_length():
    try:
        normalize_v(b"\x00" * 64)
    except ValueError as exc:
        assert "65 bytes" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_signature_recovers_to_signer():
    """The signature emitted by sign_permit_witness_transfer must verify back to the wallet."""
    account = Account.from_key(TEST_KEY)
    signature_hex, auth = sign_permit_witness_transfer(
        account,
        chain_id=CHAIN_ID,
        permit2_address=PERMIT2,
        token=SBC,
        amount=1000,
        spender=X402_PROXY,
        pay_to=MERCHANT,
        deadline=1_780_000_000,
        nonce=0xDEADBEEF,
    )

    # Rebuild the same signable message and recover the signer.
    domain = permit2_domain(CHAIN_ID, PERMIT2)
    message = {
        "permitted": {"token": SBC, "amount": 1000},
        "spender": X402_PROXY,
        "nonce": 0xDEADBEEF,
        "deadline": 1_780_000_000,
        "witness": {"to": MERCHANT, "validAfter": 0},
    }
    signable = encode_typed_data(
        domain_data=domain,
        message_types=PERMIT2_TYPES,
        message_data=message,
    )
    recovered = Account.recover_message(signable, signature=signature_hex)
    assert recovered.lower() == account.address.lower(), (
        f"signature did not recover to signer: got {recovered}, expected {account.address}"
    )

    # Authorization dict shape sanity-check (what gets sent to the facilitator).
    assert auth["from"] == account.address
    assert auth["spender"] == X402_PROXY
    assert auth["permitted"]["token"] == SBC
    assert auth["permitted"]["amount"] == "1000"
    assert auth["nonce"] == str(0xDEADBEEF)
    assert auth["deadline"] == "1780000000"
    assert auth["witness"]["to"] == MERCHANT
    assert auth["witness"]["validAfter"] == "0"


def test_random_nonce_when_not_supplied():
    account = Account.from_key(TEST_KEY)
    _, a1 = sign_permit_witness_transfer(
        account, chain_id=CHAIN_ID, permit2_address=PERMIT2, token=SBC, amount=1,
        spender=X402_PROXY, pay_to=MERCHANT, deadline=1,
    )
    _, a2 = sign_permit_witness_transfer(
        account, chain_id=CHAIN_ID, permit2_address=PERMIT2, token=SBC, amount=1,
        spender=X402_PROXY, pay_to=MERCHANT, deadline=1,
    )
    # 2^256 random nonces — collision probability is negligible. If this fails,
    # nonce randomness is broken.
    assert a1["nonce"] != a2["nonce"]


if __name__ == "__main__":
    # Allow `python signer/test_permit2.py` to run without pytest.
    import sys
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                print(f"  FAIL  {name}: {exc}")
                failures += 1
    if failures:
        print(f"\n{failures} failure(s)")
        sys.exit(1)
    print("\nall green")
