import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from eth_account import Account
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from web3 import HTTPProvider, Web3
from web3.exceptions import ContractLogicError


RPC_URL = os.getenv("RPC_URL", "").strip()
CHAIN_ID = int(os.getenv("CHAIN_ID", "72344"))
SERVICE_WALLET_ADDRESS = os.getenv("SERVICE_WALLET_ADDRESS", "").strip()
SBC_CONTRACT_ADDRESS = os.getenv(
    "SBC_CONTRACT_ADDRESS",
    "0x33ad9e4bd16b69b5bfded37d8b5d9ff9aba014fb",
).strip()
RECEIPT_TIMEOUT_S = int(os.getenv("RECEIPT_TIMEOUT_S", "30"))
REQUEST_TIMEOUT_S = int(os.getenv("RPC_TIMEOUT_SECONDS", "10"))
DEFAULT_SBC_AMOUNT = int(os.getenv("SBC_AMOUNT", "1000"))
DEFAULT_GAS_LIMIT = int(os.getenv("TX_GAS_LIMIT", "150000"))
# Minimum SBC (raw units, 6 decimals) the Turnstile will deduct for gas when RUSD is low.
# Radius docs: minimum conversion is 0.1 SBC = 100_000 raw units.
TURNSTILE_MIN_SBC_UNITS = 100_000

logger = logging.getLogger("radius_signer")
logging.basicConfig(level=logging.INFO)


ERC20_ABI = [
    {
        "type": "function",
        "name": "transfer",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]


class PayRequest(BaseModel):
    wallet_index: int = Field(ge=0)
    amount: int = Field(default=DEFAULT_SBC_AMOUNT, gt=0)
    gas_limit: int = Field(default=DEFAULT_GAS_LIMIT, gt=21000)


class PayResponse(BaseModel):
    tx_hash: str
    wallet_index: int
    sender: str
    amount: int
    confirmation_ms: int
    block_number: int
    sender_balance: int


class WalletStatus(BaseModel):
    wallet_index: int
    address: str
    sbc_balance: int
    rusd_balance_wei: int
    turnstile_reserve_required: bool


@dataclass
class WalletSlot:
    index: int
    private_key: str
    address: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _normalize_private_key(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("wallet key is empty")
    return value if value.startswith("0x") else f"0x{value}"


def _build_wallets() -> list[WalletSlot]:
    wallets = []
    for index in range(1, 4):
        raw_key = os.getenv(f"WALLET_KEY_{index}", "").strip()
        if not raw_key:
            continue
        key = _normalize_private_key(raw_key)
        address = Account.from_key(key).address
        wallets.append(WalletSlot(index=index - 1, private_key=key, address=address))
    if not wallets:
        raise RuntimeError("No wallet keys configured")
    return wallets


def _build_web3() -> Web3:
    if not RPC_URL:
        raise RuntimeError("RPC_URL is required")
    client = Web3(HTTPProvider(RPC_URL, request_kwargs={"timeout": REQUEST_TIMEOUT_S}))
    if not client.is_connected():
        raise RuntimeError(f"Failed to connect to RPC_URL: {RPC_URL}")
    actual_chain_id = client.eth.chain_id
    if actual_chain_id != CHAIN_ID:
        raise RuntimeError(f"RPC chain ID mismatch: expected {CHAIN_ID}, got {actual_chain_id}")
    return client


if not SERVICE_WALLET_ADDRESS:
    raise RuntimeError("SERVICE_WALLET_ADDRESS is required")

web3 = _build_web3()
wallets = _build_wallets()
token = web3.eth.contract(
    address=Web3.to_checksum_address(SBC_CONTRACT_ADDRESS),
    abi=ERC20_ABI,
)
service_wallet_checksum = Web3.to_checksum_address(SERVICE_WALLET_ADDRESS)

app = FastAPI(title="radius-signer", version="0.1.0")


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "chain_id": CHAIN_ID,
        "wallet_count": len(wallets),
        "service_wallet": service_wallet_checksum,
        "token_contract": SBC_CONTRACT_ADDRESS.lower(),
    }


@app.get("/wallets")
async def wallet_status() -> dict:
    statuses = []

    for slot in wallets:
        sbc_balance = await asyncio.to_thread(token.functions.balanceOf(slot.address).call)
        rusd_balance = await asyncio.to_thread(web3.eth.get_balance, slot.address)
        statuses.append(
            WalletStatus(
                wallet_index=slot.index,
                address=slot.address,
                sbc_balance=sbc_balance,
                rusd_balance_wei=rusd_balance,
                turnstile_reserve_required=rusd_balance == 0,
            ).model_dump()
        )

    return {
        "chain_id": CHAIN_ID,
        "wallet_count": len(wallets),
        "turnstile_min_sbc_units": TURNSTILE_MIN_SBC_UNITS,
        "wallets": statuses,
    }


@app.post("/pay", response_model=PayResponse)
async def pay(request: PayRequest) -> PayResponse:
    if request.wallet_index >= len(wallets):
        raise HTTPException(
            status_code=400,
            detail=f"wallet_index={request.wallet_index} is out of range for {len(wallets)} configured wallets",
        )

    slot = wallets[request.wallet_index]

    async with slot.lock:
        started_at = time.time()

        try:
            sender_balance = await asyncio.to_thread(token.functions.balanceOf(slot.address).call)
            # Guard against Turnstile silently deducting SBC before EVM execution:
            # eth_call passes when balance >= amount, but the real tx pre-deducts
            # TURNSTILE_MIN_SBC_UNITS for gas conversion (0.1 SBC minimum per trigger).
            rusd_balance = await asyncio.to_thread(web3.eth.get_balance, slot.address)
            effective_minimum = request.amount + (TURNSTILE_MIN_SBC_UNITS if rusd_balance == 0 else 0)
            if sender_balance < effective_minimum:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"wallet_index={slot.index} sender={slot.address} has insufficient SBC balance "
                        f"{sender_balance} (need {effective_minimum}: amount={request.amount} + "
                        f"turnstile_reserve={effective_minimum - request.amount})"
                    ),
                )

            transfer_fn = token.functions.transfer(
                service_wallet_checksum,
                request.amount,
            )
            try:
                await asyncio.to_thread(transfer_fn.call, {"from": slot.address})
            except ContractLogicError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=(
                        f"preflight transfer reverted for wallet_index={slot.index} "
                        f"sender={slot.address}: {exc}"
                    ),
                ) from exc
            except Exception as exc:
                logger.warning(
                    "preflight transfer call failed wallet_index=%s sender=%s: %s",
                    slot.index,
                    slot.address,
                    exc,
                )

            nonce = await asyncio.to_thread(web3.eth.get_transaction_count, slot.address, "pending")
            gas_price = await asyncio.to_thread(lambda: web3.eth.gas_price)
            gas_estimate = await asyncio.to_thread(
                transfer_fn.estimate_gas, {"from": slot.address}
            )
            # 30% headroom; Radius average ERC-20 transfer is ~101k gas (docs).
            # eth_call uses unlimited gas so a fixed cap below the average causes status=0.
            gas_limit = max(request.gas_limit, int(gas_estimate * 1.3))
            transaction = transfer_fn.build_transaction(
                {
                    "from": slot.address,
                    "chainId": CHAIN_ID,
                    "gas": gas_limit,
                    # Omit `type` entirely for a legacy transaction. eth-account
                    # rejects an explicit type 0, but legacy + gasPrice is valid.
                    "gasPrice": gas_price,
                    "nonce": nonce,
                }
            )
            signed = Account.sign_transaction(transaction, slot.private_key)
            tx_hash_bytes = await asyncio.to_thread(web3.eth.send_raw_transaction, signed.raw_transaction)
            receipt = await asyncio.to_thread(
                web3.eth.wait_for_transaction_receipt,
                tx_hash_bytes,
                RECEIPT_TIMEOUT_S,
                0.2,
            )
        except Exception as exc:
            logger.exception("payment submission failed wallet_index=%s sender=%s", slot.index, slot.address)
            raise HTTPException(status_code=502, detail=f"payment submission failed: {exc}") from exc

        if receipt.status != 1:
            tx_hash = Web3.to_hex(tx_hash_bytes)
            logger.error(
                "payment transaction failed on-chain wallet_index=%s sender=%s tx_hash=%s",
                slot.index,
                slot.address,
                tx_hash,
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    f"payment transaction failed on-chain wallet_index={slot.index} "
                    f"sender={slot.address} tx_hash={tx_hash}"
                ),
            )

        return PayResponse(
            tx_hash=Web3.to_hex(tx_hash_bytes),
            wallet_index=slot.index,
            sender=slot.address,
            amount=request.amount,
            confirmation_ms=int((time.time() - started_at) * 1000),
            block_number=receipt.blockNumber,
            sender_balance=sender_balance,
        )
