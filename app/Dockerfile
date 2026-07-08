FROM python:3.12-slim AS builder

WORKDIR /build

# gcc + libpq-dev for asyncpg's native extension
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

RUN addgroup --system app && adduser --system --ingroup app app

WORKDIR /app

COPY --from=builder /install /usr/local

COPY --chown=app:app . .

USER app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Single worker by default: prometheus_client keeps counters in per-process
# memory, so multiple uvicorn workers would each expose their own counts.
ENV WORKERS=1

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${WORKERS}"]
