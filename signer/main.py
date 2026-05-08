import asyncio
import os
import time
from dataclasses import dataclass, field

from eth_account import Account
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from web3 import HTTPProvider, Web3


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
DEFAULT_GAS_LIMIT = int(os.getenv("TX_GAS_LIMIT", "100000"))


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
            nonce = await asyncio.to_thread(web3.eth.get_transaction_count, slot.address, "pending")
            gas_price = await asyncio.to_thread(lambda: web3.eth.gas_price)
            transaction = token.functions.transfer(
                service_wallet_checksum,
                request.amount,
            ).build_transaction(
                {
                    "from": slot.address,
                    "chainId": CHAIN_ID,
                    "gas": request.gas_limit,
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
            raise HTTPException(status_code=502, detail=f"payment submission failed: {exc}") from exc

        if receipt.status != 1:
            raise HTTPException(status_code=502, detail="payment transaction failed on-chain")

        return PayResponse(
            tx_hash=tx_hash_bytes.hex(),
            wallet_index=slot.index,
            sender=slot.address,
            amount=request.amount,
            confirmation_ms=int((time.time() - started_at) * 1000),
            block_number=receipt.blockNumber,
        )
