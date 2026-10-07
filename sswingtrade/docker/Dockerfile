# SSWingTrade API image (Python 3.11). Build context = the sswingtrade/ folder (see docker-compose.yml).

# ---------------------------------------------------------------- stage 1: build the virtualenv
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt .
RUN pip install -r requirements.txt


# ---------------------------------------------------------------- stage 2: runtime
FROM python:3.11-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# libpq5   -> PostgreSQL client library (psycopg2)
# libgomp1 -> OpenMP runtime: LightGBM and XGBoost fail to import without it on slim images
# curl     -> container healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
RUN mkdir -p /app/models /app/logs
COPY app /app/app

# /health answers 200 even when Redis/Ollama are down ("degraded"), so it only fails if the API itself is dead
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

EXPOSE 8000

# ONE worker on purpose: the scheduler (daily collection, 20:00 trading cycle) lives inside the process,
# several workers would run every job several times.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
