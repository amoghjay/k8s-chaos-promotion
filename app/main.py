"""URL shortener — FastAPI + Redis + Postgres.

Payments are x402 Permit2 via the Radius facilitator: payment.py forwards the
client's PAYMENT-SIGNATURE to /verify and /settle; the settlement tx hash is
persisted to Postgres. The app itself never touches the chain.
"""

import asyncio
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
from prometheus_client import Counter, Gauge, Histogram
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, field_validator

from payment import (
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
PAYMENT_ENABLED = bool(os.getenv("FACILITATOR_URL", "").strip())

logger = logging.getLogger("url_shortener")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Prometheus metrics (beyond auto-instrumented request count + latency)
# ---------------------------------------------------------------------------
CACHE_HITS = Counter("url_shortener_cache_hits_total", "Redis cache hits")
CACHE_MISSES = Counter("url_shortener_cache_misses_total", "Redis cache misses (Postgres fallback)")
URLS_CREATED = Counter("url_shortener_urls_created_total", "Short URLs created")

# Dependency reachability (1/0), refreshed by the monitor every 10s. Health
# endpoints are excluded from request metrics, so this gauge is the only
# degraded-state signal available for chaos scoring and Grafana.
DEPENDENCY_UP = Gauge(
    "url_shortener_dependency_up",
    "1 if the backing dependency is reachable, else 0.",
    ["dependency"],
)

# Outcome labels mirror payment.SettlementStatus, plus `settled` on success.
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
_db_lock = asyncio.Lock()  # Serializes lazy pool (re)creation in ensure_db_pool()
_redis_lock = asyncio.Lock()  # Serializes lazy client (re)creation in ensure_redis()

ALPHABET = string.ascii_letters + string.digits


def _generate_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


# Schema bootstrap: CREATE for fresh installs; the DO block migrates older
# deployments whose column was named tx_hash.
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
# Database pool (lazy + retryable)
# ---------------------------------------------------------------------------
async def ensure_db_pool() -> asyncpg.Pool | None:
    """Lazily (re)create the asyncpg pool.

    Called from /ready every 5s, so the readiness probe doubles as the
    recovery loop if pool creation lost a boot race against Postgres.
    """
    global db_pool
    if db_pool is not None:
        return db_pool
    async with _db_lock:
        if db_pool is not None:  # built while we waited on the lock
            return db_pool
        try:
            pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=2,
                max_size=10,
                command_timeout=10,
                # 5s connect bound (default 60s) so a black-holed Postgres
                # can't hold _db_lock and stack up readiness probes.
                timeout=5.0,
            )
            async with pool.acquire() as conn:
                await conn.execute(SCHEMA_SQL)
            db_pool = pool
            logger.info("Postgres connected")
        except Exception as e:
            logger.error("Postgres pool creation failed (will retry): %s", e)
            db_pool = None
    return db_pool


async def ensure_redis() -> aioredis.Redis | None:
    """Lazily (re)connect Redis, the twin of ensure_db_pool().

    ping() is the real validation (from_url is lazy), so the global is only
    published on success. A live client is never rebuilt: mid-life blips heal
    via redis-py's own reconnect.
    """
    global redis_client
    if redis_client is not None:
        return redis_client
    async with _redis_lock:
        if redis_client is not None:  # built while we waited on the lock
            return redis_client
        try:
            # 0.1s timeouts: a dead cache fails fast to the Postgres fallback.
            client = aioredis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=0.1,
                socket_timeout=0.1,
            )
            await client.ping()
            redis_client = client
            logger.info("Redis connected")
        except Exception as e:
            logger.error("Redis connection failed (will retry): %s", e)
            redis_client = None
    return redis_client


# ---------------------------------------------------------------------------
# Dependency monitor — refreshes DEPENDENCY_UP gauges independent of traffic
# ---------------------------------------------------------------------------
async def _monitor_dependencies(interval: float = 10.0) -> None:
    """Publish reachability gauges on a timer, fresh even when idle.

    Going through ensure_db_pool()/ensure_redis() also gives a second
    reconnect path alongside /ready.
    """
    while True:
        pg_up = 0
        pool = await ensure_db_pool()
        if pool:
            try:
                async with pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                pg_up = 1
            except Exception:
                pg_up = 0
        DEPENDENCY_UP.labels(dependency="postgres").set(pg_up)

        redis_up = 0
        client = await ensure_redis()
        if client:
            try:
                await client.ping()
                redis_up = 1
            except Exception:
                redis_up = 0
        DEPENDENCY_UP.labels(dependency="redis").set(redis_up)

        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client

    # Best-effort at boot; /ready and the monitor retry both if either is down.
    await ensure_db_pool()
    await ensure_redis()

    # One AsyncClient per pod lifetime — the facilitator is hit on every
    # /shorten, so connection pooling and keepalive matter.
    http_client = httpx.AsyncClient()
    logger.info(
        "Payment %s (facilitator=%s)",
        "enabled" if PAYMENT_ENABLED else "disabled",
        os.getenv("FACILITATOR_URL", "<unset>"),
    )

    monitor_task = asyncio.create_task(_monitor_dependencies())

    yield

    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass
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


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------
@app.get("/livez")
async def livez():
    """Liveness: process-only, deliberately touches neither Postgres nor Redis.

    A dependency outage must fail readiness (pull the pod from endpoints),
    never liveness — restarting the pod can't fix a downstream DB and only
    amplifies the outage.
    """
    return JSONResponse({"status": "alive"})


@app.get("/health")
async def health():
    """Diagnostic only (not a probe target). Reports Postgres + Redis reachability."""
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
    """Readiness. Postgres always required; Redis only before the first success.

    After the first successful check, a Redis outage must not pull the pod
    from the Service.
    """
    global _started

    # Rebuilds the pool if a boot race left it None — the probe is the recovery loop.
    pool = await ensure_db_pool()

    pg_ok = False
    if pool:
        try:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            pg_ok = True
        except Exception:
            pass

    if not pg_ok:
        return JSONResponse({"status": "not ready", "postgres": "down"}, status_code=503)

    if not _started:
        redis_ok = False
        client = await ensure_redis()
        if client:
            try:
                await client.ping()
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

    # Writes need Postgres (Redis is only a cache), so a mid-request outage
    # degrades to 503 + retry rather than a 500. If Postgres dies after
    # settle_payment succeeds but before the INSERT, the client retries the
    # same signed request safely: the facilitator keys settlements off the
    # payload + signature (a replay returns the cached tx), and the INSERT is
    # ON CONFLICT-idempotent — so the payment is pending, not lost.
    try:
        async with db_pool.acquire() as conn:
            # Already shortened — return the existing code, no new payment required.
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
                    # settlement_tx_hash is UNIQUE: the facilitator returned a
                    # cached prior settlement already stored for another URL —
                    # the replay signal under Permit2.
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

            headers = {}
            if settlement_tx_hash:
                headers["PAYMENT-RESPONSE"] = settled_response_header(
                    settlement_tx_hash, payer or "",
                )
            return JSONResponse(
                _short_url(row["code"], body.url), status_code=201, headers=headers,
            )
    except (asyncpg.PostgresConnectionError, asyncpg.InterfaceError,
            ConnectionError, OSError, asyncio.TimeoutError) as e:
        # Postgres went away mid-request — degrade to 503, not 500. The class
        # name is logged so any connection error missing from this tuple shows up.
        logger.warning("postgres unavailable during /shorten (%s): %s", type(e).__name__, e)
        raise HTTPException(503, "Database temporarily unavailable — retry shortly") from e


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
