FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Bake the DuckDB FTS extension into the image. Retrieval LOADs it per query,
# and LOAD does not install: without this the container answers every question
# with citations silently disabled. LOAD here too — an INSTALL that succeeds
# while LOAD fails (partial download, build/runtime mismatch) is exactly the
# failure being defended against, and would otherwise ship green.
RUN python -c "import duckdb; duckdb.connect().execute('INSTALL fts; LOAD fts')"
COPY analytics_agent.py app.py data_model.py city_config.py rag.py ./
COPY cities/ cities/
COPY static/ static/
EXPOSE 8000
# --proxy-headers + --forwarded-allow-ips: the container is reachable only via
# the cloudflared tunnel, so trust the forwarded client address. The app also
# reads CF-Connecting-IP directly (see _client_ip) for the per-IP rate limit.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "30", "--timeout-graceful-shutdown", "30", "--proxy-headers", "--forwarded-allow-ips", "*"]
