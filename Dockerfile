# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# QuantResearch — production image (Flask + Gunicorn) for Coolify Dockerfile Build Pack
# Entry: gunicorn app:app  ·  binds $PORT  ·  Python 3.11  ·  Postgres-or-SQLite
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080

WORKDIR /app

# System deps:
#   libgomp1 — OpenMP runtime required by torch / scipy CPU wheels
#   curl     — container HEALTHCHECK
#   tini     — PID 1 init (clean signal handling for the app's daemon threads)
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 curl tini \
 && rm -rf /var/lib/apt/lists/*

# ── Python dependencies (own layer for build caching) ───────────────────────
# Install CPU-only PyTorch from the official CPU index FIRST so we don't pull the
# ~2.5 GB CUDA wheel from PyPI (FinBERT runs CPU-only, lazy-loaded). requirements.txt
# then sees torch already satisfied and installs everything else from PyPI.
COPY requirements.txt ./
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.3.0" \
 && pip install -r requirements.txt

# ── Application code ────────────────────────────────────────────────────────
COPY . .

# Non-root runtime user. cache/ (SQLite auth.db + payment/QR uploads) and logs/
# must be writable — mount cache/ as a persistent Coolify volume for durability.
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /app/cache /app/logs \
 && chown -R appuser:appuser /app
USER appuser

# Documentation only; Coolify injects $PORT and the app binds it at runtime.
EXPOSE 8080

# Liveness probe → the app's existing lightweight /healthz (no DB/broker dependency).
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# Production WSGI server. --workers 1 is REQUIRED: scan_state and the Angel WebSocket
# are in-process/per-worker (the app warns if WEB_CONCURRENCY > 1). Reads $PORT.
ENTRYPOINT ["tini", "--"]
CMD ["sh", "-c", "exec gunicorn app:app --bind 0.0.0.0:${PORT} --workers 1 --threads 4 --timeout 120 --access-logfile - --error-logfile -"]
