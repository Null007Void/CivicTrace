FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CIVICTRACE_LIVE_ENABLED=0 \
    CIVICTRACE_CASE_STORE=/var/lib/civictrace/cases/cases.json \
    CIVICTRACE_SPEND_BACKEND=sqlite CIVICTRACE_SPEND_STORE_PATH=/var/lib/civictrace/spend \
    CIVICTRACE_SPEND_STORE_EXPECT_ROOT=/var/lib/civictrace/spend \
    CIVICTRACE_DEPLOYMENT_ID=civictrace-local-v1 CIVICTRACE_MAX_LIVE_ATTEMPTS=2 \
    CIVICTRACE_MAX_LIVE_USD=0.90 CIVICTRACE_EST_USD_PER_PROVIDER_REQUEST=0.15 \
    NEBIUS_MODEL=nvidia/nemotron-3-super-120b-a12b
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && useradd --create-home --uid 10001 civic \
    && mkdir -p /var/lib/civictrace/cases /var/lib/civictrace/spend \
    && chown -R 10001:10001 /var/lib/civictrace
COPY civictrace/ ./civictrace/
COPY data/ ./data/
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY docs/SBOM.runtime.cdx.json ./docs/SBOM.runtime.cdx.json
USER civic
VOLUME ["/var/lib/civictrace"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"]
CMD ["uvicorn","civictrace.app:app","--host","0.0.0.0","--port","8080","--workers","1"]
