import asyncio
import logging
import os
import time
from dataclasses import dataclass

from eth_account import Account
from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, Gauge
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field
from web3 import HTTPProvider, Web3

from permit2 import sign_permit_witness_transfer

# ---- Config ---------------------------------------------------------------
RPC_URL = os.getenv("RPC_URL", "").strip()
CHAIN_ID = int(os.getenv("CHAIN_ID", "72344"))
NETWORK_CAIP2 = os.getenv("NETWORK_CAIP2", f"eip155:{CHAIN_ID}")
SERVICE_WALLET_ADDRESS = os.getenv("SERVICE_WALLET_ADDRESS", "").strip()
SBC_CONTRACT_ADDRESS = os.getenv(
    "SBC_CONTRACT_ADDRESS",
    "0x33ad9e4bd16b69b5bfded37d8b5d9ff9aba014fb",
).strip()
PERMIT2_CONTRACT_ADDRESS = os.getenv(
    "PERMIT2_CONTRACT_ADDRESS",
    "0x000000000022D473030F116dDEE9F6B43aC78BA3",
).strip()
X402_PROXY_ADDRESS = os.getenv(
    "X402_PROXY_ADDRESS",
    "0x402085c248EeA27D92E8b30b2C58ed07f9E20001",
).strip()
DEFAULT_SBC_AMOUNT = int(os.getenv("SBC_AMOUNT", "1000"))
DEFAULT_DEADLINE_SECONDS = int(os.getenv("DEADLINE_SECONDS", "300"))
REQUEST_TIMEOUT_S = int(os.getenv("RPC_TIMEOUT_SECONDS", "10"))
APPROVAL_TX_TIMEOUT_S = int(os.getenv("APPROVAL_TX_TIMEOUT_SECONDS", "60"))

MAX_UINT256 = 2**256 - 1

logger = logging.getLogger("radius_signer")
logging.basicConfig(level=logging.INFO)


# ---- Metrics --------------------------------------------------------------
# The FastAPI instrumentator already gives us http_requests_total and
# http_request_duration_seconds per handler — we only add what it can't:
# per-wallet outcome labels and boot-time approval status.
SIGN_OUTCOMES = Counter(
    "signer_sign_total",
    "Outcomes of /sign-permit2 calls labeled by terminal state and wallet.",
    ["outcome", "wallet_index"],
)
APPROVAL_OUTCOMES = Counter(
    "signer_permit2_approval_total",
    "Outcomes of the one-time SBC.approve(Permit2) per wallet at boot.",
    ["outcome", "wallet_index"],
)
WALLET_SBC_BALANCE = Gauge(
    "signer_wallet_sbc_balance_units",
    "SBC balance per signer wallet in raw 6-decimal units.",
    ["wallet_index", "address"],
)


# ---- ABI ------------------------------------------------------------------
ERC20_ABI = [
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"},
                {"name": "value", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]


# ---- Request schema -------------------------------------------------------
class SignRequest(BaseModel):
    wallet_index: int = Field(ge=0)
    amount: int = Field(default=DEFAULT_SBC_AMOUNT, gt=0)
    deadline_seconds: int = Field(default=DEFAULT_DEADLINE_SECONDS, gt=0)


@dataclass
class WalletSlot:
    index: int
    address: str
    account: Account
    bootstrapped: bool = False


def _normalize_private_key(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("wallet key is empty")
    return value if value.startswith("0x") else f"0x{value}"


def _build_wallets() -> list[WalletSlot]:
    wallets = []
    for index in range(1, 4):
        raw = os.getenv(f"WALLET_KEY_{index}", "").strip()
        if not raw:
            continue
        account = Account.from_key(_normalize_private_key(raw))
        wallets.append(
            WalletSlot(index=index - 1, address=account.address, account=account)
        )
    if not wallets:
        raise RuntimeError("No wallet keys configured (set WALLET_KEY_1[, _2, _3])")
    return wallets


def _build_web3() -> Web3:
    if not RPC_URL:
        raise RuntimeError("RPC_URL is required")
    client = Web3(HTTPProvider(RPC_URL, request_kwargs={"timeout": REQUEST_TIMEOUT_S}))
    if not client.is_connected():
        raise RuntimeError(f"Failed to connect to RPC_URL: {RPC_URL}")
    if client.eth.chain_id != CHAIN_ID:
        raise RuntimeError(
            f"RPC chain ID mismatch: expected {CHAIN_ID}, got {client.eth.chain_id}"
        )
    return client


if not SERVICE_WALLET_ADDRESS:
    raise RuntimeError("SERVICE_WALLET_ADDRESS is required")

web3 = _build_web3()
wallets = _build_wallets()
sbc = web3.eth.contract(
    address=Web3.to_checksum_address(SBC_CONTRACT_ADDRESS),
    abi=ERC20_ABI,
)
service_wallet_checksum = Web3.to_checksum_address(SERVICE_WALLET_ADDRESS)
permit2_checksum = Web3.to_checksum_address(PERMIT2_CONTRACT_ADDRESS)
x402_proxy_checksum = Web3.to_checksum_address(X402_PROXY_ADDRESS)


# ---- Bootstrap ------------------------------------------------------------
def _bootstrap() -> None:
    """For each wallet, ensure SBC.approve(Permit2, MAX) is set.

    Idempotent (skips wallets already approved). Best-effort per wallet —
    failure of one wallet doesn't crash the pod unless ALL wallets fail.
    /sign-permit2 returns 503 for any wallet whose bootstrap failed;
    operator action is to restart the pod.

    Gas note: Radius SBC.approve uses ~115k gas (vs vanilla ERC-20 ~46k)
    because of Turnstile-related state mutations, so we estimate_gas
    rather than hardcoding (a 100k limit OOG'd during the M1 spike).
    """
    for slot in wallets:
        idx = str(slot.index)
        try:
            balance = sbc.functions.balanceOf(slot.address).call()
            WALLET_SBC_BALANCE.labels(wallet_index=idx, address=slot.address).set(balance)
            allowance = sbc.functions.allowance(slot.address, permit2_checksum).call()

            if allowance > 0:
                slot.bootstrapped = True
                APPROVAL_OUTCOMES.labels(outcome="already_approved", wallet_index=idx).inc()
                logger.info("wallet %s (%s) already approved", slot.index, slot.address)
                continue

            logger.info("wallet %s (%s) needs approval; sending tx…", slot.index, slot.address)
            approve_fn = sbc.functions.approve(permit2_checksum, MAX_UINT256)
            try:
                gas_limit = int(approve_fn.estimate_gas({"from": slot.address}) * 1.5)
            except Exception as exc:
                logger.warning(
                    "estimate_gas failed for wallet %s; falling back to 250k: %s",
                    slot.index, exc,
                )
                gas_limit = 250_000
            tx = approve_fn.build_transaction({
                "from": slot.address,
                "chainId": CHAIN_ID,
                "nonce": web3.eth.get_transaction_count(slot.address, "pending"),
                "gas": gas_limit,
                "gasPrice": web3.eth.gas_price,
            })
            signed = slot.account.sign_transaction(tx)
            tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=APPROVAL_TX_TIMEOUT_S)
            if receipt.status != 1:
                raise RuntimeError(f"approval tx reverted: {Web3.to_hex(tx_hash)}")

            slot.bootstrapped = True
            APPROVAL_OUTCOMES.labels(outcome="approved", wallet_index=idx).inc()
            logger.info(
                "wallet %s approved tx=%s gas=%d",
                slot.index, Web3.to_hex(tx_hash), receipt.gasUsed,
            )
        except Exception as exc:
            APPROVAL_OUTCOMES.labels(outcome="approve_failed", wallet_index=idx).inc()
            logger.exception("bootstrap failed for wallet %s: %s", slot.index, exc)

    healthy = sum(1 for s in wallets if s.bootstrapped)
    logger.info("bootstrap complete: %d/%d wallets healthy", healthy, len(wallets))
    if healthy == 0:
        raise RuntimeError("bootstrap failed for ALL wallets — refusing to start")


_bootstrap()


# ---- FastAPI --------------------------------------------------------------
app = FastAPI(title="radius-signer", version="0.2.0")
Instrumentator(
    excluded_handlers=["/metrics", "/health"],
    should_group_status_codes=False,
).instrument(app).expose(app)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "chain_id": CHAIN_ID,
        "network": NETWORK_CAIP2,
        "wallet_count": len(wallets),
        "wallets_bootstrapped": sum(1 for s in wallets if s.bootstrapped),
        "service_wallet": service_wallet_checksum,
        "token_contract": SBC_CONTRACT_ADDRESS.lower(),
        "permit2_contract": PERMIT2_CONTRACT_ADDRESS,
        "x402_proxy": X402_PROXY_ADDRESS,
    }


@app.get("/wallets")
async def wallets_status() -> dict:
    statuses = []
    for slot in wallets:
        allowance = await asyncio.to_thread(
            sbc.functions.allowance(slot.address, permit2_checksum).call
        )
        balance = await asyncio.to_thread(
            sbc.functions.balanceOf(slot.address).call
        )
        WALLET_SBC_BALANCE.labels(
            wallet_index=str(slot.index), address=slot.address
        ).set(balance)
        statuses.append({
            "wallet_index": slot.index,
            "address": slot.address,
            "sbc_balance": balance,
            "permit2_allowance": allowance,
            "bootstrapped": slot.bootstrapped,
        })
    return {
        "chain_id": CHAIN_ID,
        "network": NETWORK_CAIP2,
        "wallet_count": len(wallets),
        "wallets": statuses,
    }


@app.post("/sign-permit2")
async def sign_permit2(request: SignRequest) -> dict:
    if request.wallet_index >= len(wallets):
        raise HTTPException(
            status_code=400,
            detail=f"wallet_index={request.wallet_index} out of range for {len(wallets)} wallets",
        )
    slot = wallets[request.wallet_index]
    idx = str(slot.index)

    if not slot.bootstrapped:
        SIGN_OUTCOMES.labels(outcome="not_bootstrapped", wallet_index=idx).inc()
        raise HTTPException(
            status_code=503,
            detail=f"wallet {slot.index} not bootstrapped; restart pod to retry approval",
        )

    try:
        deadline = int(time.time()) + request.deadline_seconds
        signature, authorization = sign_permit_witness_transfer(
            slot.account,
            chain_id=CHAIN_ID,
            permit2_address=permit2_checksum,
            token=Web3.to_checksum_address(SBC_CONTRACT_ADDRESS),
            amount=request.amount,
            spender=x402_proxy_checksum,
            pay_to=service_wallet_checksum,
            deadline=deadline,
        )
        SIGN_OUTCOMES.labels(outcome="success", wallet_index=idx).inc()
        return {"signature": signature, "permit2Authorization": authorization}
    except Exception as exc:
        SIGN_OUTCOMES.labels(outcome="signing_error", wallet_index=idx).inc()
        logger.exception("sign-permit2 failed for wallet %s", slot.index)
        raise HTTPException(status_code=500, detail=f"signing error: {exc}") from exc
