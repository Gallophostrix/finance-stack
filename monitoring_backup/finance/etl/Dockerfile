# finance/etl/Dockerfile
FROM --platform=linux/arm64/v8 python:3.11.9-slim

ARG VERSION=0.0.0
LABEL org.opencontainers.image.title="Finance ETL"
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.authors="Mikhaïl Duong"
LABEL org.opencontainers.image.source="https://https://github.com/Gallophostrix/finance-stack"

# 1) Clean Python runtime, providing logs
ENV ETL_VERSION=${VERSION}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# 2) Minimal system packages (`ca-certificates curl` if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# 3) Python dependencies (requirements are copied before code for caching)
COPY requirements /app/requirements
RUN python -m pip install --upgrade pip && \
    pip install -r /app/requirements/base.txt && \
    pip install -r /app/requirements/api.txt && \
    pip install -r /app/requirements/calc.txt

# 4) ETL code
COPY etl /app/etl

# 6) Safety: non-root user (UID 10001 to avoid conflicts with host)
RUN useradd -r -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

# 7) By default, keep container alive (override when running)
CMD ["sleep", "infinity"]
