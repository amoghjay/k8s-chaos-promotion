# ─── Build stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies (needed for asyncpg native compilation)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ─── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Non-root user for least-privilege
RUN addgroup --system app && adduser --system --ingroup app app

WORKDIR /app

# Copy installed packages from the builder
COPY --from=builder /install /usr/local

# Copy application source
COPY --chown=app:app . .

USER app

# Expose the API port
EXPOSE 8000

# Health check so Docker / Compose knows when the app is ready
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Run with 2 workers by default; override via WORKERS env var in production
ENV WORKERS=2

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${WORKERS}"]
