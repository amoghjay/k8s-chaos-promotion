"""
URL Shortener — FastAPI + Redis + Postgres
Designed for chaos engineering demonstrations.

Payment path is x402 Permit2 via the Radius facilitator. The app never touches
the chain — payment.py forwards the client's PAYMENT-SIGNATURE to the
facilitator's /verify and /settle endpoints, and persists the resulting
settlement tx hash to Postgres.
"""

import os
import secrets
import string
import logging
from contextlib import asynccontextmanager

import asyncpg
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse, Response
from prometheus_client import Counter, Histogram
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, field_validator

from payment import (
    SettlementResult,
    SettlementStatus,
    encode_header,
    payment_required_descriptor,
    settle_payment,
    settled_response_header,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://urlshortener:password@localhost:5432/urlshortener",
)
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
CODE_LENGTH = int(os.getenv("CODE_LENGTH", "8"))
REDIS_TTL = int(os.getenv("REDIS_TTL", "3600"))
PAYMENT_ENABLED = bool(os.getenv("FACILITATOR_URL", "").strip()) or bool(
    os.getenv("PAYMENT_ENABLED", "").strip()
)

logger = logging.getLogger("url_shortener")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Prometheus metrics (beyond auto-instrumented request count + latency)
# ---------------------------------------------------------------------------
CACHE_HITS = Counter("url_shortener_cache_hits_total", "Redis cache hits")
CACHE_MISSES = Counter("url_shortener_cache_misses_total", "Redis cache misses (Postgres fallback)")
URLS_CREATED = Counter("url_shortener_urls_created_total", "Short URLs created")

# Outcome bucket for the full app-perceived facilitator flow.
# Labels mirror payment.SettlementStatus + an extra `settled` value on success.
PAYMENT_FACILITATOR = Counter(
    "payment_facilitator_total",
    "Outcomes of facilitator-mediated payment attempts.",
    ["outcome"],
)
PAYMENT_SETTLEMENT_DURATION = Histogram(
    "payment_settlement_duration_seconds",
    "App-perceived end-to-end payment time (header decode + verify + settle).",
)
PAYMENT_402_RESPONSES = Counter(
    "payment_402_responses_total",
    "HTTP 402 responses emitted when PAYMENT-SIGNATURE header is missing.",
)
PAYMENT_REPLAY_ATTEMPTS = Counter(
    "payment_replay_attempts_total",
    "Duplicate settlement_tx_hash inserts caught by the UNIQUE constraint.",
)

# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------
db_pool: asyncpg.Pool | None = None
redis_client: aioredis.Redis | None = None
http_client: httpx.AsyncClient | None = None
_started: bool = False  # Flipped once after first successful readiness check

ALPHABET = string.ascii_letters + string.digits


def _generate_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


# Schema bootstrap. CREATE TABLE for fresh deploys; ALTER for upgrades from the
# pre-x402 schema that had tx_hash instead of settlement_tx_hash.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS urls (
    code               VARCHAR(16) PRIMARY KEY,
    url                TEXT NOT NULL UNIQUE,
    settlement_tx_hash VARCHAR(66) UNIQUE,
    payer_address      VARCHAR(42),
    settled_at         TIMESTAMPTZ,
    created_at         TIMESTAMPTZ DEFAULT NOW()
);
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'urls' AND column_name = 'tx_hash'
    ) THEN
        ALTER TABLE urls RENAME COLUMN tx_hash TO settlement_tx_hash;
    END IF;
END $$;
ALTER TABLE urls ADD COLUMN IF NOT EXISTS payer_address VARCHAR(42);
ALTER TABLE urls ADD COLUMN IF NOT EXISTS settled_at TIMESTAMPTZ;
"""


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, redis_client, http_client

    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, command_timeout=10)
        async with db_pool.acquire() as conn:
            try:
                await conn.execute(SCHEMA_SQL)
            except (asyncpg.UniqueViolationError, asyncpg.DuplicateTableError):
                pass  # Another pod ran the migration first — ignore.
        logger.info("Postgres connected")
    except Exception as e:
        logger.error("Postgres failed: %s", e)
        db_pool = None

    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis connected")
    except Exception as e:
        logger.error("Redis failed: %s", e)
        redis_client = None

    # One AsyncClient for the lifetime of the pod — connection pooling +
    # keepalive matter when we hit the facilitator on every /shorten.
    http_client = httpx.AsyncClient()
    logger.info(
        "Payment %s (facilitator=%s)",
        "enabled" if PAYMENT_ENABLED else "disabled",
        os.getenv("FACILITATOR_URL", "<unset>"),
    )

    yield

    if db_pool:
        await db_pool.close()
    if redis_client:
        await redis_client.aclose()
    if http_client:
        await http_client.aclose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="URL Shortener", version="2.0.0", lifespan=lifespan)

Instrumentator(
    excluded_handlers=["/metrics", "/health", "/ready"],
    should_group_status_codes=False,
).instrument(app).expose(app)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ShortenRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class ShortenResponse(BaseModel):
    code: str
    short_url: str
    original_url: str


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    """Liveness. 200 if Postgres is reachable. Redis-down does not fail liveness."""
    pg_ok = False
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            pg_ok = True
        except Exception:
            pass

    if not pg_ok:
        return JSONResponse({"status": "unhealthy", "postgres": "down"}, status_code=503)

    redis_ok = False
    if redis_client:
        try:
            await redis_client.ping()
            redis_ok = True
        except Exception:
            pass

    status = "ok" if redis_ok else "degraded"
    return JSONResponse({"status": status, "postgres": "ok", "redis": "ok" if redis_ok else "down"})


@app.get("/ready")
async def ready():
    """Readiness. Postgres required always; Redis required only before the first ready check.

    After the first successful readiness, killing Redis must not pull the pod from
    the Service — chaos experiment #2 depends on this.
    """
    global _started

    pg_ok = False
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            pg_ok = True
        except Exception:
            pass

    if not pg_ok:
        return JSONResponse({"status": "not ready", "postgres": "down"}, status_code=503)

    if not _started:
        redis_ok = False
        if redis_client:
            try:
                await redis_client.ping()
                redis_ok = True
            except Exception:
                pass

        if not redis_ok:
            return JSONResponse({"status": "not ready", "redis": "down"}, status_code=503)

        _started = True

    return JSONResponse({"status": "ready"})


# ---------------------------------------------------------------------------
# /shorten — x402 payment-gated URL creation
# ---------------------------------------------------------------------------
def _short_url(code: str, original: str) -> dict:
    return {"code": code, "short_url": f"{BASE_URL}/{code}", "original_url": original}


@app.post("/shorten", status_code=201)
async def shorten_url(
    body: ShortenRequest,
    request: Request,
    payment_signature: str | None = Header(default=None, alias="PAYMENT-SIGNATURE"),
):
    if db_pool is None:
        raise HTTPException(503, "Database unavailable")

    async with db_pool.acquire() as conn:
        # If this URL was already shortened, return it idempotently — no new payment required.
        existing = await conn.fetchrow("SELECT code FROM urls WHERE url = $1", body.url)
        if existing:
            return JSONResponse(_short_url(existing["code"], body.url), status_code=200)

        settlement_tx_hash: str | None = None
        payer: str | None = None

        if PAYMENT_ENABLED:
            if not payment_signature:
                PAYMENT_402_RESPONSES.inc()
                descriptor = payment_required_descriptor(str(request.url))
                return Response(
                    content="{}",
                    status_code=402,
                    media_type="application/json",
                    headers={"PAYMENT-REQUIRED": encode_header(descriptor)},
                )

            if http_client is None:
                raise HTTPException(503, "HTTP client not initialised")

            with PAYMENT_SETTLEMENT_DURATION.time():
                result = await settle_payment(payment_signature, http_client)

            PAYMENT_FACILITATOR.labels(outcome=result.status.value).inc()

            if result.status == SettlementStatus.FACILITATOR_UNREACHABLE:
                raise HTTPException(503, result.message)
            if result.status != SettlementStatus.SETTLED:
                raise HTTPException(402, result.message)

            settlement_tx_hash = result.settlement_tx_hash
            payer = result.payer

        code = _generate_code()

        if PAYMENT_ENABLED:
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO urls (code, url, settlement_tx_hash, payer_address, settled_at)
                    VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (url) DO NOTHING
                    RETURNING code
                    """,
                    code, body.url, settlement_tx_hash, payer,
                )
            except asyncpg.UniqueViolationError:
                # settlement_tx_hash UNIQUE — the facilitator returned a cached
                # prior settlement, which we've already stored for a different
                # URL attempt. This is the load-bearing replay signal under
                # Permit2 (see design doc §5.1).
                PAYMENT_REPLAY_ATTEMPTS.inc()
                raise HTTPException(409, "Settlement transaction already used")

            if row is None:
                existing_row = await conn.fetchrow("SELECT code FROM urls WHERE url = $1", body.url)
                if existing_row:
                    return JSONResponse(_short_url(existing_row["code"], body.url), status_code=200)
                raise HTTPException(500, "Failed to create or resolve short URL")
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO urls (code, url)
                VALUES ($1, $2)
                ON CONFLICT (url) DO UPDATE SET url = EXCLUDED.url
                RETURNING code
                """,
                code, body.url,
            )

        URLS_CREATED.inc()

        if redis_client:
            try:
                await redis_client.setex(f"url:{row['code']}", REDIS_TTL, body.url)
            except Exception:
                pass

        response = ShortenResponse(
            code=row["code"], short_url=f"{BASE_URL}/{row['code']}", original_url=body.url,
        )
        headers = {}
        if settlement_tx_hash:
            headers["PAYMENT-RESPONSE"] = settled_response_header(
                SettlementResult(
                    status=SettlementStatus.SETTLED,
                    message="settled",
                    settlement_tx_hash=settlement_tx_hash,
                    payer=payer or "",
                )
            )
        return JSONResponse(response.model_dump(), status_code=201, headers=headers)


@app.get("/payment-info")
async def payment_info():
    """Diagnostic — tells the caller what payment shape /shorten will accept."""
    return {
        "payment_enabled": PAYMENT_ENABLED,
        "facilitator_url": os.getenv("FACILITATOR_URL", ""),
        "service_wallet": os.getenv("SERVICE_WALLET_ADDRESS", ""),
        "sbc_contract": os.getenv("SBC_CONTRACT_ADDRESS", ""),
        "shorten_fee": int(os.getenv("SHORTEN_FEE", "1000")),
        "network": os.getenv("NETWORK_CAIP2", "eip155:72344"),
    }


# ---------------------------------------------------------------------------
# /{code} — redirect
# ---------------------------------------------------------------------------
@app.get("/{code}")
async def redirect_url(code: str):
    if redis_client:
        try:
            url = await redis_client.get(f"url:{code}")
            if url:
                CACHE_HITS.inc()
                return RedirectResponse(url=url, status_code=302)
        except Exception:
            pass  # Redis down — fall through to Postgres.

    CACHE_MISSES.inc()
    if db_pool is None:
        raise HTTPException(503, "Database unavailable")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT url FROM urls WHERE code = $1", code)

    if not row:
        raise HTTPException(404, f"Code '{code}' not found")

    url = row["url"]
    if redis_client:
        try:
            await redis_client.setex(f"url:{code}", REDIS_TTL, url)
        except Exception:
            pass

    return RedirectResponse(url=url, status_code=302)
