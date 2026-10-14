# DIVE Project Memory service image.
# Build from the repository root:
#   docker build -f ops/docker/Dockerfile -t dive-memory:0.4.0 .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DIVE_DB_PATH=/data/memory.db

WORKDIR /app

# Dependencies first so source changes do not invalidate the layer.
COPY requirements/runtime.lock ./requirements/runtime.lock
RUN pip install --no-cache-dir -r requirements/runtime.lock

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && useradd --system --uid 900 --create-home dive \
    && install -d -o dive -g dive /data

USER dive
EXPOSE 8765
VOLUME ["/data"]

# Liveness must come from the running service, not from a static file:
# /api/readiness probes the actual storage backend.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/readiness', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["dive-memory"]
CMD ["--db", "/data/memory.db", "serve", "--host", "0.0.0.0", "--port", "8765"]
