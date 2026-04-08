"""
URL Shortener — FastAPI + Redis + Postgres
Designed for chaos engineering demonstrations.
"""

import os
import secrets
import string
import logging
from contextlib import asynccontextmanager

import asyncio
import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from prometheus_client import Counter, Histogram
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, field_validator
from payment import init_web3, verify_payment, get_payment_info, PaymentStatus

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://urlshortener:password@localhost:5432/urlshortener")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
CODE_LENGTH = int(os.getenv("CODE_LENGTH", "8"))
REDIS_TTL = int(os.getenv("REDIS_TTL", "3600"))
PAYMENT_ENABLED = bool(os.getenv("RADIUS_RPC_URL", "").strip())

logger = logging.getLogger("url_shortener")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Prometheus metrics (beyond auto-instrumented request count + latency)
# ---------------------------------------------------------------------------
CACHE_HITS = Counter("url_shortener_cache_hits_total", "Redis cache hits")
CACHE_MISSES = Counter("url_shortener_cache_misses_total", "Redis cache misses (Postgres fallback)")
URLS_CREATED = Counter("url_shortener_urls_created_total", "Short URLs created")
PAYMENT_VERIFICATION_DURATION = Histogram(
    "payment_verification_duration_seconds",
    "Time spent verifying payment transactions",
)
PAYMENT_VERIFICATIONS = Counter(
    "payment_verifications_total",
    "Total payment verification attempts by status",
    ["status"],
)
PAYMENT_402_RESPONSES = Counter(
    "payment_402_responses_total",
    "Total HTTP 402 responses for payment-required requests",
)
PAYMENT_REPLAY_ATTEMPTS = Counter(
    "payment_replay_attempts_total",
    "Total payment replay attempts detected via tx_hash uniqueness",
)

# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------
db_pool: asyncpg.Pool | None = None
redis_client: aioredis.Redis | None = None
_started: bool = False  # Flipped once after first successful readiness check

ALPHABET = string.ascii_letters + string.digits


def _generate_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, redis_client

    # Postgres
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10, command_timeout=10)
        async with db_pool.acquire() as conn:
            try:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS urls (
                        code       VARCHAR(16) PRIMARY KEY,
                        url        TEXT NOT NULL UNIQUE,
                        tx_hash    VARCHAR(66) UNIQUE,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
            except (asyncpg.UniqueViolationError, asyncpg.DuplicateTableError):
                pass  # Another pod created it first - ignore
        logger.info("Postgres connected")
    except Exception as e:
        logger.error("Postgres failed: %s", e)
        db_pool = None

    # Redis
    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis connected")
    except Exception as e:
        logger.error("Redis failed: %s", e)
        redis_client = None

    if PAYMENT_ENABLED:
        init_web3()

    yield

    if db_pool:
        await db_pool.close()
    if redis_client:
        await redis_client.aclose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="URL Shortener", version="1.0.0", lifespan=lifespan)

Instrumentator(
    excluded_handlers=["/metrics", "/health", "/ready"],
).instrument(app).expose(app)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ShortenRequest(BaseModel):
    url: str
    tx_hash: str | None = None

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
# Routes
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Health endpoints (separate liveness and readiness)
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    """
    LIVENESS probe. Returns 200 if the process is alive and Postgres is reachable.
    Redis being down does NOT make this fail — the app degrades gracefully
    by falling back to Postgres for reads. During chaos testing, killing Redis
    should NOT cause Kubernetes to restart or remove the FastAPI pod.
    """
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

    # Check Redis but don't fail on it
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
    """
    READINESS probe. During initial startup, requires BOTH Postgres and Redis
    to be reachable (so Kubernetes doesn't send traffic before backends are up).
    After the first successful check, flips a flag and only requires Postgres —
    so killing Redis mid-run does NOT pull the pod from the Service.
    """
    global _started

    # Postgres is always required
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

    # Redis only required before first successful startup
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

        _started = True  # Both backends confirmed — never require Redis again

    return JSONResponse({"status": "ready"})

@app.post("/shorten", response_model=ShortenResponse, status_code=201)
async def shorten_url(body: ShortenRequest):
    if db_pool is None:
        raise HTTPException(503, "Database unavailable")

    async with db_pool.acquire() as conn:
        # If this URL already exists, return it without requiring another payment.
        existing_url = await conn.fetchrow("SELECT code FROM urls WHERE url = $1", body.url)
        if existing_url:
            return JSONResponse(
                {
                    "code": existing_url["code"],
                    "short_url": f"{BASE_URL}/{existing_url['code']}",
                    "original_url": body.url,
                },
                status_code=200,
            )

        tx_hash: str | None = None
        if PAYMENT_ENABLED:
            if not body.tx_hash:
                PAYMENT_402_RESPONSES.inc()
                return JSONResponse(get_payment_info(), status_code=402)

            tx_hash = body.tx_hash.strip()
            with PAYMENT_VERIFICATION_DURATION.time():
                payment_result = await asyncio.to_thread(verify_payment, tx_hash)

            PAYMENT_VERIFICATIONS.labels(status=payment_result.status.value).inc()
            if payment_result.status == PaymentStatus.RPC_ERROR:
                raise HTTPException(503, payment_result.message)
            if payment_result.status != PaymentStatus.SUCCESS:
                raise HTTPException(400, payment_result.message)
        code = _generate_code()
        if PAYMENT_ENABLED and tx_hash:
            try:
                row = await conn.fetchrow(
                    """
                    INSERT INTO urls (code, url, tx_hash)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (url) DO NOTHING
                    RETURNING code
                    """,
                    code, body.url, tx_hash
                )
            except asyncpg.UniqueViolationError:
                PAYMENT_REPLAY_ATTEMPTS.inc()
                raise HTTPException(409, "Transaction hash already used")
            if row is None:
                existing_row = await conn.fetchrow("SELECT code FROM urls WHERE url = $1", body.url)
                if existing_row:
                    return JSONResponse(
                        {
                            "code": existing_row["code"],
                            "short_url": f"{BASE_URL}/{existing_row['code']}",
                            "original_url": body.url,
                        },
                        status_code=200,
                    )
                raise HTTPException(500, "Failed to create or resolve short URL")
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO urls (code, url)
                VALUES ($1, $2)
                ON CONFLICT (url) DO UPDATE SET url = EXCLUDED.url
                RETURNING code
                """,
                code, body.url
            )

        URLS_CREATED.inc()

        if redis_client:
            try:
                await redis_client.setex(f"url:{row['code']}", REDIS_TTL, body.url)
            except Exception:
                pass

        return ShortenResponse(code=row["code"], short_url=f"{BASE_URL}/{row['code']}", original_url=body.url)


@app.get("/payment-info")
async def payment_info():
    info = get_payment_info()
    info["payment_enabled"] = PAYMENT_ENABLED
    return info


@app.get("/{code}")
async def redirect_url(code: str):
    # 1. Try Redis (cache hit)
    if redis_client:
        try:
            url = await redis_client.get(f"url:{code}")
            if url:
                CACHE_HITS.inc()
                return RedirectResponse(url=url, status_code=302)
        except Exception:
            pass  # Redis down — fall through to Postgres

    # 2. Postgres fallback
    CACHE_MISSES.inc()
    if db_pool is None:
        raise HTTPException(503, "Database unavailable")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT url FROM urls WHERE code = $1", code)

    if not row:
        raise HTTPException(404, f"Code '{code}' not found")

    url = row["url"]

    # Re-warm cache (best-effort)
    if redis_client:
        try:
            await redis_client.setex(f"url:{code}", REDIS_TTL, url)
        except Exception:
            pass

    return RedirectResponse(url=url, status_code=302)
