"""
URL Shortener — FastAPI + Redis + Postgres
Designed for chaos engineering demonstrations.

Payment path is x402 Permit2 via the Radius facilitator. The app never touches
the chain — payment.py forwards the client's PAYMENT-SIGNATURE to the
facilitator's /verify and /settle endpoints, and persists the resulting
settlement tx hash to Postgres.
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

# Dependency reachability (1/0), refreshed by the monitor every 10s. /health
# and /ready are excluded from request metrics and a 200 can't tell "ok" from
# "degraded", so this gauge is the only clean degraded-state signal for chaos
# scoring + Grafana.
DEPENDENCY_UP = Gauge(
    "url_shortener_dependency_up",
    "1 if the backing dependency is reachable, else 0.",
    ["dependency"],
)

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
_db_lock = asyncio.Lock()  # Serializes lazy pool (re)creation in ensure_db_pool()
_redis_lock = asyncio.Lock()  # Serializes lazy client (re)creation in ensure_redis()

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
# Database pool (lazy + retryable)
# ---------------------------------------------------------------------------
async def ensure_db_pool() -> asyncpg.Pool | None:
    """Lazily (re)create the asyncpg pool, rebuilding it on demand.

    Boot-only create_pool() pinned db_pool to None when the app won the
    bring-up race against Postgres' WAL recovery, wedging /ready forever
    (chaos found this twice — see LEARNINGS Phase 6.1). /ready calls this
    every 5s, so the probe itself is the recovery loop.
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

    Boot-only connect pinned redis_client to None on a cluster-bring-up DNS
    race, wedging /ready forever. ping() is the real validation (from_url is
    lazy), so publish the global only on success. Never rebuilds a live client:
    mid-life blips heal via redis-py's own reconnect.
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

    Going through ensure_db_pool()/ensure_redis() also makes this a second
    reconnect path alongside /ready, so a wedged dependency recovers with no
    traffic.
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

    # One AsyncClient for the lifetime of the pod — connection pooling +
    # keepalive matter when we hit the facilitator on every /shorten.
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
    """Liveness. Process-only — deliberately does NOT touch Postgres or Redis.

    A backing-dependency outage must fail READINESS (pull the pod from Service
    endpoints), never LIVENESS (restart the pod). Restarting never fixes a
    downstream DB; it only amplifies the outage. Chaos experiment #1 proved this:
    when liveness pointed at /health (PG-coupled), a 60s Postgres outage tripped
    the probe (3x503) and the kubelet restarted every replica, turning a
    recoverable dependency blip into a full app outage.
    """
    return JSONResponse({"status": "alive"})


@app.get("/health")
async def health():
    """Diagnostic only (NOT a probe target). Reports PG + Redis reachability."""
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

    # Drives the lazy-pool retry: if a boot race left db_pool=None, this rebuilds
    # it here, so the readiness probe itself is the recovery loop.
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

    # Postgres is the source of truth for the code↔URL mapping; a write can't
    # fall back to Redis (cache, not durable). When PG is unreachable we fail
    # FAST with 503 + Retry-After rather than letting an asyncpg connection
    # error bubble into a 500. The `is None` guard above only covers "pool never
    # built" — it does NOT cover PG dying mid-request, which is where the
    # acquire()/fetchrow() calls below throw.
    #
    # Paid-but-unwritten window: if PG dies AFTER settle_payment succeeds but
    # before the INSERT, the client gets 503 and retries the SAME signed request.
    # That recovers cleanly and without double-charging because (a) the Radius
    # facilitator is idempotent — "Settlements are keyed from the payment payload
    # and signature, so duplicate settlement attempts can return the existing
    # result" (docs: x402-integration) — so the replay returns the cached tx, and
    # (b) the INSERT is ON CONFLICT (url) idempotent. So the payment is pending,
    # not lost. (A durable outbox would be over-engineering here.)
    try:
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
        # PG went away mid-request (e.g. pod-failure). Degrade cleanly: 503, not 500.
        # type(e).__name__ in the log so a re-run tells us if any connection-error
        # class slipped this tuple (then widen it).
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
