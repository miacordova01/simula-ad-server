# syntax=docker/dockerfile:1
# Multi-stage: wheels are built once and copied in, so the runtime image
# carries no compiler toolchain and no build cache.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Run as a non-root user: this process renders attacker-influenced strings and
# talks to an LLM, so it should not be uid 0.
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-index --find-links=/wheels -r requirements.txt && rm -rf /wheels

COPY app ./app
COPY assets ./assets

USER appuser

# Cloud Run injects $PORT; default to 8080 for local runs.
ENV PORT=8080
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0) if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8080)}/healthz', timeout=2).status==200 else sys.exit(1)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
