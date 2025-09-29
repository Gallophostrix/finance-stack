# finance/etl/Dockerfile
FROM python:3.11.9-slim

# 1) Clean Python runtime, providing logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 2) Minimal system packages (`ca-certificates curl` if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# 3) Python dependencies (requirements are copied before code for caching)
COPY requirements/base.txt /tmp/req-base.txt
COPY requirements/auto.txt /tmp/req-api.txt
COPY requirements/mwr.txt  /tmp/req-calc.txt
RUN pip install --no-cache-dir -r /tmp/req-base.txt \
    && pip install --no-cache-dir -r /tmp/req-api.txt \
    && pip install --no-cache-dir -r /tmp/req-calc.txt

# 4) ETL code
COPY api /app/etl/api
COPY calc  /app/etl/calc

# 6) Safety: non-root user (UID 10001 to avoid conflicts with host)
RUN useradd -r -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

# 7) By default, run the api ETL
ENTRYPOINT ["python", "-m", "etl.api.etl_api"]